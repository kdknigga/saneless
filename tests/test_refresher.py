"""
CheckRefresher unit tests.

Every assertion about the refresh *policy* goes through ``_tick()`` called
synchronously with a counting stand-in for ``run_checks`` and a fake clock, so
the policy is tested without a thread at all.  Exactly one test starts the
thread, and it asserts only start, daemon-ness and a bounded stop.

Nothing here waits on the wall clock.  ``pytest-timeout``'s signal method can
fail a test whose thread is wedged, but it cannot reap the thread, so a leaked
refresher would go on probing through every later test in the session.  The
autouse fixture below stops whatever a test started, which is the actual
containment.

Covers requirements: APPL-02.
"""

from __future__ import annotations

import inspect
import threading
from typing import TYPE_CHECKING

import pytest

from saneless.checks import CheckContext, CheckKey, CheckResult, CheckState
from saneless.vocabulary import ProfileStorage
from saneless.web import refresher as refresher_module
from saneless.web.checks_cache import CheckCache
from saneless.web.refresher import WATCH_WINDOW_SECONDS, CheckRefresher
from saneless.worker import STOP_JOIN_SECONDS

if TYPE_CHECKING:
    from collections.abc import Iterator

    from saneless.config import Settings

_JOIN_TIMEOUT_SECONDS = 5.0


class _FakeClock:
    """A monotonic clock the test moves by hand instead of waiting for."""

    def __init__(self, start: float = 1000.0) -> None:
        """Start the clock at ``start`` seconds."""
        self.now = start

    def __call__(self) -> float:
        """Return the current fake monotonic reading."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward, the way a real one would move on its own."""
        self.now += seconds


class _RunChecksSpy:
    """A stand-in for ``run_checks`` that records every context handed to it."""

    def __init__(self, error: Exception | None = None) -> None:
        """Record nothing yet; raise ``error`` on every call when given one."""
        self.calls: list[CheckContext] = []
        self.gates: list[threading.Lock | None] = []
        self.results = _results()
        self._error = error

    def __call__(
        self, context: CheckContext, *, scanner_gate: threading.Lock | None = None
    ) -> tuple[CheckResult, ...]:
        """
        Record the context and the gate, then either raise or return results.

        Args:
            context: The context the refresher assembled.
            scanner_gate: The gate the refresher handed in rather than held.

        Returns:
            The canned results.

        """
        self.calls.append(context)
        self.gates.append(scanner_gate)
        if self._error is not None:
            raise self._error
        return self.results


class _ReentrantRunChecksSpy:
    """A ``run_checks`` stand-in that probes again from inside its own call."""

    def __init__(self, refresher_box: list[CheckRefresher]) -> None:
        """
        Re-enter the probe path of whatever refresher ``refresher_box`` holds.

        A box rather than the refresher itself, because the spy has to exist
        before the refresher that calls it does.

        Args:
            refresher_box: A list the caller fills with the one refresher.

        """
        self.calls: list[CheckContext] = []
        self.results = _results()
        self._box = refresher_box

    def __call__(
        self, context: CheckContext, *, scanner_gate: threading.Lock | None = None
    ) -> tuple[CheckResult, ...]:
        """
        Record the call, probe again from inside it, and return results.

        The nested probe is the whole point: it is a second checker arriving
        while the first is mid-flight, with no thread scheduling luck involved.

        Args:
            context: The context the refresher assembled.
            scanner_gate: The gate the refresher handed in rather than held.

        Returns:
            The canned results.

        """
        self.calls.append(context)
        for refresher in self._box:
            refresher.probe_now()
        return self.results


class _StoreCounter:
    """Counts the cache writes a probe makes, and forwards each one."""

    def __init__(self, cache: CheckCache) -> None:
        """
        Wrap ``cache``'s ``store`` so every write is recorded.

        Args:
            cache: The cache whose writes are being counted.

        """
        self.cache = cache
        self.count = 0

    def __call__(self, results: tuple[CheckResult, ...]) -> None:
        """
        Record the write and let it through.

        Args:
            results: What the probe produced.

        """
        self.count += 1
        CheckCache.store(self.cache, results)


class _ScanState:
    """The worker's job-in-flight fact, as the refresher reads it."""

    def __init__(self, *, active: bool = False) -> None:
        """
        Start with a job in flight or not.

        Args:
            active: Whether the worker has a job in flight.

        """
        self.active = active

    def __call__(self) -> bool:
        """
        Report whether a scan is running right now.

        Returns:
            The current flag.

        """
        return self.active


