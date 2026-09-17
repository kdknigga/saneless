"""The lazy background thread that keeps the status strip's cache warm."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Final

from saneless.checks import run_checks
from saneless.worker import STOP_JOIN_SECONDS

if TYPE_CHECKING:
    from collections.abc import Callable

    from saneless.checks import CheckContext

    from .checks_cache import CheckCache

__all__ = ["TICK_SECONDS", "WATCH_WINDOW_SECONDS", "CheckRefresher"]

logger = logging.getLogger(__name__)

# How long after the last page load the refresher keeps probing (D-05).
# Ninety seconds is three TTL cycles after the operator walks away, which is
# enough that a reload two minutes later still finds warm results without an
# appliance nobody is looking at generating a Paperless request and a scanner
# probe every thirty seconds for ever.  Read at call time, so tests can
# shorten it.
WATCH_WINDOW_SECONDS: Final = 90.0

# The Event.wait granularity of the idle loop.  One second keeps Refresh
# responsive -- a click that expires the cache is picked up within a tick --
# and costs nothing, because a tick with nothing to do is two lock acquisitions
# and a subtraction.  Read at call time.
TICK_SECONDS: Final = 1.0


class CheckRefresher:
    """
    A daemon thread that refills the check cache, but only while someone looks.

    D-04's whole point is that a page render never probes: an unplugged scanner
    host is a TCP connect that hangs until the OS gives up, so the mechanism has
    to be "the page reads a cache a background thread fills", not "the page
    probes quickly".  This is that thread.

    It follows :class:`~saneless.worker.ScanWorker` in every structural respect
    D-07 names -- a daemon thread built in ``__init__`` and started separately,
    a ``_stopping`` :class:`threading.Event` set once by :meth:`stop`, an
    ``Event.wait`` idle sleep rather than a blocking one so stopping wakes it
    at once, and a join bounded by the same ``STOP_JOIN_SECONDS`` imported from
    that module rather than redefined -- or, when the lifespan brings both
    threads down against one deadline, by whatever is left of it (WR-07).

    Every dependency is injected, and the context arrives from a factory rather
    than from ``app.state``, so the policy can be exercised by calling
    :meth:`_tick` directly with no thread, no application and no network.

    Args:
        cache: The cache this fills.
        context_factory: Builds the ``CheckContext`` for one refresh.
        scanner_gate: Returns the worker's scanner gate at call time, because
            the worker may be rebuilt independently of the refresher.
        clock: The monotonic source the watch window is measured with.

    """

    def __init__(
        self,
        cache: CheckCache,
        context_factory: Callable[[], CheckContext],
        scanner_gate: Callable[[], threading.Lock],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Build the thread and its synchronisation, without starting it."""
        self._cache = cache
        self._context_factory = context_factory
        self._scanner_gate = scanner_gate
        self._clock = clock
        self._thread = threading.Thread(target=self._run, daemon=True)
        # Set once by stop(); read by the idle wait, which it wakes at once.
        self._stopping = threading.Event()
        # Guards every read and every write of self._last_watched, and nothing
        # else.  Request threads write it and the refresher thread reads it.
        self._watch_lock = threading.Lock()
        # When a page last said someone was looking, or None for "nobody ever
        # has".  None rather than 0.0 because time.monotonic() on Linux counts
        # from boot: for the first ninety seconds of an appliance's life 0.0
        # would sit *inside* the watch window and the refresher would probe
        # with no watcher at all -- exactly the case D-05 exists to prevent,
        # and exactly when an appliance boots.
        self._last_watched: float | None = None

    def start(self) -> None:
        """Start the refresher thread."""
        self._thread.start()
        logger.info("CheckRefresher started")

    def note_watcher(self) -> None:
        """
        Record that a page is being looked at right now (D-05).

        Called from request threads by the index page and the strip routes.
        It writes one float and nothing else: it cannot trigger a probe, so no
        number of page loads raises the probe rate above the TTL (T-30-26).
        The stamp is not persisted and a restart forgets it, which is the whole
        of what "someone is watching" means here.
        """
        now = self._clock()
        with self._watch_lock:
            self._last_watched = now

    def build_context(self) -> CheckContext:
        """
        Assemble the dependencies for one probe, exactly as a tick would.

        Public because D-09's Refresh button probes from a request handler and
        has to bypass the TTL to do it: routing that click through
        :meth:`_tick` would do nothing at all while the cache is still fresh,
        which is exactly when somebody who has just plugged the scanner back in
        presses the button.  Handing the one factory back out is what keeps the
        button's probe and the thread's probe building the *same* context,
        instead of the route assembling a second one that could drift.

        It builds a context and nothing else: no probe, no cache write and no
        lock, so calling it from a request thread costs nothing.

        Returns:
            A context with ``skip_scanner`` unset.  The caller sets that from
            its own non-blocking attempt on the scanner gate, because only the
            caller knows whether it got the gate.

        """
        return self._context_factory()

    def request_stop(self) -> None:
        """
        Ask the refresher to stop, without waiting for the thread.

        This is the signal half of :meth:`stop`, split out for a caller that
        has more than one thread to bring down.  The lifespan has two, and it
        joins them one after the other, so signalling both before joining
        either does not by itself bound the total -- an earlier version of this
        docstring claimed it did, which WR-07 corrects.  What the early signal
        actually buys is that a refresher merely between ticks wakes on the
        event during the worker's join and exits for free, so its own join is
        skipped.  What holds the worst case at one ``STOP_JOIN_SECONDS``
        instead of one per thread is the single deadline the lifespan takes
        before either join and passes to :meth:`stop` (Amendment A-7).

        Setting the event is the whole of it, so this never blocks, and calling
        it on a refresher that was never started -- a lifespan that failed
        during startup -- is safe.
        """
        self._stopping.set()

    def stop(self, timeout: float | None = None) -> bool:
        """
        Stop the refresher and report whether the thread actually stopped.

        The event is set first, so a loop already awake finishes its tick and
        then exits rather than starting another.  The join is bounded by the
        worker's ``STOP_JOIN_SECONDS``, or by the caller's remaining share of
        it: the lifespan takes one deadline for both threads, so what the
        worker's join already spent is not spent again here (WR-07).  When this
        returns ``False`` the thread is still inside a probe and may still
        write to the cache, so the lifespan must leave the Paperless client and
        the scanner open (A-7).  Calling it again, or on a refresher never
        started, is safe -- including after :meth:`request_stop`, which sets
        the same event.

        Args:
            timeout: Seconds to wait for the thread, or ``None`` for the whole
                of ``STOP_JOIN_SECONDS``.  A negative value is clamped to
                zero, so a caller arriving with its budget already spent gets
                a poll rather than the unbounded wait ``join(None)`` would be.

        Returns:
            Whether the refresher thread has stopped.

        """
        self.request_stop()
        if self._thread.is_alive():
            budget = STOP_JOIN_SECONDS if timeout is None else max(0.0, timeout)
            self._thread.join(timeout=budget)
        stopped = not self._thread.is_alive()
        if stopped:
            logger.info("CheckRefresher stopped")
        return stopped

    def _run(self) -> None:
        """
        Tick until stopped.

        ``Event.wait`` is the idle sleep here, and a blocking sleep is never
        used: setting the event returns from the wait immediately, so
        ``stop()`` wakes the thread at once instead of after a full tick.  That
        is the same property ``queue.shutdown(immediate=True)`` gives the
        worker's blocked ``get()``.
        """
        while not self._stopping.wait(TICK_SECONDS):
            self._tick()

    def _tick(self) -> None:
        """
        Refresh the cache if a refresh is due, and do nothing otherwise.

        The whole refresh policy lives here and only here, and it is a plain
        synchronous method so the tests can call it with no thread running.

        Two guards come first, in this order: nobody is watching (D-05), and
        the cache is still fresh (D-03).  Either one means no probe, which is
        what bounds the probe rate no matter how many page loads land.

        The scanner gate is then tried without blocking.  Failing to take it
        means a scan is running, and the refresh goes ahead for the four checks
        that never needed the scanner with ``skip_scanner`` set -- D-08's
        "paused during scan".  Blocking on the gate instead would queue behind
        a scan that can legitimately run for minutes and then enter SANE at
        some arbitrary later moment, which is why ``ScanWorker.scanner_gate``
        documents the non-blocking attempt as the only permitted move.

        A failing ``run_checks`` is logged and stores nothing, which leaves the
        previous entry in the cache.  That is the whole point of last-known-good:
        a probe that could not be taken must not blank a strip that was correct
        thirty seconds ago.  ``run_checks`` catches its own per-check failures,
        so reaching this handler means the registry itself broke.
        """
        now = self._clock()
        with self._watch_lock:
            last_watched = self._last_watched
        if last_watched is None or now - last_watched > WATCH_WINDOW_SECONDS:
            return
        if self._cache.is_fresh():
            return

        gate = self._scanner_gate()
        acquired = gate.acquire(blocking=False)
        try:
            context = replace(self.build_context(), skip_scanner=not acquired)
            results = run_checks(context)
        except Exception:
            # No exception text goes anywhere near the cache or the page; the
            # strip keeps showing the developer-authored rows it already had.
            logger.exception("Check refresh failed; keeping the previous results")
        else:
            self._cache.store(results)
        finally:
            if acquired:
                gate.release()
