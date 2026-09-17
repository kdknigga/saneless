"""
Lifespan tests: crash recovery at startup and a guarded close at shutdown.

Covers requirements: ROBU-06, ROBU-02.  Code review finding M-03; decisions
D-13 (startup order: validate, recover, prune, start the worker) and D-09
(close the store and the Paperless client only after the worker confirmed it
stopped).

The store is file-backed throughout: ``create_app`` opens
``settings.output.db_path``, so a test seeds rows by opening its own
``JobStore`` on that path, writing, and closing it before the app is built --
exactly the rows a crashed process would leave behind.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount, Route

import saneless.scanner.sane_backend as sane_backend_mod
from saneless.config import (
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
)
from saneless.job import JobStore
from saneless.paperless import UploadResult
from saneless.scanner.sane_backend import SaneBackend
from saneless.vocabulary import (
    RESTART_REASON,
    TERMINAL_STATES,
    JobState,
    WorkerHealth,
)
from saneless.web import app as app_module
from saneless.web import refresher as refresher_module
from saneless.web.app import create_app
from saneless.worker import STOP_JOIN_SECONDS
from tests.conftest import StubScannerBackend, wait_for_state
from tests.fake_sane import FakeSaneModule

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

_APP_LOGGER = "saneless.web.app"

# The status area's opening tag, captured whole so a polling attribute
# elsewhere on the page cannot satisfy or break the assertion.
_STATUS_AREA = re.compile(r'<div id="status-area"(?P<attrs>[^>]*)>')


@dataclass(frozen=True)
class _Seeded:
    """The ids of the rows a crashed process left behind."""

    done: str
    pending: str
    awaiting_flip: str
    scanning: str

    @property
    def active(self) -> tuple[str, str, str]:
        """The three rows that were still in an active state."""
        return (self.pending, self.awaiting_flip, self.scanning)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Build settings whose job store is a file under tmp_path."""
    return Settings(
        scanner=ScannerConfig(device="test:device:001"),
        paperless=PaperlessConfig(url="http://localhost:8000", token="test-token"),
        output=OutputConfig(tmp_dir=str(tmp_path), data_dir=str(tmp_path)),
        profiles={"default": ProfileConfig()},
    )


def _build_app(settings: Settings) -> FastAPI:
    """Build the real app with a stub scanner and no Paperless network calls."""
    app = create_app(settings, StubScannerBackend())
    app.state.paperless.get_tags = list
    app.state.paperless.get_correspondents = list
    return app


def _seed_crashed_store(settings: Settings) -> _Seeded:
    """
    Write the rows a killed process leaves behind, then close the store.

    One finished job and three in-flight ones.  The SCANNING row is created
    last, so it is the newest row and the one the status area falls back to.
    """
    store = JobStore(db_path=str(settings.output.db_path))
    try:
        done = store.create_job("default", "finished before the crash")
        store.finish_job(done.id, JobState.DONE)
        pending = store.create_job("default", "queued when the process died")
        flip = store.create_job("default", "waiting at the flip prompt")
        store.update_state(flip.id, JobState.AWAITING_FLIP)
        scanning = store.create_job("default", "mid-scan when the process died")
        store.update_state(scanning.id, JobState.SCANNING)
    finally:
        store.close()
    return _Seeded(
        done=done.id,
        pending=pending.id,
        awaiting_flip=flip.id,
        scanning=scanning.id,
    )


def _interrupted_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Collect the app logger's WARNING messages about interrupted jobs."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == _APP_LOGGER
        and record.levelno == logging.WARNING
        and "interrupted" in record.getMessage()
    ]


# --- Startup recovery (ROBU-06, D-13) ---------------------------------------


def test_startup_fails_every_active_job_with_the_restart_reason(
    settings: Settings,
) -> None:
    """Rows left active by a crash are ERROR with RESTART_REASON; DONE is kept."""
    seeded = _seed_crashed_store(settings)
    app = _build_app(settings)
    with TestClient(app):
        store: JobStore = app.state.job_store
        for job_id in seeded.active:
            job = store.get_job(job_id)
            assert job is not None
            assert job.state is JobState.ERROR
            assert job.error == RESTART_REASON
        done = store.get_job(seeded.done)
        assert done is not None
        assert done.state is JobState.DONE
        assert done.error is None