def _results(message: str = "All good.") -> tuple[CheckResult, ...]:
    """Build a small, distinguishable result tuple."""
    return (
        CheckResult(key=CheckKey.SCANNER, state=CheckState.OK, message=message),
        CheckResult(key=CheckKey.PAPERLESS, state=CheckState.OK, message=message),
    )


def _context(settings: Settings) -> CheckContext:
    """Build the context the refresher's factory hands back."""
    return CheckContext(
        settings=settings,
        scanner=None,
        paperless=None,
        profile_storage=ProfileStorage.PERSISTED,
    )


def _build(
    settings: Settings,
    clock: _FakeClock,
    gate: threading.Lock,
    scan: _ScanState | None = None,
) -> tuple[CheckRefresher, CheckCache]:
    """
    Assemble a refresher over a cache that shares the same fake clock.

    Args:
        settings: The settings the context factory hands back.
        clock: The monotonic source shared by the cache and the refresher.
        gate: The scanner gate the refresher passes to ``run_checks``.
        scan: The worker's job-in-flight fact, idle when omitted.

    Returns:
        The refresher and the cache it fills.

    """
    cache = CheckCache(clock=clock)
    refresher = CheckRefresher(
        cache=cache,
        context_factory=lambda: _context(settings),
        scanner_gate=lambda: gate,
        scan_active=scan if scan is not None else _ScanState(),
        clock=clock,
    )
    return refresher, cache


@pytest.fixture(autouse=True)
def started_refreshers() -> Iterator[list[CheckRefresher]]:
    """Stop every refresher a test started, so no thread outlives its test."""
    created: list[CheckRefresher] = []
    yield created
    for refresher in created:
        refresher.stop()


def _spy(
    monkeypatch: pytest.MonkeyPatch, error: Exception | None = None
) -> _RunChecksSpy:
    """Replace the refresher's ``run_checks`` with a counting stand-in."""
    spy = _RunChecksSpy(error=error)
    monkeypatch.setattr("saneless.web.refresher.run_checks", spy)
    return spy


