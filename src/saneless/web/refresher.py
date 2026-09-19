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

# How long after the last page load the refresher keeps probing.
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

    A page render never probes, and that is the point of this class: an
    unplugged scanner host is a TCP connect that hangs until the OS gives up,
    so the mechanism has to be "the page reads a cache a background thread
    fills", not "the page probes quickly".  This is that thread.

    It follows :class:`~saneless.worker.ScanWorker` in every structural respect
    -- a daemon thread built in ``__init__`` and started separately,
    a ``_stopping`` :class:`threading.Event` set once by :meth:`stop`, an
    ``Event.wait`` idle sleep rather than a blocking one so stopping wakes it
    at once, a per-tick ``try``/``except Exception`` so nothing but stopping
    ends the loop, and a join bounded by the same
    ``STOP_JOIN_SECONDS`` imported from that module rather than redefined --
    or, when the lifespan brings both threads down against one deadline, by
    whatever is left of it.

    Every dependency is injected, and the context arrives from a factory rather
    than from ``app.state``, so the policy can be exercised by calling
    :meth:`_tick` directly with no thread, no application and no network.

    Args:
        cache: The cache this fills.
        context_factory: Builds the ``CheckContext`` for one refresh.
        scanner_gate: Returns the worker's scanner gate at call time, because
            the worker may be rebuilt independently of the refresher.
        scan_active: Reports whether the worker has a job in flight, read at
            call time for the same reason ``scanner_gate`` is a callable -- the
            worker may be rebuilt independently of the refresher.  This, and
            not a failed acquire on the gate, is what decides whether the
            scanner check is skipped.
        clock: The monotonic source the watch window is measured with.

    """

    def __init__(
        self,
        cache: CheckCache,
        context_factory: Callable[[], CheckContext],
        scanner_gate: Callable[[], threading.Lock],
        scan_active: Callable[[], bool],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Build the thread and its synchronisation, without starting it."""
        self._cache = cache
        self._context_factory = context_factory
        self._scanner_gate = scanner_gate
        self._scan_active = scan_active
        self._clock = clock
        self._thread = threading.Thread(target=self._run, daemon=True)
        # Admits one checker at a time to _probe_and_store, and guards nothing
        # else.  Two checkers contending for the *scanner* gate is what made
        # gate contention indistinguishable from a running scan, so the
        # second checker is turned away here instead, before it ever reaches
        # the gate.  Taken without blocking, always, and never held across a
        # cache read.
        self._probe_lock = threading.Lock()
        # Set once by stop(); read by the idle wait, which it wakes at once.
        self._stopping = threading.Event()
        # Guards every read and every write of self._last_watched, and nothing
        # else.  Request threads write it and the refresher thread reads it.
        self._watch_lock = threading.Lock()
        # When a page last said someone was looking, or None for "nobody ever
        # has".  None rather than 0.0 because time.monotonic() on Linux counts
        # from boot: for the first ninety seconds of an appliance's life 0.0
        # would sit *inside* the watch window and the refresher would probe
        # with no watcher at all -- exactly the case the watch window exists to
        # prevent, and exactly when an appliance boots.
        self._last_watched: float | None = None

    def start(self) -> None:
        """Start the refresher thread."""
        self._thread.start()
        logger.info("CheckRefresher started")

    def note_watcher(self) -> None:
        """
        Record that a page is being looked at right now.

        Called from request threads by the index page and the strip routes.
        It writes one float and nothing else: it cannot trigger a probe, so no
        number of page loads raises the probe rate above the TTL.
        The stamp is not persisted and a restart forgets it, which is the whole
        of what "someone is watching" means here.
        """
        now = self._clock()
        with self._watch_lock:
            self._last_watched = now

    def build_context(self) -> CheckContext:
        """
        Assemble the dependencies for one probe, exactly as a tick would.

        Public because the Refresh button probes from a request handler and
        has to bypass the TTL to do it: routing that click through
        :meth:`_tick` would do nothing at all while the cache is still fresh,
        which is exactly when somebody who has just plugged the scanner back in
        presses the button.  Handing the one factory back out is what keeps the
        button's probe and the thread's probe building the *same* context,
        instead of the route assembling a second one that could drift.

        It builds a context and nothing else: no probe, no cache write and no
        lock, so calling it from a request thread costs nothing.

        Returns:
            A context with ``skip_scanner`` unset.  :meth:`_probe_and_store`
            sets it from the worker's own record of a job in flight, which is
            the fact the flag claims.

        """
        return self._context_factory()

    def request_stop(self) -> None:
        """
        Ask the refresher to stop, without waiting for the thread.

        This is the signal half of :meth:`stop`, split out for a caller that
        has more than one thread to bring down.  The lifespan has two, and it
        joins them one after the other, so signalling both before joining
        either does not by itself bound the total -- an earlier version of this
        docstring claimed it did.  What the early signal
        actually buys is that a refresher merely between ticks wakes on the
        event during the worker's join and exits for free, so its own join is
        skipped.  What holds the worst case at one ``STOP_JOIN_SECONDS``
        instead of one per thread is the single deadline the lifespan takes
        before either join and passes to :meth:`stop`.

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
        worker's join already spent is not spent again here.  When this
        returns ``False`` the thread is still inside a probe and may still
        write to the cache, so the lifespan must leave the Paperless client and
        the scanner open.  Calling it again, or on a refresher never
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
        Tick until stopped, and let nothing but stopping end the loop.

        ``Event.wait`` is the idle sleep here, and a blocking sleep is never
        used: setting the event returns from the wait immediately, so
        ``stop()`` wakes the thread at once instead of after a full tick.  That
        is the same property ``queue.shutdown(immediate=True)`` gives the
        worker's blocked ``get()``.
        """
        while not self._stopping.wait(TICK_SECONDS):
            try:
                self._tick()
            except Exception:
                # The backstop ``ScanWorker._run`` puts round each job, for
                # the rule it names there: nothing ends the loop but stopping.
                # Without it one raise anywhere in a tick ends
                # this thread for the life of the process, and a dead
                # refresher is a strip that never updates again and says
                # nothing about it -- the operator sees a last-checked time
                # that simply stops moving.  The ``wait`` above
                # stays outside the guard, so stopping is still the one thing
                # that ends the loop.
                logger.exception("Check refresher tick failed; continuing")

    @property
    def probe_in_flight(self) -> bool:
        """
        Say whether some caller owns the probe right now.

        This is a *read* and never an acquire.  A render is not allowed to
        contend for a lock a probe holds -- the probe can be inside a
        ``getaddrinfo`` that Linux retries for two minutes, and a request
        thread parked behind it is a page that never arrives.  It is the same
        rule ``_checks_context`` already applies to the scanner gate, where it
        reads the worker's job record rather than trying the gate.

        The answer is a snapshot that may be false the instant it is returned,
        and that is fine, because the only thing it decides is whether the page
        asks once more.  A stale ``True`` costs one extra cache read; a stale
        ``False`` costs nothing, because the probe that just finished has
        already stored and the next body carries its results.

        Returns:
            True while a probe holds the single-flight lock.

        """
        return self._probe_lock.locked()

    def probe_now(self) -> bool:
        """
        Probe at once, whatever the TTL and the watch window say.

        This is the Refresh button's path.  It bypasses both guards
        deliberately: ``_tick`` returns early on a fresh cache, which is
        precisely the first thirty seconds after a page load -- exactly when
        somebody who has just plugged the scanner back in presses the button --
        and somebody pressing a button is, by definition, somebody watching.

        It exists so the route and the refresher thread cannot drift into two
        probe implementations.  They had drifted, and three separate bugs came
        from the same block existing twice.  Both callers
        now go through :meth:`_probe_and_store`; neither this nor ``_tick``
        calls the other, which is the discipline ``job.py``'s
        ``TestLockDiscipline`` enforces for public-to-public self-calls.

        Returns:
            True when this call took the probe and ran it, and False when it
            collapsed into one already in flight.  ``False`` tells the one
            caller that reads it -- ``routes.refresh_checks`` -- three things:
            another checker owns the probe, that checker's ``store`` is
            imminent, and this call did nothing at all.  The handler owes the
            page a way to collect the imminent answer, and owes the clicker
            their manual-refresh claim back, because a collapse cost no
            Paperless request, no saned dial and no filesystem write.

        """
        return self._probe_and_store()

    def _probe_and_store(self) -> bool:
        """
        Run one probe and store what it found, or let an in-flight one do it.

        The probe lock is taken without blocking and a second checker simply
        leaves: the first one's ``store`` is imminent, and a duplicate probe
        would buy an identical answer for the price of a Paperless request and
        two filesystem writes.  Collapsing them is also what stops two checkers
        contending for the *scanner* gate, which is the contention that used to
        reach a household member as "a scan is running".

        ``skip_scanner`` comes from the worker's own record of a job in flight,
        the same fact ``_checks_context`` renders as ``scan_active``, so the
        strip's words and its colour cannot disagree.  The scanner gate is
        *passed* to ``run_checks`` rather than held around it, because only the
        scanner check enters libsane and the Paperless budget and the two
        directory writes have no business parking a scan start.

        Residual, stated plainly: a skipped row stored during a scan stays on
        the strip until the next probe, so for up to one TTL after a scan ends
        the strip can still say "not checked while a scan is running" beside an
        idle appliance.  That window is closed in practice by the
        terminal-state reload the status area already issues, and unlike a row
        drawn from gate contention, this one was true when it was written.

        A failing ``run_checks`` is logged and stores nothing, which leaves the
        previous entry in the cache.  That is the whole point of last-known-good:
        a probe that could not be taken must not blank a strip that was correct
        thirty seconds ago.  ``run_checks`` catches its own per-check failures,
        so reaching this handler means the registry itself broke.  The ``store``
        is inside the same guard for the same reason: a write that raises also
        leaves the previous entry in place, and neither failure reaches the
        caller.  The store used to sit in an ``else`` arm, and an exception
        raised in an ``else`` arm is not routed to that ``try``'s handlers --
        which is how it came to be outside the guard it looked like it was
        inside, and how a raising write ended the refresher thread for the life
        of the process.

        Returns:
            "Did this call probe", which is deliberately *not* "did this call
            store".  The ``except`` arm probed and stored nothing, and it
            returns True: a caller that read a raising probe as a collapse
            would ask the page to wait for a result that is never coming.  A
            store that raised is that same case and returns True too -- it
            probed.  The one False is the early return, where the lock was
            already held.

        """
        if not self._probe_lock.acquire(blocking=False):
            return False
        try:
            context = replace(self.build_context(), skip_scanner=self._scan_active())
            results = run_checks(context, scanner_gate=self._scanner_gate())
            self._cache.store(results)
        except Exception:
            # No exception text goes anywhere near the cache or the page; the
            # strip keeps showing the developer-authored rows it already had.
            logger.exception("Check refresh failed; keeping the previous results")
        finally:
            self._probe_lock.release()
        return True

    def _tick(self) -> None:
        """
        Refresh the cache if a refresh is due, and do nothing otherwise.

        The refresh *policy* lives here and only here, and it is a plain
        synchronous method so the tests can call it with no thread running.
        The probe itself lives in :meth:`_probe_and_store`, which the Refresh
        button reaches through :meth:`probe_now`.

        Two guards come first, in this order: nobody is watching, and the
        cache is still fresh.  Either one means no probe, which is
        what bounds the probe rate no matter how many page loads land.
        """
        now = self._clock()
        with self._watch_lock:
            last_watched = self._last_watched
        if last_watched is None or now - last_watched > WATCH_WINDOW_SECONDS:
            return
        if self._cache.is_fresh():
            return
        # The boolean is dropped deliberately.  It exists for the request
        # handler, which owes an answer to a page; the thread has nobody
        # waiting on this tick and will come round again on the next one.
        _ = self._probe_and_store()
