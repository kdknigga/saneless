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
import os
import re
import sqlite3
import stat
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount, Route

import saneless.scanner.sane_backend as sane_backend_mod
from saneless import worker as worker_module
from saneless.config import (
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
)
from saneless.job import JobStore
from saneless.paperless import ApiDelivery, TaskFiled
from saneless.pipeline import PipelineRequest, ScanResult
from saneless.scanner.sane_backend import SaneBackend
from saneless.vocabulary import (
    RESTART_REASON,
    RESTART_UPLOADING_REASON,
    TERMINAL_STATES,
    ErrorCategory,
    JobState,
    ScanOutcome,
    WorkerHealth,
)
from saneless.web import app as app_module
from saneless.web import refresher as refresher_module
from saneless.web.app import create_app
from saneless.web.cache import CachedMetadataLookup
from saneless.worker import STOP_JOIN_SECONDS
from tests.conftest import (
    StubScannerBackend,
    leaf_routes,
    leave_killed_workspace,
    poll_until,
    wait_for_state,
)
from tests.fake_sane import FakeSaneModule

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

_APP_LOGGER = "saneless.web.app"

# The worker's idle tick while a test waits for it to heal, and how long the
# test waits: a degraded worker retries startup recovery on every tick.
_FAST_TICK = 0.02
_HEAL_BUDGET = 5.0

# ``PRAGMA auto_vacuum`` reads 2 for INCREMENTAL.
_INCREMENTAL = 2

# The status area's opening tag, captured whole so a polling attribute
# elsewhere on the page cannot satisfy or break the assertion.
_STATUS_AREA = re.compile(r'<div id="status-area"(?P<attrs>[^>]*)>')

# The owner token the crashed process's rows record, so a browser presenting it
# is shown their text: a job's detail reaches only the browser that started it.
_SEEDING_BROWSER = "the-browser-that-started-the-crashed-jobs"


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
    store = JobStore(db_path=settings.output.db_path)
    try:
        done = store.create_job(
            "default", "finished before the crash", owner_token=_SEEDING_BROWSER
        )
        store.finish_job(done.id, JobState.DONE)
        pending = store.create_job(
            "default", "queued when the process died", owner_token=_SEEDING_BROWSER
        )
        flip = store.create_job(
            "default", "waiting at the flip prompt", owner_token=_SEEDING_BROWSER
        )
        store.update_state(flip.id, JobState.AWAITING_FLIP)
        scanning = store.create_job(
            "default", "mid-scan when the process died", owner_token=_SEEDING_BROWSER
        )
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
        client.cookies.set("saneless_owner", _SEEDING_BROWSER)
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

    def recording_fail() -> int:
        calls.append("fail_active_jobs")
        return original_fail()

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

    def failing_fail_active_jobs() -> int:
        msg = "attempt to write a readonly database"
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


# --- Orphaned workspaces at startup -------------------------------------------

# The title the crashed SCANNING row carries, which its workspace records too.
_SCANNING_TITLE = "mid-scan when the process died"


def _orphan_the_scanning_job(settings: Settings, seeded: _Seeded) -> Path:
    """
    Leave the workspace a SIGKILLed scan of the SCANNING row would leave.

    Returns:
        The orphaned workspace, holding two spooled pages.

    """
    return leave_killed_workspace(
        settings.output.tmp_dir, job_id=seeded.scanning, title=_SCANNING_TITLE
    )


def _preserved_pdfs(settings: Settings) -> list[Path]:
    """Return the PDFs in ``failed/``, by name; none if it does not exist."""
    failed_dir = settings.output.failed_dir
    return sorted(failed_dir.glob("*.pdf")) if failed_dir.exists() else []