def test_tick_without_a_watcher_does_nothing(
    default_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An appliance nobody is looking at must not probe at all (D-05)."""
    spy = _spy(monkeypatch)
    refresher, cache = _build(default_settings, _FakeClock(), threading.Lock())
    refresher._tick()
    assert spy.calls == []
    assert cache.current().results is None


def test_tick_with_a_watcher_runs_the_checks_once(
    default_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stamped watcher plus a cold cache is exactly when a probe is due."""
    spy = _spy(monkeypatch)
    refresher, cache = _build(default_settings, _FakeClock(), threading.Lock())
    refresher.note_watcher()
    refresher._tick()
    assert len(spy.calls) == 1
    assert cache.current().results == spy.results


def test_tick_does_not_reprobe_while_the_cache_is_fresh(
    default_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The TTL bounds probes to one per 30 s however often ticks land (T-30-26)."""
    spy = _spy(monkeypatch)
    clock = _FakeClock()
    refresher, _cache = _build(default_settings, clock, threading.Lock())
    refresher.note_watcher()
    refresher._tick()
    for _ in range(5):
        clock.advance(5.0)
        refresher.note_watcher()
        refresher._tick()
    assert len(spy.calls) == 1


def test_tick_reprobes_once_the_ttl_expires(
    default_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A watched, expired cache is refilled on the next tick."""
    spy = _spy(monkeypatch)
    clock = _FakeClock()
    refresher, _cache = _build(default_settings, clock, threading.Lock())
    refresher.note_watcher()
    refresher._tick()
    clock.advance(31.0)
    refresher.note_watcher()
    refresher._tick()
    assert len(spy.calls) == 2


def test_tick_stops_once_the_watch_window_closes(
    default_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probing stops 90 s after the last page load, not eventually (D-05)."""
    spy = _spy(monkeypatch)
    clock = _FakeClock()
    refresher, _cache = _build(default_settings, clock, threading.Lock())
    refresher.note_watcher()
    clock.advance(WATCH_WINDOW_SECONDS + 1.0)
    refresher._tick()
    assert spy.calls == []


def test_note_watcher_moves_the_window(
    default_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tick that did nothing before the stamp does work after it."""
    spy = _spy(monkeypatch)
    refresher, _cache = _build(default_settings, _FakeClock(), threading.Lock())
    refresher._tick()
    assert spy.calls == []
    refresher.note_watcher()
    refresher._tick()
    assert len(spy.calls) == 1


def test_tick_skips_the_scanner_while_a_job_is_in_flight(
    default_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scan in flight means skip_scanner, never a second caller in SANE (D-08)."""
    spy = _spy(monkeypatch)
    gate = threading.Lock()
    refresher, _cache = _build(
        default_settings, _FakeClock(), gate, _ScanState(active=True)
    )
    refresher.note_watcher()
    refresher._tick()
    assert len(spy.calls) == 1
    assert spy.calls[0].skip_scanner is True


def test_a_gate_held_by_another_checker_is_not_a_running_scan(
    default_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    WR-04: checker-versus-checker contention must not read as "a scan is running".

    The gate is held by somebody who is not the worker and no job is in
    flight, which is exactly the shape that put "Not checked while a scan is
    running" beside "Last checked 14:02" on an idle appliance.
    """
    spy = _spy(monkeypatch)
    gate = threading.Lock()
    refresher, _cache = _build(default_settings, _FakeClock(), gate)
    refresher.note_watcher()
    gate.acquire()
    try:
        refresher._tick()
    finally:
        gate.release()
    assert len(spy.calls) == 1
    assert spy.calls[0].skip_scanner is False


def test_tick_hands_the_gate_to_run_checks_instead_of_holding_it(
    default_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WR-03: the registry holds the gate for the one check that needs it."""
    spy = _spy(monkeypatch)
    gate = threading.Lock()
    refresher, _cache = _build(default_settings, _FakeClock(), gate)
    refresher.note_watcher()
    refresher._tick()
    assert spy.gates == [gate]
    assert gate.acquire(blocking=False) is True
    gate.release()


def test_a_failing_probe_releases_the_probe_lock(
    default_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe that raised must not lock every later checker out for ever."""
    _spy(monkeypatch, error=RuntimeError("probe exploded"))
    gate = threading.Lock()
    refresher, _cache = _build(default_settings, _FakeClock(), gate)
    refresher.note_watcher()
    refresher._tick()
    good = _spy(monkeypatch)
    refresher.probe_now()
    assert len(good.calls) == 1


def test_two_overlapping_probes_run_the_checks_once(
    default_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Single flight: a second checker arriving mid-probe costs nothing.

    The second probe is issued from *inside* the first one's ``run_checks``,
    so the overlap is a fact of the call stack rather than of thread
    scheduling luck.
    """
    box: list[CheckRefresher] = []
    spy = _ReentrantRunChecksSpy(box)
    monkeypatch.setattr("saneless.web.refresher.run_checks", spy)
    refresher, _cache = _build(default_settings, _FakeClock(), threading.Lock())
    box.append(refresher)
    refresher.note_watcher()
    refresher._tick()
    assert len(spy.calls) == 1


def test_the_second_of_two_overlapping_probes_stores_nothing(
    default_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One probe means one cache write, however many checkers wanted it."""
    box: list[CheckRefresher] = []
    spy = _ReentrantRunChecksSpy(box)
    monkeypatch.setattr("saneless.web.refresher.run_checks", spy)
    refresher, cache = _build(default_settings, _FakeClock(), threading.Lock())
    box.append(refresher)
    counter = _StoreCounter(cache)
    monkeypatch.setattr(cache, "store", counter)
    refresher.note_watcher()
    refresher._tick()
    assert counter.count == 1


def test_probe_now_ignores_a_fresh_cache(
    default_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-09: an explicit click does not wait out the TTL."""
    spy = _spy(monkeypatch)
    refresher, _cache = _build(default_settings, _FakeClock(), threading.Lock())
    refresher.note_watcher()
    refresher._tick()
    refresher.probe_now()
    assert len(spy.calls) == 2


def test_probe_now_ignores_a_closed_watch_window(
    default_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Somebody pressing the button is, by definition, somebody watching."""
    spy = _spy(monkeypatch)
    clock = _FakeClock()
    refresher, _cache = _build(default_settings, clock, threading.Lock())
    clock.advance(WATCH_WINDOW_SECONDS + 1.0)
    refresher.probe_now()
    assert len(spy.calls) == 1


def test_probe_now_stores_what_the_checks_returned(
    default_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The button's probe fills the same cache the thread's probe fills."""
    spy = _spy(monkeypatch)
    refresher, cache = _build(default_settings, _FakeClock(), threading.Lock())
    refresher.probe_now()
    assert cache.current().results == spy.results


def test_a_failing_probe_now_leaves_the_previous_entry_in_place(
    default_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Last-known-good is still last-known-good on the button's path."""
    good = _spy(monkeypatch)
    refresher, cache = _build(default_settings, _FakeClock(), threading.Lock())
    refresher.probe_now()
    _spy(monkeypatch, error=RuntimeError("probe exploded"))
    refresher.probe_now()
    assert cache.current().results == good.results


def test_probe_now_skips_the_scanner_while_a_job_is_in_flight(
    default_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit click does not get to defeat the exclusive-scanner rule."""
    spy = _spy(monkeypatch)
    scan = _ScanState(active=True)
    refresher, _cache = _build(default_settings, _FakeClock(), threading.Lock(), scan)
    refresher.probe_now()
    assert spy.calls[0].skip_scanner is True


def test_the_scan_fact_is_read_at_probe_time(
    default_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job that ends between probes unskips the scanner on the next one."""
    spy = _spy(monkeypatch)
    scan = _ScanState(active=True)
    refresher, _cache = _build(default_settings, _FakeClock(), threading.Lock(), scan)
    refresher.probe_now()
    scan.active = False
    refresher.probe_now()
    assert [call.skip_scanner for call in spy.calls] == [True, False]


def test_the_refresher_takes_five_injected_dependencies(
    default_settings: Settings,
) -> None:
    """
    ``PLR0913`` caps ``__init__`` at five non-self parameters, and this is five.

    Args:
        default_settings: The shared settings fixture.

    """
    refresher, _cache = _build(default_settings, _FakeClock(), threading.Lock())
    parameters = inspect.signature(type(refresher).__init__).parameters
    assert len(parameters) == 6


def test_a_failing_run_leaves_the_previous_entry_in_place(
    default_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Last-known-good survives a failed refresh, which is its whole point."""
    good = _spy(monkeypatch)
    clock = _FakeClock()
    refresher, cache = _build(default_settings, clock, threading.Lock())
    refresher.note_watcher()
    refresher._tick()
    previous = cache.current().results

    _spy(monkeypatch, error=RuntimeError("probe exploded"))
    clock.advance(31.0)
    refresher.note_watcher()
    refresher._tick()

    assert cache.current().results == previous
    assert previous == good.results


def test_start_then_stop_reports_a_stopped_daemon_thread(
    default_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    started_refreshers: list[CheckRefresher],
) -> None:
    """The thread is a daemon and stop() wakes it at once and says so (D-07)."""
    _spy(monkeypatch)
    cache = CheckCache()
    gate = threading.Lock()
    refresher = CheckRefresher(
        cache=cache,
        context_factory=lambda: _context(default_settings),
        scanner_gate=lambda: gate,
        scan_active=_ScanState(),
    )
    started_refreshers.append(refresher)
    refresher.start()
    assert refresher._thread.daemon is True
    assert refresher._thread.is_alive() is True
    assert refresher.stop() is True
    assert refresher._thread.is_alive() is False


def test_stop_on_a_refresher_that_never_started_returns_true(
    default_settings: Settings,
) -> None:
    """Shutdown must not care whether startup got as far as start() (D-07)."""
    refresher, _cache = _build(default_settings, _FakeClock(), threading.Lock())
    assert refresher.stop() is True


def test_request_stop_signals_without_joining(
    default_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    started_refreshers: list[CheckRefresher],
) -> None:
    """
    The signal half of stop(), so a caller can set two events then join two.

    The lifespan owns two sequential joins, so the early signal is not what
    bounds the total -- the shared deadline it passes to ``stop(timeout=...)``
    is (WR-07).  Signalling first is still worth doing: a refresher merely
    between ticks wakes during the worker's join and exits for free, so its
    own join is skipped entirely.
    """
    _spy(monkeypatch)
    cache = CheckCache()
    gate = threading.Lock()
    refresher = CheckRefresher(
        cache=cache,
        context_factory=lambda: _context(default_settings),
        scanner_gate=lambda: gate,
        scan_active=_ScanState(),
    )
    started_refreshers.append(refresher)
    refresher.start()
    refresher.request_stop()
    assert refresher._stopping.is_set() is True
    assert refresher.stop() is True


def test_request_stop_on_a_refresher_that_never_started_is_safe(
    default_settings: Settings,
) -> None:
    """A lifespan that failed before start() must still be able to signal."""
    refresher, _cache = _build(default_settings, _FakeClock(), threading.Lock())
    refresher.request_stop()
    assert refresher._stopping.is_set() is True
    assert refresher.stop() is True


def test_stop_is_safe_to_call_twice(
    default_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    started_refreshers: list[CheckRefresher],
) -> None:
    """A second stop() neither raises nor changes the answer."""
    _spy(monkeypatch)
    cache = CheckCache()
    gate = threading.Lock()
    refresher = CheckRefresher(
        cache=cache,
        context_factory=lambda: _context(default_settings),
        scanner_gate=lambda: gate,
        scan_active=_ScanState(),
    )
    started_refreshers.append(refresher)
    refresher.start()
    refresher._thread.join(timeout=0.0)
    assert refresher.stop() is True
    assert refresher.stop() is True
    assert refresher._thread.is_alive() is False


def test_stop_joins_within_the_shared_bound(
    default_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    started_refreshers: list[CheckRefresher],
) -> None:
    """The join bound is imported from the worker, not redefined here."""
    assert refresher_module.STOP_JOIN_SECONDS is STOP_JOIN_SECONDS

    _spy(monkeypatch)
    cache = CheckCache()
    gate = threading.Lock()
    refresher = CheckRefresher(
        cache=cache,
        context_factory=lambda: _context(default_settings),
        scanner_gate=lambda: gate,
        scan_active=_ScanState(),
    )
    started_refreshers.append(refresher)
    refresher.start()
    assert refresher.stop() is True
    refresher._thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
    assert refresher._thread.is_alive() is False


class _RecordingThread:
    """
    A stand-in for the refresher's thread that records the bound it was joined with.

    The point of ``stop(timeout=...)`` is the *value* handed to ``join``, not
    how long anything actually takes, so this reports liveness the way a test
    dictates and remembers every timeout it was given.  No real thread has to
    be slow, and nothing here waits on the wall clock.
    """

    def __init__(self, *, alive: bool, alive_after_join: bool = False) -> None:
        """Report ``alive`` until joined, then ``alive_after_join``."""
        self.join_timeouts: list[float | None] = []
        self._alive = alive
        self._alive_after_join = alive_after_join
        self._joined = False

    def is_alive(self) -> bool:
        """Whether this pretend thread is still running."""
        return self._alive_after_join if self._joined else self._alive

    def join(self, timeout: float | None = None) -> None:
        """Record the bound the caller asked for, and return at once."""
        self.join_timeouts.append(timeout)
        self._joined = True


def _with_thread(
    monkeypatch: pytest.MonkeyPatch,
    default_settings: Settings,
    thread: _RecordingThread,
) -> CheckRefresher:
    """Build an unstarted refresher whose thread is the recording double."""
    refresher, _cache = _build(default_settings, _FakeClock(), threading.Lock())
    monkeypatch.setattr(refresher, "_thread", thread)
    return refresher


def test_stop_without_a_timeout_joins_with_the_shared_bound(
    default_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default is still the worker's bound, so every existing caller is unmoved."""
    thread = _RecordingThread(alive=True, alive_after_join=True)
    refresher = _with_thread(monkeypatch, default_settings, thread)
    assert refresher.stop() is False
    assert thread.join_timeouts == [STOP_JOIN_SECONDS]


def test_stop_passes_the_callers_bound_to_the_join(
    default_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller part-way through a shared deadline joins for what is left of it."""
    thread = _RecordingThread(alive=True, alive_after_join=False)
    refresher = _with_thread(monkeypatch, default_settings, thread)
    assert refresher.stop(timeout=2.5) is True
    assert thread.join_timeouts == [2.5]


def test_stop_with_a_spent_budget_polls_instead_of_waiting(
    default_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A caller whose budget is gone gets a poll and a ``False``, not another wait.

    WR-07: this is the case the shared deadline exists for -- a worker that ate
    the whole bound must not hand the refresher a fresh one.
    """
    thread = _RecordingThread(alive=True, alive_after_join=True)
    refresher = _with_thread(monkeypatch, default_settings, thread)
    assert refresher.stop(timeout=0.0) is False
    assert thread.join_timeouts == [0.0]


def test_stop_clamps_a_negative_timeout_to_zero(
    default_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A negative bound must never become ``join(None)``, an unbounded wait."""
    thread = _RecordingThread(alive=True, alive_after_join=True)
    refresher = _with_thread(monkeypatch, default_settings, thread)
    assert refresher.stop(timeout=-1.0) is False
    assert thread.join_timeouts == [0.0]


def test_stop_with_a_timeout_does_not_join_a_finished_thread(
    default_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A thread that already exited costs the caller none of its budget."""
    thread = _RecordingThread(alive=False)
    refresher = _with_thread(monkeypatch, default_settings, thread)
    assert refresher.stop(timeout=1.0) is True
    assert thread.join_timeouts == []


def test_probe_now_reports_that_it_probed(
    default_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route has to tell a probe from a collapse, so the probe says which."""
    _spy(monkeypatch)
    refresher, _cache = _build(default_settings, _FakeClock(), threading.Lock())
    assert refresher.probe_now() is True


def test_probe_now_reports_a_collapse_while_another_checker_holds_the_lock(
    default_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    WR-03: a collapsed click must be visible to the handler that made it.

    The lock is taken by the test rather than by a thread, which is the same
    idiom ``tests/test_web_checks.py::_RecordingRefresher.probe_lock`` exists
    for: the overlap is a fact of the call, with no scheduling luck in it.
    """
    spy = _spy(monkeypatch)
    refresher, cache = _build(default_settings, _FakeClock(), threading.Lock())
    assert refresher._probe_lock.acquire(blocking=False) is True
    try:
        assert refresher.probe_now() is False
    finally:
        refresher._probe_lock.release()
    assert spy.calls == []
    assert cache.current().results is None


def test_a_probe_that_raised_still_reports_that_it_probed(
    default_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    "Did it probe" is "did it get the lock", never "did it store".

    A caller that read a raising probe as a collapse would ask the page to
    wait for a result that is never coming.
    """
    spy = _spy(monkeypatch, error=RuntimeError("probe exploded"))
    refresher, cache = _build(default_settings, _FakeClock(), threading.Lock())
    assert refresher.probe_now() is True
    assert len(spy.calls) == 1
    assert cache.current().results is None


def test_a_tick_still_returns_nothing(
    default_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The thread's contract is unchanged; only the button's caller reads it."""
    _spy(monkeypatch)
    refresher, cache = _build(default_settings, _FakeClock(), threading.Lock())
    refresher.note_watcher()
    assert refresher._tick() is None
    assert cache.current().results is not None


def test_probe_in_flight_is_false_on_an_idle_refresher(
    default_settings: Settings,
) -> None:
    """Nothing is in flight before anybody probes, so the page need not ask."""
    refresher, _cache = _build(default_settings, _FakeClock(), threading.Lock())
    assert refresher.probe_in_flight is False


def test_probe_in_flight_is_true_while_a_checker_holds_the_probe_lock(
    default_settings: Settings,
) -> None:
    """The reader's answer is exactly "does somebody own the probe right now"."""
    refresher, _cache = _build(default_settings, _FakeClock(), threading.Lock())
    assert refresher._probe_lock.acquire(blocking=False) is True
    try:
        assert refresher.probe_in_flight is True
    finally:
        refresher._probe_lock.release()
    assert refresher.probe_in_flight is False


def test_probe_in_flight_is_true_inside_the_probe_and_false_after_it(
    default_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The window the settling poll rides on is the probe's own duration.

    Read from inside ``run_checks`` -- which is where the in-flight window
    actually is -- rather than inferred from the lock by the test.
    """
    box: list[CheckRefresher] = []
    seen: list[bool] = []

    def _record(
        context: CheckContext, *, scanner_gate: threading.Lock | None = None
    ) -> tuple[CheckResult, ...]:
        """
        Record the in-flight reading mid-probe and return canned results.

        Args:
            context: The context the refresher assembled.
            scanner_gate: The gate the refresher handed in rather than held.

        Returns:
            The canned results.

        """
        seen.append(box[0].probe_in_flight)
        return _results()

    monkeypatch.setattr("saneless.web.refresher.run_checks", _record)
    refresher, _cache = _build(default_settings, _FakeClock(), threading.Lock())
    box.append(refresher)
    assert refresher.probe_now() is True
    assert seen == [True]
    assert refresher.probe_in_flight is False


def test_reading_probe_in_flight_does_not_take_the_probe_lock(
    default_settings: Settings,
) -> None:
    """
    T-30-29-03: a render observes the lock and never contends for it.

    Two reads on an idle refresher leave the lock free for a probe to take,
    and a read while it is held returns rather than blocking -- a reader that
    acquired would either hang here or strand the lock for the next probe.
    """
    refresher, _cache = _build(default_settings, _FakeClock(), threading.Lock())
    assert refresher.probe_in_flight is False
    assert refresher.probe_in_flight is False
    assert refresher._probe_lock.acquire(blocking=False) is True
    assert refresher.probe_in_flight is True
    refresher._probe_lock.release()
    assert refresher._probe_lock.acquire(blocking=False) is True
    refresher._probe_lock.release()
