"""The lazy background thread that keeps the status strip's cache warm."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import replace
from enum import StrEnum
from typing import TYPE_CHECKING, Final

from saneless.checks import run_checks
from saneless.worker import STOP_JOIN_SECONDS

if TYPE_CHECKING:
    from collections.abc import Callable

    from saneless.checks import CheckContext

    from .checks_cache import CheckCache

__all__ = ["TICK_SECONDS", "WATCH_WINDOW_SECONDS", "CheckRefresher", "ManualProbe"]

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


class ManualProbe(StrEnum):
    """
    What a requested probe came to within the caller's wait.

    The route reads it for one decision: whether the clicker's manual-refresh
    claim was spent.  A collapsed request spent nothing and gets it back; a
    done or pending one keeps it, because a probe ran or is running for it.
    """

    # A probe already held the lock when the request arrived, so nothing was
    # asked for: that probe's store is imminent and is the answer.
    COLLAPSED = "collapsed"
    # A probe that started after the request finished inside the wait.
    DONE = "done"
    # The wait ended first, or the refresher is stopping.  The request stays
    # outstanding until a probe serves it, and the strip asks again meanwhile.
    PENDING = "pending"


class _StopEvent(threading.Event):
    """
    The refresher's stop event, which a signal handler can also set, lock-free.

    ``threading.Event.set`` holds the Event's own lock, which is not
    reentrant.  ``serve``'s signal handlers run on the main thread, and the
    lifespan stops the refresher on that same thread.  A handler that called
    ``set`` while the call it interrupted was inside one would wait for ever
    on that frame's lock, and the server would hang until it was killed.

    :meth:`notice` is a single attribute store instead, as in
    ``pipeline.Settled``, so a signal that lands anywhere, including inside a
    ``set``, is safe.  :meth:`is_set` reports a notice as well as a ``set``.
    That is all a notice reaches, and it is enough: every reader of this
    event asks ``is_set``.  The idle loop and a waiting request sleep on the
    refresher's condition, not on this event, and every probe polls its
    abort.  So a probe in flight sees a notice at its next poll, and the run
    starts no check after it.  :meth:`wait` would not see a notice, and
    nothing calls it.  A notice cannot wake a sleeper either, because that
    takes the condition's lock.  The idle loop sees it within
    ``TICK_SECONDS`` instead, and turns it into a full stop on its own thread.
    """

    def __init__(self) -> None:
        """Start clear: no stop has been asked for or noticed."""
        super().__init__()
        self._noticed = False

    def notice(self) -> None:
        """Record a stop, taking no lock."""
        self._noticed = True

    def is_set(self) -> bool:
        """
        Say whether a stop was noticed or set.

        Returns:
            Whether :meth:`notice` or ``set`` has been called.

        """
        return self._noticed or super().is_set()


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
    idle sleep that stopping wakes at once rather than a blocking one, a
    per-tick ``try``/``except Exception`` so nothing but stopping
    ends the loop, and a join bounded by the same
    ``STOP_JOIN_SECONDS`` imported from that module rather than redefined --
    or, when the lifespan brings both threads down against one deadline, by
    whatever is left of it.

    Every dependency is injected, and the context arrives from a factory rather
    than from the app's services, so the policy can be exercised by calling
    :meth:`_tick` directly with no thread, no application and no network.

    Args:
        cache: The cache this fills.
        context_factory: Builds the ``CheckContext`` for one refresh.
        scanner_gate: Returns the scanner gate a probe takes.  The worker
            creates its gate once, so every call returns the same lock; this
            is a callable only so it is injected the same way as
            ``scan_active``.
        scan_active: Reports whether the worker has a job in flight, read on
            every tick because the answer changes as jobs start and end.  This, and
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
        # note_stop() only notices it, which wakes nobody; see _StopEvent.
        self._stopping = _StopEvent()
        # Guards _requested and _completed, and is what a waiting request and
        # the idle loop both sleep on.  It is held only to read or move a
        # counter, never across a probe, so a request thread never waits
        # behind run_checks for it.
        self._changed = threading.Condition()
        # Generations of requested probes: how many have been asked for, and
        # the highest one a finished probe served.  A probe serves every
        # request made before it started, so the gap between the two is the
        # work still owed.
        self._requested = 0
        self._completed = 0
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

        Every probe builds its context here, whether a tick or a request asked
        for it, so the two cannot drift into different contexts.

        It builds a context and nothing else: no probe, no cache write and no
        lock, so calling it from any thread costs nothing.

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

        Setting the event and waking whoever sleeps on the condition is the
        whole of it, and the condition is never held across a probe, so this
        never waits on one.  Calling it on a refresher that was never started
        -- a lifespan that failed during startup -- is safe.  It does take the
        condition's lock and the event's, though, and the event's is not
        reentrant.  So a signal handler must not call it: it runs on the main
        thread, and the main thread may be inside this very call when the
        signal lands.  A signal handler calls :meth:`note_stop` instead.

        The same event is the abort every probe runs under, so it also stops
        a probe in flight, and no check after it runs.  A scanner listing ends
        within a fraction of a second, its child killed and reaped by the
        refresher thread that started it.  A saned pre-probe stops waiting
        for saned's reply within a fraction of a second too, and a connect in
        progress within what is left of ``saned_probe.PROBE_CONNECT_SECONDS``.  Without
        that, a stop would wait out a listing for up to its whole deadline,
        or a silent saned host for its whole handshake budget, far past the
        few seconds an idle server is given to shut down.  This thread never
        signals the child itself; it only sets the event.

        Two waits cannot be cut short, and a stop that lands in one waits
        for it.  A name lookup takes no timeout at all.  The Paperless check
        is one HTTP request of up to ``saned_probe.PROBE_CONNECT_SECONDS`` plus
        ``saned_probe.PROBE_READ_SECONDS``, longer than the lifespan's shared join.
        ``serve`` therefore calls :meth:`note_stop` as soon as the server is
        told to stop, before uvicorn waits for the requests still being
        answered, and the lifespan calls this afterwards.  The run then ends
        after the check in flight instead of going on to the next, and that
        check has the request drain as well as the join to end in.  An idle
        server's drain is short, though, so a stop early in a Paperless check,
        or in a slow name lookup, can still find the thread running when the
        join ends, and the lifespan then closes nothing.

        A request waiting in :meth:`request_probe` is woken as well and told
        its probe is still pending, so no request thread waits out its whole
        wait on a refresher that is going away.
        """
        with self._changed:
            self._stopping.set()
            self._changed.notify_all()

    def note_stop(self) -> None:
        """
        Record that the server has been told to stop, taking no lock.

        This is the stop a signal handler gives.  It is one attribute store,
        so it is safe wherever the signal lands, including inside the
        lifespan's own :meth:`request_stop` on the same thread.  It is seen
        everywhere :meth:`request_stop`'s event is read.  A probe in flight
        sees it at its next poll, so a scanner listing or a saned probe is
        aborted as by :meth:`request_stop`, and the run starts no check after
        the one in flight.  The refresher's run then ends.

        It wakes nobody, because waking takes the condition's lock.  An idle
        refresher sees it within ``TICK_SECONDS``, and turns it into a full
        :meth:`request_stop` on its own thread, which a signal never runs on.
        That wakes any request still waiting.  The lifespan still calls
        :meth:`request_stop` and joins the thread, so nothing closes under it.
        Calling this more than once, or on a refresher never started, is safe.
        """
        self._stopping.notice()

    def stop(self, timeout: float | None = None) -> bool:
        """
        Stop the refresher and report whether the thread actually stopped.

        The event is set first, so a loop already awake finishes its tick and
        then exits rather than starting another, and a probe in flight is
        aborted as :meth:`request_stop` describes, so the thread is normally
        gone within about a second even mid-listing or waiting on a silent
        saned host.  It is not when the stop lands in the Paperless check's
        request or in a name lookup, which :meth:`request_stop` cannot cut
        short, and which ``serve``'s early :meth:`note_stop` gives only the
        request drain more to end in.  The join is bounded by the worker's
        ``STOP_JOIN_SECONDS``, or by the caller's remaining share of it: the
        lifespan takes one deadline for both threads, so what the worker's
        join already spent is not spent again here.  When this returns
        ``False`` the thread is still inside a probe and may still write to
        the cache, so the lifespan must leave the Paperless client and the
        scanner open.  Calling it again, or on a refresher never started, is
        safe -- including after :meth:`request_stop` or :meth:`note_stop`,
        which set the same event.

        Args:
            timeout: Seconds to wait for the thread, or ``None`` for the whole
                of ``STOP_JOIN_SECONDS``.  A negative value is clamped to
                zero, so a caller arriving with its budget already spent gets
                a single check of the thread rather than any wait.

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

        A ``Condition.wait_for`` is the idle sleep here, and a blocking sleep
        is never used: a stop or a request notifies the condition and returns
        from the wait immediately, so ``stop()`` wakes the thread at once
        instead of after a full tick, and Check again's probe starts at once
        instead of on the next one.  That is the same property
        ``queue.shutdown(immediate=True)`` gives the worker's blocked ``get()``.

        A requested probe bypasses :meth:`_tick`'s two guards: the TTL would
        turn the click away while the cache is fresh, which is exactly when
        somebody who has just plugged the scanner back in presses the button,
        and somebody pressing a button is by definition somebody watching.
        Only this thread calls :meth:`_probe_and_store`, so the request cannot
        collapse here.
        """
        while True:
            with self._changed:
                self._changed.wait_for(self._woken, TICK_SECONDS)
                requested = self._requested > self._completed
            if self._stopping.is_set():
                # A stop note_stop() only noticed has woken nobody.  Made a
                # full stop here, on a thread no signal handler runs on, it
                # wakes a request still waiting for a probe that will not run.
                self.request_stop()
                return
            try:
                if requested:
                    _ = self._probe_and_store()
                else:
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

    def _woken(self) -> bool:
        """
        Say whether the idle loop has something to do before its next tick.

        Called by ``wait_for`` with the condition held.

        Returns:
            Whether a stop was asked for or a requested probe is still owed.

        """
        return self._stopping.is_set() or self._requested > self._completed

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
        and the only thing it decides is whether the page asks once more.  A
        stale ``True`` costs one extra cache read.  A stale ``False`` is
        harmless only because ``_checks_context`` reads this before it reads
        the cache: a probe that stored before the flag went false has stored
        before the cache read too, so that same body carries its results.  Read
        the other way round, a probe landing between the two reads leaves a
        body with the old rows and no reason to ask again.

        A requested probe that has not finished counts as in flight too, from
        the moment it is asked for, so a request answered as pending leaves a
        page that keeps asking.  The counters are read without the condition:
        each read is atomic, and a stale answer is harmless for the same
        reason as above, because ``_completed`` moves only after the store.

        Returns:
            True while a probe holds the single-flight lock, or a requested
            probe has not yet completed.

        """
        return self._probe_lock.locked() or self._requested > self._completed

    def request_probe(self, *, wait: float) -> ManualProbe:
        """
        Ask the refresher thread for a probe now, and wait a bounded time for it.

        This is Check again's path.  The probe runs on the refresher thread and
        never on the caller's: libsane is not reentrant, a probe can sit in a
        ``getaddrinfo`` for minutes, and a request thread still inside one
        would keep the server alive past its shutdown budget.  So the caller
        only signals, and waits at most ``wait`` seconds for the answer.

        It also keeps the route and the refresher thread from drifting into
        two probe implementations.  They once had, and three separate bugs came
        from the same block existing twice.  The only probe is
        :meth:`_probe_and_store`, and only the refresher thread calls it.

        A probe that *starts* after the request serves it, so a due tick that
        wins the race answers the request too, and two requests made before a
        probe starts share it.  The condition is held only to move the
        counters and is released while waiting, and never while a probe runs.

        Args:
            wait: The most seconds to wait for the probe to finish.  A probe
                still running when it ends goes on running, and stores what
                it finds for the strip to collect.

        Returns:
            ``COLLAPSED`` at once when a probe already holds the lock: its
            store is imminent and nothing was asked for.  ``DONE`` when a
            probe that started after the request finished inside the wait,
            whether or not it stored.  ``PENDING`` when the wait ended first,
            or the refresher is stopping and will serve nothing more.

        """
        with self._changed:
            if self._stopping.is_set():
                return ManualProbe.PENDING
            if self._probe_lock.locked():
                return ManualProbe.COLLAPSED
            self._requested += 1
            ticket = self._requested
            self._changed.notify_all()
            self._changed.wait_for(
                lambda: self._completed >= ticket or self._stopping.is_set(),
                max(0.0, wait),
            )
            done = self._completed >= ticket
        return ManualProbe.DONE if done else ManualProbe.PENDING

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

        The probe runs under the refresher's own stop Event, as the context's
        ``abort``: a stop ends a scanner listing in flight and the run with it,
        and a run that ended that way, or that finished after the stop was
        asked for, stores nothing, for the same last-known-good reason.

        The probe serves every request made before it took the lock, and says
        so once it has finished, whatever it came to -- stored, failed or
        aborted -- so no waiting request is left waiting for a probe that has
        already run.  The lock is released first, so a request answered here
        renders with no probe in flight.

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
        # Read after the lock is taken: a request that found the lock free has
        # already counted itself by the time the condition is ours.
        with self._changed:
            serving = self._requested
        try:
            context = replace(
                self.build_context(),
                skip_scanner=self._scan_active(),
                abort=self._stopping,
            )
            results = run_checks(context, scanner_gate=self._scanner_gate())
            # A run the stop cut short holds only the checks that finished.
            # The previous entry is kept rather than replaced by part of one.
            if not self._stopping.is_set():
                self._cache.store(results)
        except Exception:
            # No exception text goes anywhere near the cache or the page; the
            # strip keeps showing the developer-authored rows it already had.
            logger.exception("Check refresh failed; keeping the previous results")
        finally:
            self._probe_lock.release()
            with self._changed:
                self._completed = max(self._completed, serving)
                self._changed.notify_all()
        return True

    def _tick(self) -> None:
        """
        Refresh the cache if a refresh is due, and do nothing otherwise.

        The refresh *policy* lives here and only here, and it is a plain
        synchronous method so the tests can call it with no thread running.
        The probe itself lives in :meth:`_probe_and_store`, which a Check again
        request reaches through :meth:`request_probe` and the refresher
        thread's own loop.

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
        # The boolean is dropped deliberately.  A tick has nobody waiting on
        # it and will come round again on the next one, and a request it
        # happens to serve hears so through the generation counters.
        _ = self._probe_and_store()