def test_an_orphaned_workspace_is_named_in_its_jobs_restart_error(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    """A killed scan's pages reach failed/, and its row says where."""
    seeded = _seed_crashed_store(settings)
    workspace = _orphan_the_scanning_job(settings, seeded)
    app = _build_app(settings)
    caplog.set_level(logging.INFO, logger=_APP_LOGGER)
    with TestClient(app) as client:
        store: JobStore = app.state.job_store
        scanning = store.get_job(seeded.scanning)
        others = [store.get_job(job_id) for job_id in seeded.active[:2]]
        client.cookies.set("saneless_owner", _SEEDING_BROWSER)
        page = client.get("/").text

    (pdf,) = _preserved_pdfs(settings)
    assert pdf.name.endswith("-partial.pdf")
    assert not workspace.exists()
    assert scanning is not None
    assert scanning.state is JobState.ERROR
    assert scanning.error is not None
    assert scanning.error.startswith(f"{RESTART_REASON}. ")
    assert scanning.error.count(RESTART_REASON) == 1
    assert f"preserved at {pdf}" in scanning.error
    assert scanning.error_category is None
    for other in others:
        assert other is not None
        assert (other.state, other.error) == (JobState.ERROR, RESTART_REASON)
    # The owner's page names the kept file relative to the data directory.
    assert f"failed/{pdf.name}" in page
    warnings = _interrupted_warnings(caplog)
    assert len(warnings) == 1
    assert "3" in warnings[0]


def test_a_failing_orphan_sweep_never_stops_startup(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A sweep that raises is logged; recovery and the app carry on."""
    seeded = _seed_crashed_store(settings)

    def failing_sweep(*_args: object, **_kwargs: object) -> list[object]:
        msg = "tmp_dir went away while the orphan sweep read it"
        raise OSError(msg)

    monkeypatch.setattr(app_module, "sweep_orphans", failing_sweep)
    app = _build_app(settings)
    caplog.set_level(logging.INFO, logger=_APP_LOGGER)
    with TestClient(app) as client:
        response = client.get("/health")
        store: JobStore = app.state.job_store
        rows = [store.get_job(job_id) for job_id in seeded.active]

    assert response.status_code == 200
    for row in rows:
        assert row is not None
        assert (row.state, row.error) == (JobState.ERROR, RESTART_REASON)
    assert any(
        record.name == _APP_LOGGER
        and record.levelno == logging.WARNING
        and record.exc_info is not None
        and "workspace recovery" in record.getMessage()
        for record in caplog.records
    )


def test_an_orphan_text_the_store_refused_is_written_once_it_recovers(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The worker starts degraded and later writes where the pages went."""
    monkeypatch.setattr(worker_module, "_IDLE_TICK_SECONDS", _FAST_TICK)
    seeded = _seed_crashed_store(settings)
    _orphan_the_scanning_job(settings, seeded)
    app = _build_app(settings)
    store: JobStore = app.state.job_store
    original = store.fail_recovered_jobs
    calls: list[dict[str, str]] = []

    def failing_once(texts: Mapping[str, str]) -> int:
        calls.append(dict(texts))
        if len(calls) == 1:
            msg = "attempt to write a readonly database"
            raise sqlite3.OperationalError(msg)
        return original(texts)

    monkeypatch.setattr(store, "fail_recovered_jobs", failing_once)
    caplog.set_level(logging.INFO, logger=_APP_LOGGER)
    with TestClient(app):
        healed = poll_until(
            lambda: app.state.worker.health is WorkerHealth.HEALTHY, _HEAL_BUDGET
        )
        scanning = store.get_job(seeded.scanning)

    (pdf,) = _preserved_pdfs(settings)
    assert healed
    assert len(calls) == 2
    assert calls[0] == calls[1]
    assert scanning is not None
    assert scanning.state is JobState.ERROR
    assert scanning.error is not None
    assert scanning.error.startswith(f"{RESTART_REASON}. ")
    assert f"preserved at {pdf}" in scanning.error
    assert any(
        record.name == _APP_LOGGER
        and record.levelno == logging.ERROR
        and "Crash recovery" in record.getMessage()
        for record in caplog.records
    )


# --- A restart while uploading -----------------------------------------------

# The title the crashed UPLOADING row carries, which its workspace records too.
_UPLOADING_TITLE = "uploading when the process died"


@dataclass(frozen=True)
class _SeededUpload:
    """The ids of an uploading row and a scanning row a crashed process left."""

    uploading: str
    scanning: str


def _seed_crashed_upload(settings: Settings) -> _SeededUpload:
    """
    Write an UPLOADING row and a SCANNING row, as a killed process leaves them.

    Returns:
        The two rows' ids.

    """
    store = JobStore(db_path=settings.output.db_path)
    try:
        uploading = store.create_job(
            "default", _UPLOADING_TITLE, owner_token=_SEEDING_BROWSER
        )
        store.update_state(uploading.id, JobState.UPLOADING)
        scanning = store.create_job(
            "default", _SCANNING_TITLE, owner_token=_SEEDING_BROWSER
        )
        store.update_state(scanning.id, JobState.SCANNING)
    finally:
        store.close()
    return _SeededUpload(uploading=uploading.id, scanning=scanning.id)


def _orphan_the_uploading_job(settings: Settings, seeded: _SeededUpload) -> Path:
    """
    Leave the workspace a SIGKILLed upload of the UPLOADING row would leave.

    Returns:
        The orphaned workspace, holding two spooled pages.

    """
    return leave_killed_workspace(
        settings.output.tmp_dir, job_id=seeded.uploading, title=_UPLOADING_TITLE
    )


def test_startup_restart_words_an_uploading_row_as_maybe_delivered(
    settings: Settings,
) -> None:
    """An uploading row may have reached paperless-ngx; a scanning row did not."""
    seeded = _seed_crashed_upload(settings)
    app = _build_app(settings)
    with TestClient(app):
        store: JobStore = app.state.job_store
        uploading = store.get_job(seeded.uploading)
        scanning = store.get_job(seeded.scanning)

    assert uploading is not None
    assert uploading.state is JobState.ERROR
    assert uploading.error == RESTART_UPLOADING_REASON
    assert uploading.error_category is ErrorCategory.UNCONFIRMED_SEND
    assert scanning is not None
    assert scanning.state is JobState.ERROR
    assert scanning.error == RESTART_REASON
    assert scanning.error_category is None


def test_startup_restart_names_the_kept_pages_of_an_uploading_job(
    settings: Settings,
) -> None:
    """The kept-file sentence follows the uploading reason, said once."""
    seeded = _seed_crashed_upload(settings)
    _orphan_the_uploading_job(settings, seeded)
    app = _build_app(settings)
    with TestClient(app):
        store: JobStore = app.state.job_store
        uploading = store.get_job(seeded.uploading)
        scanning = store.get_job(seeded.scanning)

    (pdf,) = _preserved_pdfs(settings)
    assert uploading is not None
    assert uploading.state is JobState.ERROR
    assert uploading.error is not None
    assert uploading.error.startswith(f"{RESTART_UPLOADING_REASON}. ")
    assert RESTART_REASON not in uploading.error
    assert uploading.error.count(RESTART_UPLOADING_REASON) == 1
    assert f"preserved at {pdf}" in uploading.error
    assert uploading.error_category is ErrorCategory.UNCONFIRMED_SEND
    assert scanning is not None
    assert (scanning.state, scanning.error, scanning.error_category) == (
        JobState.ERROR,
        RESTART_REASON,
        None,
    )


def test_a_refused_restart_of_an_uploading_job_is_worded_once_the_store_recovers(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker's replay words and categorises the rows as startup would have."""
    monkeypatch.setattr(worker_module, "_IDLE_TICK_SECONDS", _FAST_TICK)
    seeded = _seed_crashed_upload(settings)
    _orphan_the_uploading_job(settings, seeded)
    app = _build_app(settings)
    store: JobStore = app.state.job_store
    original = store.fail_recovered_jobs
    calls: list[dict[str, str]] = []

    def failing_once(kept: Mapping[str, str]) -> int:
        calls.append(dict(kept))
        if len(calls) == 1:
            msg = "attempt to write a readonly database"
            raise sqlite3.OperationalError(msg)
        return original(kept)

    monkeypatch.setattr(store, "fail_recovered_jobs", failing_once)
    with TestClient(app):
        healed = poll_until(
            lambda: app.state.worker.health is WorkerHealth.HEALTHY, _HEAL_BUDGET
        )
        uploading = store.get_job(seeded.uploading)
        scanning = store.get_job(seeded.scanning)

    (pdf,) = _preserved_pdfs(settings)
    assert healed
    assert len(calls) == 2
    assert calls[0] == calls[1]
    # The mapping carries only where the pages went: the store words the
    # restart by the row's state.
    assert all(RESTART_REASON not in text for text in calls[0].values())
    assert uploading is not None
    assert uploading.state is JobState.ERROR
    assert uploading.error is not None
    assert uploading.error.startswith(f"{RESTART_UPLOADING_REASON}. ")
    assert f"preserved at {pdf}" in uploading.error
    assert uploading.error_category is ErrorCategory.UNCONFIRMED_SEND
    assert scanning is not None
    assert (scanning.state, scanning.error, scanning.error_category) == (
        JobState.ERROR,
        RESTART_REASON,
        None,
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


def test_the_stuck_worker_warning_names_the_preservation_extension(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    The warning says the worker's join may have run past the ordinary bound.

    A worker keeping a stopped scan's pages waits up to
    ``PRESERVATION_JOIN_SECONDS`` longer than ``STOP_JOIN_SECONDS``, so a
    warning naming only the ordinary bound would understate how long the
    shutdown took and send the operator looking for the wrong cause.
    """
    app = _build_app(settings)
    worker = app.state.worker
    store: JobStore = app.state.job_store
    paperless = app.state.paperless
    real_stop = worker.stop

    monkeypatch.setattr(worker, "stop", lambda: False)
    caplog.set_level(logging.WARNING, logger=_APP_LOGGER)
    try:
        with TestClient(app):
            pass
    finally:
        assert real_stop()
        paperless.close()
        store.close()

    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == _APP_LOGGER
        and record.levelno == logging.WARNING
        and "did not stop within" in record.getMessage()
    ]
    assert len(warnings) == 1
    assert f"up to {worker_module.PRESERVATION_JOIN_SECONDS:g} s" in warnings[0]
    assert "preserv" in warnings[0]


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
# later covered on the day it lands, instead of quietly uncovered.  The reverse
# holds too: an entry naming a route the app no longer serves fails the test,
# so a removed route cannot leave a stale entry behind, and a drive the router
# turns away with a 404 or 405 fails it, so an entry whose method no longer
# matches its route cannot pass without reaching the handler.
_ROUTE_CALLS: dict[str, dict[str, Any]] = {
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
    "/api/profiles/multi-page": {"method": "GET", "params": {"profile": "default"}},
    "/api/cache/invalidate": {"method": "POST", "params": {"resource": "tags"}},
    "/api/jobs/history": {"method": "GET"},
    # Answering a prompt no job is waiting at is a real request that the route
    # handles by design (D-16), so the flip routes need no job to be driven.
    "/api/flip/continue": {"method": "POST", "data": {"job_id": "no-such-job"}},
    "/api/flip/abort": {"method": "POST", "data": {"job_id": "no-such-job"}},
    # The same holds for a multi-page answer: it names no waiting job, so the
    # route drops it and renders the status, reaching the handler all the same.
    "/api/multi-page/answer": {
        "method": "POST",
        "data": {"job_id": "no-such-job", "prompt": "1", "answer": "NEXT"},
    },
}

# The entries the proof does not drive, each named with its reason rather than
# dropped, so what is covered stays auditable.
_ROUTE_SKIPS: dict[str, str] = {
    "/static": (
        "a StaticFiles mount rather than an endpoint: it serves bytes off disk "
        "through Starlette and runs no saneless code at all"
    ),
}

# Every path the real app serves, measured rather than predicted.  Held here as
# the phase's canonical set so the helper's own test compares against something
# independent of the route map above, which the lifecycle proof already checks
# the app against in both directions.
_LEAF_PATHS = frozenset(
    {
        "/",
        "/api/cache/invalidate",
        "/api/checks",
        "/api/checks/refresh",
        "/api/correspondents",
        "/api/flip/abort",
        "/api/flip/continue",
        "/api/jobs/current/status",
        "/api/jobs/history",
        "/api/jobs/{job_id}/status",
        "/api/multi-page/answer",
        "/api/paperless/test",
        "/api/profiles/description",
        "/api/profiles/multi-page",
        "/api/scan",
        "/api/tags",
        "/health",
        "/static",
    }
)


def test_leaf_routes_flattens_the_included_router(settings: Settings) -> None:
    """
    leaf_routes yields every leaf, the /static Mount included (DEP-05, D-03).

    fastapi 0.141 represents an included router as one opaque wrapper object in
    ``app.routes`` rather than splicing its routes in, so a plain
    ``isinstance(route, APIRoute)`` filter over ``app.routes`` finds none of
    them.  D-03 requires the helper to yield ``Mount`` objects as well as
    ``APIRoute`` ones, because the served-against-map check below enumerates
    ``Route | Mount`` and would otherwise report ``/static`` as stale.

    The app is driven inside the lifespan so the job store it opened is closed
    afterwards; ``create_app`` opens that store eagerly and only the lifespan
    shutdown closes it.
    """
    app = _build_app(settings)
    with TestClient(app):
        leaves = leaf_routes(app)

        assert len(leaves) == 18, (
            f"the app serves {len(leaves)} leaf routes, not the 18 this test "
            f"pins; a route was added or removed, so update this literal"
        )
        api_routes = [route for route in leaves if isinstance(route, APIRoute)]
        assert len(api_routes) == 17, (
            f"{len(api_routes)} of the leaves are APIRoute, not the 17 this "
            f"test pins; a route was added or removed, so update this literal"
        )
        # D-03's whole point: the Mount survives the flattening.
        mounts = [route for route in leaves if isinstance(route, Mount)]
        assert [mount.path for mount in mounts] == ["/static"]

        paths = {route.path for route in leaves if isinstance(route, Route | Mount)}
        assert paths == _LEAF_PATHS


def test_leaf_routes_refuses_to_report_no_routes() -> None:
    """
    An empty enumeration raises rather than silently emptying every caller.

    D-02's choke point, tested directly rather than only through the five call
    sites.  A helper that returned ``[]`` here would leave the cross-origin
    coverage guard green while proving nothing about which routes the guard
    actually covers, so the empty result has to be loud.
    """
    empty = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    with pytest.raises(
        AssertionError, match="included-router wrapper has changed shape"
    ):
        leaf_routes(empty)


class _SaneCallsByThread:
    """
    Record each SANE start and stop, and whether one given thread made it.

    Wraps a fake module's ``init`` and ``exit`` in place, so the fake's own
    counters keep counting every call.
    """

    def __init__(
        self,
        fake: FakeSaneModule,
        thread: threading.Thread,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Start recording.

        Args:
            fake: The module double whose ``init`` and ``exit`` to wrap.
            thread: The thread whose calls are told apart from the rest.
            monkeypatch: Undoes the wrapping at the end of the test.

        """
        self._thread = thread
        self._calls: list[tuple[str, bool]] = []
        real_init = fake.init
        real_exit = fake.exit

        def recording_init() -> tuple[int, int, int, int]:
            self._record("init")
            return real_init()

        def recording_exit() -> None:
            self._record("exit")
            real_exit()

        monkeypatch.setattr(fake, "init", recording_init)
        monkeypatch.setattr(fake, "exit", recording_exit)

    def _record(self, name: str) -> None:
        self._calls.append((name, threading.current_thread() is self._thread))

    def on_thread(self) -> list[str]:
        """
        Name the calls the given thread made, in order.

        Returns:
            ``"init"`` or ``"exit"`` per call.

        """
        return [name for name, on in self._calls if on]

    def elsewhere(self) -> list[str]:
        """
        Name the calls any other thread made, in order.

        Returns:
            ``"init"`` or ``"exit"`` per call.

        """
        return [name for name, on in self._calls if not on]


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

    The worker thread does restart SANE, once at the top of every scan job, so
    each ``init``/``exit`` call is recorded with the thread that made it: a
    request path must make none, and the worker exactly one exit and one init
    per job.

    The route map is checked in both directions: every served route is named
    in it, and every entry in it names a route the app serves.
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
    app.state.paperless.upload_document = lambda *_a, **_k: ApiDelivery(
        task_id="d-19-proof"
    )
    app.state.paperless.poll_task = lambda *_a, **_k: TaskFiled(
        task={"status": "SUCCESS"}
    )
    store: JobStore = app.state.job_store
    # Every SANE start and stop from here on, by thread.  The constructor's
    # init above is already counted by the fake.
    sane_calls = _SaneCallsByThread(fake, app.state.worker._thread, monkeypatch)

    # Size first, contents second: an empty enumeration satisfies every set
    # comparison below, so the count is what makes them mean anything.  The
    # literal is measured against the running app, deliberately not derived
    # from _ROUTE_CALLS | _ROUTE_SKIPS, which the two checks below already
    # compare the app against in both directions.
    enumerated = [
        route for route in leaf_routes(app) if isinstance(route, Route | Mount)
    ]
    assert len(enumerated) == 18, (
        f"the app serves {len(enumerated)} Route/Mount leaves, not the 18 this "
        f"test pins; a route was added or removed, so update this literal"
    )

    uncovered = {
        route.path
        for route in leaf_routes(app)
        if isinstance(route, Route | Mount)
        and route.path not in _ROUTE_CALLS
        and route.path not in _ROUTE_SKIPS
    }
    assert uncovered == set(), (
        f"routes {sorted(uncovered)} are neither driven nor skipped with a "
        f"reason; add them to _ROUTE_CALLS or _ROUTE_SKIPS"
    )
    served = {
        route.path for route in leaf_routes(app) if isinstance(route, Route | Mount)
    }
    stale = (_ROUTE_CALLS.keys() | _ROUTE_SKIPS.keys()) - served
    assert stale == set(), (
        f"the route map names {sorted(stale)}, which the app does not serve; "
        f"remove them from _ROUTE_CALLS or _ROUTE_SKIPS"
    )

    with TestClient(app) as client:
        assert fake.init_call_count == 1
        assert fake.exit_call_count == 0

        # What the loop below will actually drive, asserted before it runs:
        # the existing `assert submitted` fires only afterwards, and a loop
        # over an empty enumeration would reach it having proven nothing.
        drivable = [
            route
            for route in leaf_routes(app)
            if getattr(route, "path", "") in _ROUTE_CALLS
        ]
        assert len(leaf_routes(app)) == 18, (
            f"the app serves {len(leaf_routes(app))} leaves, not the 18 this "
            f"test pins; a route was added or removed, so update this literal"
        )
        assert len(drivable) == 17, (
            f"{len(drivable)} of the leaves are named in _ROUTE_CALLS, not the "
            f"17 this test pins; a route was added or removed, so update this "
            f"literal"
        )

        for route in leaf_routes(app):
            path = getattr(route, "path", "")
            call = _ROUTE_CALLS.get(path)
            if call is None:
                continue
            kwargs = dict(call)
            method = kwargs.pop("method")
            response = client.request(method, path, **kwargs)
            assert response.status_code < 500, (path, response.status_code)
            # A 404 or 405 means routing turned the request away before any
            # handler ran, so the route was never exercised.  This is what
            # catches an entry whose method no longer matches its route.
            assert response.status_code not in {404, 405}, (
                path,
                response.status_code,
            )
            # The claim is per route, not per run: a single count at the end
            # could not say which handler had moved it.  A scan job the route
            # submitted may be restarting SANE on the worker thread meanwhile,
            # which is allowed; a call from any other thread is not.
            assert sane_calls.elsewhere() == [], path

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
        assert fake.device.calls

    # The lifespan's shutdown is the one stop made off the worker thread, and
    # the worker restarted SANE exactly once for each job it ran.
    assert sane_calls.elsewhere() == ["exit"]
    assert sane_calls.on_thread() == ["exit", "init"] * len(submitted)
    assert fake.init_call_count == 1 + len(submitted)
    assert fake.exit_call_count == 1 + len(submitted)
    assert fake.exit_while_blocked is False


# --- The generated schema builds but is not served ----------------------------


def test_the_schema_builds_in_process_and_is_not_served(settings: Settings) -> None:
    """
    app.openapi() builds from the real app, which serves no schema.

    FastAPI resolves a route's return annotation when the decorator runs, so a
    name imported only for type checking leaves a forward reference that makes
    this call raise.  The call runs inside the lifespan so the job store is
    closed afterwards.  The attribute check pins that all three URLs are
    switched off, so switching openapi_url back on cannot quietly bring the
    documentation pages back.
    """
    app = _build_app(settings)
    with TestClient(app) as client:
        schema = app.openapi()
        served = {
            route.path for route in leaf_routes(app) if isinstance(route, APIRoute)
        }
        # Both sides of the comparison below would be empty if the app served
        # no API routes, so the size is asserted before the contents.
        assert len(served) == 17, (
            f"the app serves {len(served)} APIRoute paths, not the 17 this "
            f"test pins; a route was added or removed, so update this literal"
        )
        assert set(schema["paths"]) == served
        assert client.get("/openapi.json").status_code == 404
        assert (app.openapi_url, app.docs_url, app.redoc_url) == (None, None, None)


# --- Private directories and the job database at startup ----------------------


def _private_settings(tmp_path: Path) -> Settings:
    """Build settings whose scratch and data directories do not exist yet."""
    return Settings(
        scanner=ScannerConfig(device="test:device:001"),
        paperless=PaperlessConfig(url="http://localhost:8000", token="test-token"),
        output=OutputConfig(
            tmp_dir=str(tmp_path / "scratch"), data_dir=str(tmp_path / "state")
        ),
        profiles={"default": ProfileConfig()},
    )


def _build_app_under_umask(settings: Settings, umask: int) -> FastAPI:
    """Build the app with ``umask`` in force, restoring the old one after."""
    old = os.umask(umask)
    try:
        return _build_app(settings)
    finally:
        os.umask(old)


def test_create_app_makes_tmp_dir_and_data_dir_private(tmp_path: Path) -> None:
    """Both directories are created 0700 even under umask 002."""
    settings = _private_settings(tmp_path)
    app = _build_app_under_umask(settings, 0o002)
    with TestClient(app):
        pass
    assert stat.S_IMODE(settings.output.tmp_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(settings.output.data_dir.stat().st_mode) == 0o700


def test_create_app_keeps_an_existing_data_dir_mode(tmp_path: Path) -> None:
    """A data_dir that already exists is used as it is, not re-moded."""
    settings = _private_settings(tmp_path)
    settings.output.data_dir.mkdir()
    settings.output.data_dir.chmod(0o755)
    app = _build_app(settings)
    with TestClient(app):
        pass
    assert stat.S_IMODE(settings.output.data_dir.stat().st_mode) == 0o755


def test_create_app_converts_an_old_database_to_incremental_vacuum(
    tmp_path: Path,
) -> None:
    """A job database an earlier release created is converted once, at startup."""
    settings = _private_settings(tmp_path)
    settings.output.data_dir.mkdir(mode=0o700)
    JobStore(db_path=settings.output.db_path).close()
    raw = sqlite3.connect(settings.output.db_path)
    try:
        raw.execute("PRAGMA auto_vacuum = NONE")
        raw.execute("VACUUM")
        assert raw.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
    finally:
        raw.close()

    app = _build_app(settings)
    with TestClient(app):
        pass

    raw = sqlite3.connect(settings.output.db_path)
    try:
        assert raw.execute("PRAGMA auto_vacuum").fetchone()[0] == _INCREMENTAL
    finally:
        raw.close()


def test_create_app_vacuum_failure_is_a_warning_not_a_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A failed conversion is logged and the server still starts.

    ``VACUUM`` needs free space about the size of the database, and a
    database bloated by a flood may sit on a full disk.
    """

    def full_disk(_store: JobStore) -> bool:
        msg = "database or disk is full"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(JobStore, "enable_incremental_auto_vacuum", full_disk)
    settings = _private_settings(tmp_path)
    with caplog.at_level(logging.WARNING, logger=_APP_LOGGER):
        app = _build_app(settings)
    warnings = [
        record
        for record in caplog.records
        if record.name == _APP_LOGGER and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert "auto-vacuum" in warnings[0].getMessage()
    assert warnings[0].exc_info is not None
    with TestClient(app) as client:
        assert client.get("/").status_code == 200


# --- The worker checks ids through the page's metadata cache ------------------


def test_the_worker_lookup_reads_the_page_cache_first(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A job's lookup reads the cache the pickers use, and refetches from the client.

    The pipeline is replaced by one that records its request, so the test
    reads the lookup the app gave the worker without scanning anything.
    """
    captured: list[PipelineRequest] = []

    def capturing_pipeline(
        _scanner: object,
        _paperless: object,
        _settings: object,
        request: PipelineRequest,
    ) -> ScanResult:
        """Record the request and report a clean upload."""
        captured.append(request)
        return ScanResult(
            outcome=ScanOutcome.SUCCESS,
            pages_scanned=1,
            pages_removed=0,
            pages_uploaded=1,
        )

    fetched: list[float | None] = []

    def listing_tags(*, timeout: float | None = None) -> list[dict[str, object]]:
        """Answer tag 3 only, noting the budget each fetch was given."""
        fetched.append(timeout)
        return [{"id": 3}]

    monkeypatch.setattr("saneless.worker.run_pipeline", capturing_pipeline)
    app = _build_app(settings)
    app.state.paperless.get_tags = listing_tags
    with TestClient(app) as client:
        app.state.cache.set("tags", [{"id": 3}, {"id": 7}])
        response = client.post(
            "/api/scan", data={"profile": "default", "title": "Wired", "tags": ["3"]}
        )
        assert response.status_code == 200, response.text
        store: JobStore = app.state.job_store
        job_id = store.list_recent(limit=1)[0].id
        wait_for_state(store, job_id, TERMINAL_STATES, timeout=5.0)

    assert len(captured) == 1
    lookup = captured[0].metadata_lookup
    assert isinstance(lookup, CachedMetadataLookup)
    assert lookup.tag_ids(fresh=False) == frozenset({3, 7})
    assert fetched == []
    assert lookup.tag_ids(fresh=True) == frozenset({3})
    assert fetched == [5.0]