def test_recovered_row_is_what_the_status_area_shows(settings: Settings) -> None:
    """The newest recovered row renders as a settled error, not a poll (T9)."""
    _seed_crashed_store(settings)
    app = _build_app(settings)
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert f"&#10007; Error: {RESTART_REASON}" in response.text
    match = _STATUS_AREA.search(response.text)
    assert match is not None
    assert "hx-trigger" not in match["attrs"]
    assert "hx-get" not in match["attrs"]


def test_recovery_runs_before_the_worker_starts(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker never sees an orphan as live work: it starts after recovery."""
    seeded = _seed_crashed_store(settings)
    app = _build_app(settings)
    worker = app.state.worker
    store: JobStore = app.state.job_store
    original_start = worker.start
    seen: list[JobState | None] = []

    def recording_start() -> None:
        job = store.get_job(seeded.scanning)
        seen.append(None if job is None else job.state)
        original_start()

    monkeypatch.setattr(worker, "start", recording_start)
    with TestClient(app):
        pass
    assert seen == [JobState.ERROR]


def test_startup_order_is_recover_then_prune_then_start(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prune runs after crash recovery and before the worker starts (D-13)."""
    app = _build_app(settings)
    worker = app.state.worker
    store: JobStore = app.state.job_store
    calls: list[str] = []
    original_fail = store.fail_active_jobs
    original_prune = store.prune
    original_start = worker.start

    def recording_fail(reason: str) -> int:
        calls.append("fail_active_jobs")
        return original_fail(reason)

    def recording_prune(max_age_days: int, max_rows: int) -> int:
        calls.append("prune")
        return original_prune(max_age_days, max_rows)

    def recording_start() -> None:
        calls.append("start")
        original_start()

    monkeypatch.setattr(store, "fail_active_jobs", recording_fail)
    monkeypatch.setattr(store, "prune", recording_prune)
    monkeypatch.setattr(worker, "start", recording_start)
    with TestClient(app):
        pass
    assert calls == ["fail_active_jobs", "prune", "start"]


def test_startup_warns_with_the_number_of_interrupted_jobs(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """A WARNING counts the jobs recovery failed."""
    _seed_crashed_store(settings)
    app = _build_app(settings)
    caplog.set_level(logging.INFO, logger=_APP_LOGGER)
    with TestClient(app):
        pass
    warnings = _interrupted_warnings(caplog)
    assert len(warnings) == 1
    assert "3" in warnings[0]


def test_startup_with_no_active_jobs_logs_no_recovery_warning(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """A clean store gives recovery nothing to report."""
    app = _build_app(settings)
    caplog.set_level(logging.INFO, logger=_APP_LOGGER)
    with TestClient(app):
        pass
    assert _interrupted_warnings(caplog) == []


def test_prune_failure_never_blocks_startup(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing startup prune is logged with its traceback; the app serves."""
    app = _build_app(settings)
    store: JobStore = app.state.job_store

    def failing_prune(max_age_days: int, max_rows: int) -> int:
        msg = f"disk I/O error pruning {max_age_days}d / {max_rows} rows"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(store, "prune", failing_prune)
    caplog.set_level(logging.INFO, logger=_APP_LOGGER)
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert any(
        record.name == _APP_LOGGER
        and record.levelno == logging.WARNING
        and record.exc_info is not None
        and "prune" in record.getMessage()
        for record in caplog.records
    )


def test_recovery_failure_starts_the_worker_degraded(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A store recovery cannot write still starts, degraded, recovery pending."""
    app = _build_app(settings)
    store: JobStore = app.state.job_store

    def failing_fail_active_jobs(reason: str) -> int:
        msg = f"attempt to write a readonly database ({reason})"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(store, "fail_active_jobs", failing_fail_active_jobs)
    caplog.set_level(logging.INFO, logger=_APP_LOGGER)
    with TestClient(app):
        assert app.state.worker.health is WorkerHealth.DEGRADED
    assert any(
        record.name == _APP_LOGGER
        and record.levelno == logging.ERROR
        and record.exc_info is not None
        and "Crash recovery" in record.getMessage()
        for record in caplog.records
    )


# --- Guarded close at shutdown (ROBU-06, D-07, D-09) -------------------------


def test_shutdown_closes_resources_after_both_threads_stop(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Both threads confirm before anything closes (A-7).

    The refresher holds the same Paperless client the worker does and may be
    inside SANE, so the close sequence is owed *two* confirmed stops, not one.
    """
    scanner = StubScannerBackend()
    app = create_app(settings, scanner)
    app.state.paperless.get_tags = list
    app.state.paperless.get_correspondents = list
    worker = app.state.worker
    refresher = app.state.refresher
    store: JobStore = app.state.job_store
    paperless = app.state.paperless
    calls: list[str] = []
    original_stop = worker.stop
    original_refresher_stop = refresher.stop
    original_paperless_close = paperless.close
    original_store_close = store.close

    def recording_stop() -> bool:
        calls.append("worker.stop")
        return original_stop()

    def recording_refresher_stop(timeout: float | None = None) -> bool:
        calls.append("refresher.stop")
        return original_refresher_stop(timeout=timeout)

    def recording_paperless_close() -> None:
        calls.append("paperless.close")
        original_paperless_close()

    def recording_store_close() -> None:
        calls.append("job_store.close")
        original_store_close()

    def recording_scanner_close() -> None:
        calls.append("scanner.close")

    monkeypatch.setattr(worker, "stop", recording_stop)
    monkeypatch.setattr(refresher, "stop", recording_refresher_stop)
    monkeypatch.setattr(paperless, "close", recording_paperless_close)
    monkeypatch.setattr(store, "close", recording_store_close)
    monkeypatch.setattr(scanner, "close", recording_scanner_close)
    caplog.set_level(logging.INFO, logger=_APP_LOGGER)
    with TestClient(app):
        pass
    assert calls == [
        "worker.stop",
        "refresher.stop",
        "paperless.close",
        "job_store.close",
        "scanner.close",
    ]
    assert any(
        record.name == _APP_LOGGER
        and record.levelno == logging.INFO
        and record.getMessage() == "App shutdown complete"
        for record in caplog.records
    )


def test_both_stop_events_are_set_before_either_join_begins(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Signalling both threads first is what lets an idle one exit before its join.

    It is not what bounds the total -- the joins are sequential, and the shared
    deadline is what keeps the worst case at one bound (WR-07, and
    ``TestShutdownSharesOneJoinBudget`` below).  What the early signal buys is
    asserted here instead, on the recorded order of the internal event sets and
    joins rather than on elapsed time: a timing assertion would be a flake on a
    loaded machine and would still not say *why* the shutdown was quick.

    ``refresher.join`` usually does not appear at all.  By the time the worker's
    bounded join returns, the refresher's one-second ``Event.wait`` has woken on
    the event set before it and the thread has already exited, so ``stop()``
    skips the join entirely.  That absence is the whole benefit, which is why
    this asserts over the joins that happened rather than over a fixed
    four-element list.
    """
    app = _build_app(settings)
    worker = app.state.worker
    refresher = app.state.refresher
    events: list[str] = []
    worker_set = worker._stopping.set
    refresher_set = refresher._stopping.set
    worker_join = worker._thread.join
    refresher_join = refresher._thread.join

    def recording_worker_set() -> None:
        events.append("worker.set")
        worker_set()

    def recording_refresher_set() -> None:
        events.append("refresher.set")
        refresher_set()

    def recording_worker_join(timeout: float | None = None) -> None:
        events.append("worker.join")
        worker_join(timeout=timeout)

    def recording_refresher_join(timeout: float | None = None) -> None:
        events.append("refresher.join")
        refresher_join(timeout=timeout)

    monkeypatch.setattr(worker._stopping, "set", recording_worker_set)
    monkeypatch.setattr(refresher._stopping, "set", recording_refresher_set)
    monkeypatch.setattr(worker._thread, "join", recording_worker_join)
    monkeypatch.setattr(refresher._thread, "join", recording_refresher_join)
    with TestClient(app):
        pass
    assert {"worker.set", "refresher.set"} <= set(events)
    last_set = max(events.index("worker.set"), events.index("refresher.set"))
    joins = [index for index, name in enumerate(events) if name.endswith(".join")]
    assert joins
    assert all(index > last_set for index in joins)
    # Non-vacuous: the worker's join is always reached, and the refresher was
    # already signalled before it began.  That is the free exit.
    assert "worker.join" in events
    assert events.index("refresher.set") < events.index("worker.join")


class _FakeMonotonic:
    """
    A stand-in for ``app``'s ``time`` module whose clock the test moves by hand.

    Only ``monotonic`` is needed: the lifespan reads it twice, once for the
    deadline and once for what is left of it.  Moving it by hand is how a
    worker can be made to cost the whole join bound without anything sleeping.
    """

    def __init__(self, start: float = 1000.0) -> None:
        """Start the fake clock at ``start`` seconds."""
        self.now = start

    def monotonic(self) -> float:
        """Return the current fake monotonic reading."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward, the way a slow join would move a real one."""
        self.now += seconds


def _recorded_refresher_budget(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    worker_cost: float,
) -> float | None:
    """
    Run one whole lifespan and report the bound ``refresher.stop`` was given.

    ``worker.stop`` is wrapped so it really stops the worker and then advances
    the fake clock by ``worker_cost``, which is how much of the shared deadline
    a slow worker is made to spend.  ``refresher.stop`` is wrapped so it records
    the bound and then joins with its own default, so the real thread always
    stops and this test leaks neither a thread nor an open store.
    """
    app = _build_app(settings)
    worker = app.state.worker
    refresher = app.state.refresher
    clock = _FakeMonotonic()
    budgets: list[float | None] = []
    original_worker_stop = worker.stop
    original_refresher_stop = refresher.stop

    def costly_worker_stop() -> bool:
        stopped = original_worker_stop()
        clock.advance(worker_cost)
        return stopped

    def recording_refresher_stop(timeout: float | None = None) -> bool:
        budgets.append(timeout)
        return original_refresher_stop()

    monkeypatch.setattr(app_module, "time", clock, raising=False)
    monkeypatch.setattr(worker, "stop", costly_worker_stop)
    monkeypatch.setattr(refresher, "stop", recording_refresher_stop)
    with TestClient(app):
        pass
    assert len(budgets) == 1
    return budgets[0]


class TestShutdownSharesOneJoinBudget:
    """
    One deadline is taken before either join, and the two joins spend it (WR-07).

    Without this, two threads parked in an unbounded ``getaddrinfo`` cost
    ``2 * STOP_JOIN_SECONDS``, and a container stop grace period sized on the
    documented bound is half of what it needs to be.
    """

    def test_an_instant_worker_leaves_the_refresher_the_whole_budget(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A worker that stops at once spends none of the shared deadline."""
        budget = _recorded_refresher_budget(settings, monkeypatch, worker_cost=0.0)
        assert budget == pytest.approx(STOP_JOIN_SECONDS)

    def test_a_worker_that_spends_half_the_budget_leaves_half(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What the worker's join took is gone from the refresher's share."""
        budget = _recorded_refresher_budget(
            settings, monkeypatch, worker_cost=STOP_JOIN_SECONDS / 2
        )
        assert budget == pytest.approx(STOP_JOIN_SECONDS / 2)

    def test_a_worker_that_spends_the_whole_budget_leaves_nothing(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The assertion that makes the doubling impossible.

        A worker that spent the whole deadline leaves the refresher a poll,
        not a second full bound.
        """
        budget = _recorded_refresher_budget(
            settings, monkeypatch, worker_cost=STOP_JOIN_SECONDS
        )
        assert budget == 0.0

    def test_an_overspent_budget_never_reaches_stop_as_a_negative(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A worker slower than the whole bound still yields zero, not below."""
        budget = _recorded_refresher_budget(
            settings, monkeypatch, worker_cost=STOP_JOIN_SECONDS * 2
        )
        assert budget == 0.0


def test_the_refresher_starts_after_the_worker(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate accessor needs a live worker behind it before the first tick."""
    app = _build_app(settings)
    worker = app.state.worker
    refresher = app.state.refresher
    calls: list[str] = []
    worker_start = worker.start
    refresher_start = refresher.start

    def recording_worker_start() -> None:
        calls.append("worker.start")
        worker_start()

    def recording_refresher_start() -> None:
        calls.append("refresher.start")
        refresher_start()

    monkeypatch.setattr(worker, "start", recording_worker_start)
    monkeypatch.setattr(refresher, "start", recording_refresher_start)
    with TestClient(app):
        pass
    assert calls == ["worker.start", "refresher.start"]


def test_the_refresher_thread_runs_only_inside_the_lifespan(
    settings: Settings,
) -> None:
    """Entering starts the thread; leaving joins it, so no test leaks one."""
    app = _build_app(settings)
    refresher = app.state.refresher
    assert refresher._thread.is_alive() is False
    with TestClient(app):
        assert refresher._thread.is_alive() is True
    assert refresher._thread.is_alive() is False


def test_the_refreshers_scan_fact_is_the_workers_own_job_id(
    settings: Settings,
) -> None:
    """
    WR-04: the strip's words and its colour read the same fact.

    ``_checks_context`` renders ``scan_active`` from ``worker.current_job_id``,
    and the refresher's scanner skip has to come from there too -- deriving it
    from a failed lock acquisition is what let "not checked while a scan is
    running" appear on an idle appliance.

    Args:
        settings: The test's own configuration.

    """
    app = _build_app(settings)
    with TestClient(app):
        worker = app.state.worker
        scan_active = app.state.refresher._scan_active
        assert worker.current_job_id is None
        assert scan_active() is False
        worker._current_job_id = "job-1"
        try:
            assert scan_active() is True
        finally:
            worker._current_job_id = None


def test_startup_runs_no_check_probe(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Server start is never delayed by a probe (D-04, D-06).

    Nothing stamps a watcher across an empty lifespan, so the refresher's ticks
    return before reaching the network and the cache is still cold afterwards --
    which is precisely the state the first render shows as ``Checking…``.
    """
    calls: list[object] = []

    def spy_run_checks(context: object, **_kwargs: object) -> tuple[()]:
        calls.append(context)
        return ()

    monkeypatch.setattr(refresher_module, "run_checks", spy_run_checks)
    app = _build_app(settings)
    with TestClient(app):
        pass
    assert calls == []
    assert app.state.checks.current().results is None


def test_shutdown_leaves_resources_open_when_the_refresher_does_not_stop(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A refresher still inside a probe closes nothing, even with the worker stopped.

    This is Pitfall 1: ``paperless.close()`` under an in-flight probe raises
    inside the refresher, and ``scanner.close()`` runs ``sane_exit()`` while a
    ``sane_get_devices`` call may be outstanding, which ``sane_backend`` names
    as a segfault risk.
    """
    scanner = StubScannerBackend()
    app = create_app(settings, scanner)
    app.state.paperless.get_tags = list
    app.state.paperless.get_correspondents = list
    refresher = app.state.refresher
    store: JobStore = app.state.job_store
    paperless = app.state.paperless
    calls: list[str] = []
    real_refresher_stop = refresher.stop
    real_store_close = store.close
    real_paperless_close = paperless.close

    def stuck_stop(timeout: float | None = None) -> bool:
        return False

    def spy_paperless_close() -> None:
        calls.append("paperless.close")

    def spy_store_close() -> None:
        calls.append("job_store.close")

    def spy_scanner_close() -> None:
        calls.append("scanner.close")

    monkeypatch.setattr(refresher, "stop", stuck_stop)
    monkeypatch.setattr(paperless, "close", spy_paperless_close)
    monkeypatch.setattr(store, "close", spy_store_close)
    monkeypatch.setattr(scanner, "close", spy_scanner_close)
    caplog.set_level(logging.INFO, logger=_APP_LOGGER)
    try:
        with TestClient(app):
            pass
        assert calls == []
        assert any(
            record.name == _APP_LOGGER
            and record.levelno == logging.WARNING
            and "refresher" in record.getMessage()
            for record in caplog.records
        )
    finally:
        # The real thread was signalled by the lifespan and is idle; join it
        # and release what the app left open, so this test leaks nothing.
        assert real_refresher_stop() is True
        real_paperless_close()
        real_store_close()


def test_shutdown_leaves_resources_open_when_the_worker_does_not_stop(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stuck worker keeps the store and client open, and nothing is written."""
    app = _build_app(settings)
    worker = app.state.worker
    store: JobStore = app.state.job_store
    paperless = app.state.paperless
    existing = store.create_job("default", "written before startup")
    calls: list[str] = []
    stop_requested = False
    real_stop = worker.stop
    real_store_close = store.close
    real_paperless_close = paperless.close

    def stuck_stop() -> bool:
        nonlocal stop_requested
        stop_requested = True
        return False

    def spy_paperless_close() -> None:
        calls.append("paperless.close")

    def spy_store_close() -> None:
        calls.append("job_store.close")

    # The worker is idle, so nothing should write at all; any write after
    # stop() was asked would be the shutdown-time write D-07 forbids.
    def spy_finish_job(*args: object, **kwargs: object) -> None:
        if stop_requested:
            calls.append("finish_job")

    def spy_update_state(*args: object, **kwargs: object) -> None:
        if stop_requested:
            calls.append("update_state")

    monkeypatch.setattr(worker, "stop", stuck_stop)
    monkeypatch.setattr(worker, "_current_job_id", "job-xyz")
    monkeypatch.setattr(paperless, "close", spy_paperless_close)
    monkeypatch.setattr(store, "close", spy_store_close)
    monkeypatch.setattr(store, "finish_job", spy_finish_job)
    monkeypatch.setattr(store, "update_state", spy_update_state)
    caplog.set_level(logging.INFO, logger=_APP_LOGGER)
    try:
        with TestClient(app):
            pass
        assert stop_requested
        assert calls == []
        still_open = store.get_job(existing.id)
        assert still_open is not None
        assert any(
            record.name == _APP_LOGGER
            and record.levelno == logging.WARNING
            and "job-xyz" in record.getMessage()
            and "5" in record.getMessage()
            for record in caplog.records
        )
    finally:
        # The real thread is idle; stop it and release what the app left open,
        # so this test leaks neither a thread nor a database handle.
        assert real_stop()
        real_paperless_close()
        real_store_close()


class _ClosingScanner(StubScannerBackend):
    """A stub backend that records the closes the lifespan gives it."""

    def __init__(self) -> None:
        """Start with no close recorded."""
        self.close_calls = 0

    def close(self) -> None:
        """Count the close the lifespan owes this backend."""
        self.close_calls += 1


def test_shutdown_closes_the_scanner_after_the_store(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The scanner is the third thing closed, after Paperless and the store (D-18).

    It goes last because it is the one whose close reaches process-global
    state: ``sane_exit`` closes every open handle, so it runs only once the
    worker has confirmed it stopped and the rest of the shutdown is done.
    """
    scanner = _ClosingScanner()
    app = create_app(settings, scanner)
    app.state.paperless.get_tags = list
    app.state.paperless.get_correspondents = list
    store: JobStore = app.state.job_store
    paperless = app.state.paperless
    calls: list[str] = []
    original_paperless_close = paperless.close
    original_store_close = store.close

    def recording_paperless_close() -> None:
        calls.append("paperless.close")
        original_paperless_close()

    def recording_store_close() -> None:
        calls.append("job_store.close")
        original_store_close()

    def recording_scanner_close() -> None:
        calls.append("scanner.close")

    monkeypatch.setattr(paperless, "close", recording_paperless_close)
    monkeypatch.setattr(store, "close", recording_store_close)
    monkeypatch.setattr(scanner, "close", recording_scanner_close)
    with TestClient(app):
        pass

    assert calls == ["paperless.close", "job_store.close", "scanner.close"]


def test_shutdown_leaves_the_scanner_open_when_the_worker_does_not_stop(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A worker that did not stop may still be inside SANE, so nothing is closed.

    This is the same rule D-09 already applies to the store and the Paperless
    client, and it matters more here: ``sane_exit`` closes every open handle
    and runs holding the GIL, which is precisely what a thread still inside a
    read cannot survive.
    """
    scanner = _ClosingScanner()
    app = create_app(settings, scanner)
    app.state.paperless.get_tags = list
    app.state.paperless.get_correspondents = list
    worker = app.state.worker
    store: JobStore = app.state.job_store
    paperless = app.state.paperless
    real_stop = worker.stop

    monkeypatch.setattr(worker, "stop", lambda: False)
    try:
        with TestClient(app):
            pass
        assert scanner.close_calls == 0
    finally:
        # The real thread is idle; stop it and release what the app left open.
        assert real_stop()
        paperless.close()
        store.close()


def test_idle_worker_shutdown_closes_the_store(settings: Settings) -> None:
    """With the real, idle worker, leaving the lifespan closes the job store."""
    app = _build_app(settings)
    store: JobStore = app.state.job_store
    existing = store.create_job("default", "written before startup")
    with TestClient(app):
        pass
    with pytest.raises(sqlite3.ProgrammingError):
        store.get_job(existing.id)


# --- SANE is unreachable from a request path (HARD-05, D-19) -----------------

# How every route the app exposes is called, so the proof below can drive all
# of them.  A route the app serves and this map does not name fails the test
# rather than being passed over in silence: that is what makes a route added
# later covered on the day it lands, instead of quietly uncovered.
_ROUTE_CALLS: dict[str, dict[str, Any]] = {
    "/docs": {"method": "GET"},
    "/docs/oauth2-redirect": {"method": "GET"},
    "/redoc": {"method": "GET"},
    "/": {"method": "GET"},
    "/health": {"method": "GET"},
    "/api/paperless/test": {"method": "GET"},
    "/api/scan": {
        "method": "POST",
        "data": {"profile": "default", "title": "D-19 proof"},
    },
    "/api/jobs/current/status": {"method": "GET"},
    # Driven with the literal template path, which names no row.  That is a
    # real request the route handles by design: an unknown id degrades to the
    # current-or-most-recent rendering rather than a 404 (D-25), so no job has
    # to be staged for this proof to reach the handler.
    "/api/jobs/{job_id}/status": {"method": "GET"},
    # The strip is a cache read, so it enters no SANE call at all; the refresh
    # route is the one that does, which is exactly why this proof must drive it
    # -- it runs the scanner check through the same backend handle the worker
    # uses, and it must not leave one outstanding at sane_exit() (D-18, A-7).
    "/api/checks": {"method": "GET"},
    "/api/checks/refresh": {"method": "POST"},
    "/api/tags": {"method": "GET"},
    "/api/correspondents": {"method": "GET"},
    # Driven with a real profile name, so the handler runs its locked lookup
    # and renders rather than short-circuiting on the 422 an unknown name gets.
    "/api/profiles/description": {"method": "GET", "params": {"profile": "default"}},
    "/api/cache/invalidate": {"method": "POST", "params": {"resource": "tags"}},
    "/api/jobs/history": {"method": "GET"},
    # Answering a prompt no job is waiting at is a real request that the route
    # handles by design (D-16), so the flip routes need no job to be driven.
    "/api/flip/continue": {"method": "POST", "data": {"job_id": "no-such-job"}},
    "/api/flip/abort": {"method": "POST", "data": {"job_id": "no-such-job"}},
}

# The entries the proof does not drive, each named with its reason rather than
# dropped, so what is covered stays auditable.
_ROUTE_SKIPS: dict[str, str] = {
    "/static": (
        "a StaticFiles mount rather than an endpoint: it serves bytes off disk "
        "through Starlette and runs no saneless code at all"
    ),
    "/openapi.json": (
        "FastAPI's generated schema, which this app cannot produce: the route "
        "handlers annotate their returns as 'Response' under postponed "
        "evaluation, and pydantic raises rather than resolving the forward "
        "reference. It runs no saneless handler and opens no scanner, and the "
        "500 predates this plan; recorded in deferred-items.md"
    ),
}


def test_sane_lifecycle_across_startup_every_route_and_shutdown(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    No request path reaches sane.init() or sane.exit() (HARD-05, D-19).

    Asserted behaviourally rather than by searching for the names: a route
    that constructed a backend, or a handler that shut SANE down to recover
    from something, would move these counters while passing any grep.  The app
    is driven over a real ``SaneBackend`` -- the only backend that touches SANE
    at all -- so the counters are the library's own view of what happened.

    ``exit_while_blocked`` ties the proof to D-12/D-13: whatever the routes did,
    ``sane_exit`` never ran with a read outstanding.
    """
    fake = FakeSaneModule(
        devices=[("test:device:001", "TestVendor", "TestModel", "scanner")]
    )
    monkeypatch.setattr(sane_backend_mod, "sane", fake)
    # This process may have initialised SANE in an earlier test; the guard is
    # process-level by design, so the proof starts by re-arming it.
    sane_backend_mod.shutdown()
    fake.exit_call_count = 0

    scanner = SaneBackend()
    assert fake.init_call_count == 1

    app = create_app(settings, scanner)
    app.state.paperless.get_tags = list
    app.state.paperless.get_correspondents = list
    app.state.paperless.test_connection = lambda: "connected"
    app.state.paperless.upload_document = lambda *_a, **_k: UploadResult(
        delivered_to_api=True, task_uuid="d-19-proof"
    )
    app.state.paperless.poll_task = lambda *_a, **_k: {"status": "SUCCESS"}
    store: JobStore = app.state.job_store

    uncovered = {
        route.path
        for route in app.routes
        if isinstance(route, Route | Mount)
        and route.path not in _ROUTE_CALLS
        and route.path not in _ROUTE_SKIPS
    }
    assert uncovered == set(), (
        f"routes {sorted(uncovered)} are neither driven nor skipped with a "
        f"reason; add them to _ROUTE_CALLS or _ROUTE_SKIPS"
    )

    with TestClient(app) as client:
        assert fake.init_call_count == 1
        assert fake.exit_call_count == 0

        for route in app.routes:
            path = getattr(route, "path", "")
            call = _ROUTE_CALLS.get(path)
            if call is None:
                continue
            kwargs = dict(call)
            method = kwargs.pop("method")
            response = client.request(method, path, **kwargs)
            assert response.status_code < 500, (path, response.status_code)
            # The claim is per route, not per run: a single count at the end
            # could not say which handler had moved it.
            assert fake.init_call_count == 1, path
            assert fake.exit_call_count == 0, path

        # The submitted scan runs on the worker thread, so it is waited out
        # here rather than raced with the shutdown below: a worker that had
        # not stopped would skip the close for an honest reason and say
        # nothing about reachability.
        submitted = store.list_recent(limit=10)
        assert submitted, "POST /api/scan created no job, so nothing was proven"
        for job in submitted:
            wait_for_state(store, job.id, TERMINAL_STATES, timeout=15.0)
        # And the submission really did reach the scanner -- from the worker
        # thread, which is the only place the app is allowed to touch SANE
        # from.  Without this the counters above would also be satisfied by a
        # scan that never started.
        assert fake.open("test:device:001").calls

    assert fake.init_call_count == 1
    assert fake.exit_call_count == 1
    assert fake.exit_while_blocked is False
