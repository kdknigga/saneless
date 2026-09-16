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

import threading
from typing import TYPE_CHECKING

import pytest
from saneless.web.refresher import WATCH_WINDOW_SECONDS, CheckRefresher

from saneless.checks import CheckContext, CheckKey, CheckResult, CheckState
from saneless.vocabulary import ProfileStorage
from saneless.web import refresher as refresher_module
from saneless.web.checks_cache import CheckCache
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
        self.results = _results()
        self._error = error

    def __call__(self, context: CheckContext) -> tuple[CheckResult, ...]:
        """Record the context and either raise or return canned results."""
        self.calls.append(context)
        if self._error is not None:
            raise self._error
        return self.results


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
) -> tuple[CheckRefresher, CheckCache]:
    """Assemble a refresher over a cache that shares the same fake clock."""
    cache = CheckCache(clock=clock)
    refresher = CheckRefresher(
        cache=cache,
        context_factory=lambda: _context(settings),
        scanner_gate=lambda: gate,
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


def test_tick_skips_the_scanner_while_the_gate_is_held(
    default_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scan in flight means skip_scanner, never a second caller in SANE (D-08)."""
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
    assert spy.calls[0].skip_scanner is True


def test_tick_takes_and_releases_a_free_gate(
    default_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no scan running the scanner is checked and the gate handed back."""
    spy = _spy(monkeypatch)
    gate = threading.Lock()
    refresher, _cache = _build(default_settings, _FakeClock(), gate)
    refresher.note_watcher()
    refresher._tick()
    assert spy.calls[0].skip_scanner is False
    assert gate.acquire(blocking=False) is True
    gate.release()


def test_tick_releases_the_gate_when_the_checks_raise(
    default_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing probe must not leave the worker's scanner gate held."""
    _spy(monkeypatch, error=RuntimeError("probe exploded"))
    gate = threading.Lock()
    refresher, _cache = _build(default_settings, _FakeClock(), gate)
    refresher.note_watcher()
    refresher._tick()
    assert gate.acquire(blocking=False) is True
    gate.release()


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
    )
    started_refreshers.append(refresher)
    refresher.start()
    assert refresher.stop() is True
    refresher._thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
    assert refresher._thread.is_alive() is False
