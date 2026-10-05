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

# How long after the last page load the refresher keeps probing: three TTL
# cycles, so a reload soon after still finds warm results, without an appliance
# nobody is looking at probing for ever.  Read at call time, so tests can shorten it.
WATCH_WINDOW_SECONDS: Final = 90.0

# The idle loop's wait granularity, and so how soon it sees a noticed stop or a
# due refresh.  Read at call time.
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

    ``threading.Event.set`` takes the Event's non-reentrant lock, and the
    signal handlers run on the main thread, where the lifespan may be inside
    ``set``; a handler calling it there would hang.  :meth:`notice` is a single
    attribute store instead, as in ``pipeline.Settled``, and :meth:`is_set`
    reports it; :meth:`wait` would not see it, so nothing calls that.  A
    notice wakes no sleeper, so the idle loop sees it within
    ``TICK_SECONDS`` and turns it into a full stop on its own thread.
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

    A page render never probes: an unplugged scanner host is a TCP connect
    that hangs until the OS gives up, so the page reads a cache this thread
    fills while someone has loaded a page within ``WATCH_WINDOW_SECONDS``.
    See docs/explanation/decisions/0011-lazy-bounded-health-strip.md.

    Every dependency is injected, so the policy can be exercised by calling
    :meth:`_tick` directly with no thread, no application and no network.

    Args:
        cache: The cache this fills.
        context_factory: Builds the ``CheckContext`` for one refresh.
        scanner_gate: Returns the scanner gate a probe takes.  The worker
            creates its gate once, so every call returns the same lock; this
            is a callable only so it is injected the same way as
            ``scan_active``.
        scan_active: Reports whether the worker has a job in flight, read on
            every tick because the answer changes as jobs start and end.  This,
            not a failed acquire on the gate, decides whether the scanner
            check is skipped.
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
        # Admits one checker at a time to _probe_and_store, so a second
        # checker never contends for the scanner gate, where it would read as
        # a running scan.  Always taken without blocking.
        self._probe_lock = threading.Lock()
        self._stopping = _StopEvent()
        # Guards _requested and _completed; never held across a probe, so a
        # request thread never waits behind run_checks.
        self._changed = threading.Condition()
        # A probe serves every request made before it started, so the gap
        # between the two counters is the work still owed.
        self._requested = 0
        self._completed = 0
        # Guards _last_watched and nothing else.
        self._watch_lock = threading.Lock()
        # None, not 0.0: time.monotonic() on Linux counts from boot, so 0.0
        # would sit inside the watch window just after boot.
        self._last_watched: float | None = None

    def start(self) -> None:
        """Start the refresher thread."""
        self._thread.start()
        logger.info("CheckRefresher started")

    def note_watcher(self) -> None:
        """
        Record that a page is being looked at right now.

        It writes one float and cannot trigger a probe, so no number of page
        loads raises the probe rate above the TTL.
        """
        now = self._clock()
        with self._watch_lock:
            self._last_watched = now

    def build_context(self) -> CheckContext:
        """
        Assemble the dependencies for one probe, exactly as a tick would.

        Every probe, a tick's or a request's, builds its context here, so the
        two cannot drift.  It probes nothing and takes no lock.

        Returns:
            A context with ``skip_scanner`` unset; :meth:`_probe_and_store`
            sets it.

        """
        return self._context_factory()

    def request_stop(self) -> None:
        """
        Ask the refresher to stop, without waiting for the thread.

        The lifespan calls this before the worker's join, so a refresher
        between ticks exits during it; the single deadline the lifespan passes
        to :meth:`stop` is what bounds the total.  A waiting
        :meth:`request_probe` is woken and told its probe is pending.  It takes
        the event's non-reentrant lock, so a signal handler calls
        :meth:`note_stop` instead.

        The event is also every probe's abort, so a scanner listing or a saned
        pre-probe in flight ends within a fraction of a second, and no later
        check runs.  A name lookup and the Paperless request cannot be cut
        short, so ``serve`` calls :meth:`note_stop` as soon as it is told to
        stop, giving the check in flight the request drain as well as the join.
        """
        with self._changed:
            self._stopping.set()
            self._changed.notify_all()

    def note_stop(self) -> None:
        """
        Record that the server has been told to stop, taking no lock.

        This is the stop a signal handler gives: one attribute store, safe
        wherever the signal lands.  A probe in flight sees it at its next poll,
        as with :meth:`request_stop`; an idle refresher sees it within
        ``TICK_SECONDS`` and makes it a full :meth:`request_stop` on its own
        thread.  Calling this more than once, or on a refresher never started,
        is safe.
        """
        self._stopping.notice()

    def stop(self, timeout: float | None = None) -> bool:
        """
        Stop the refresher and report whether the thread actually stopped.

        A probe in flight is aborted as :meth:`request_stop` describes.  When
        this returns ``False`` the thread is still inside a probe and may still
        write to the cache, so the lifespan must leave the Paperless client and
        the scanner open.  Calling it again, or on a refresher never started, is
        safe.

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

        The idle sleep is a ``Condition.wait_for``, so a stop or a request
        wakes the thread at once.  A requested probe bypasses :meth:`_tick`'s
        guards: the TTL would turn the click away while the cache is fresh,
        which is exactly when someone who just plugged the scanner back in
        presses the button.
        """
        while True:
            with self._changed:
                self._changed.wait_for(self._woken, TICK_SECONDS)
                requested = self._requested > self._completed
            if self._stopping.is_set():
                # A noticed stop has woken nobody; made a full stop here, it
                # wakes a request still waiting for a probe that will not run.
                self.request_stop()
                return
            try:
                if requested:
                    _ = self._probe_and_store()
                else:
                    self._tick()
            except Exception:
                # Nothing ends the loop but stopping: a dead refresher is a
                # strip whose last-checked time silently stops moving.
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

        This is a read and never an acquire: a render must not wait behind a
        probe that may sit in ``getaddrinfo`` for minutes.  A stale ``False``
        is harmless only because ``strip_view.checks_context`` reads this before
        the cache, so a probe that stored before the flag went false is in that
        body.  A requested probe counts from the moment it is asked for.

        Returns:
            True while a probe holds the single-flight lock, or a requested
            probe has not yet completed.

        """
        return self._probe_lock.locked() or self._requested > self._completed

    def request_probe(self, *, wait: float) -> ManualProbe:
        """
        Ask the refresher thread for a probe now, and wait a bounded time for it.

        The probe runs on the refresher thread, never the caller's: libsane is
        not reentrant, and a request thread inside a minutes-long
        ``getaddrinfo`` would keep the server alive past its shutdown budget.
        A probe that starts after the request serves it, so two requests made
        before a probe starts share it.

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

        A second checker leaves at once: the first one's ``store`` is
        imminent, and collapsing keeps two checkers off the scanner gate.
        ``skip_scanner`` comes from the worker's record of a job in flight, the
        fact ``strip_view.checks_context`` renders, so the strip's words and
        colour agree; the gate is passed to ``run_checks`` rather than held
        around it, so only the scanner check can park a scan start.

        Last-known-good: a failing ``run_checks``, a raising ``store``, or a run
        the stop aborted stores nothing and leaves the previous entry in place.
        The store sits inside the ``try``, not an ``else`` arm, whose exception
        would escape the handler and end the thread.  Every request made before
        the lock was taken is marked served, whatever the probe came to.

        Returns:
            Whether this call probed, not whether it stored: only the early
            return, where the lock was already held, gives False.

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
            # A run the stop cut short holds only part of the checks.
            if not self._stopping.is_set():
                self._cache.store(results)
        except Exception:
            # No exception text reaches the cache or the page.
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

        The refresh policy lives here only.  Nobody watching, or a fresh
        cache, means no probe, which bounds the probe rate however many page
        loads land.
        """
        now = self._clock()
        with self._watch_lock:
            last_watched = self._last_watched
        if last_watched is None or now - last_watched > WATCH_WINDOW_SECONDS:
            return
        if self._cache.is_fresh():
            return
        _ = self._probe_and_store()
