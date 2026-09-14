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
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from saneless.config import (
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
)
from saneless.job import JobStore
from saneless.scanner.base import (
    DeviceCapabilities,
    DeviceInfo,
    ScanBatch,
    ScannerBackend,
    ScanSettings,
)
from saneless.vocabulary import RESTART_REASON, JobState, WorkerHealth
from saneless.web.app import create_app

if TYPE_CHECKING:
    from pathlib import Path

    from fastapi import FastAPI

_APP_LOGGER = "saneless.web.app"

# The status area's opening tag, captured whole so a polling attribute
# elsewhere on the page cannot satisfy or break the assertion.
_STATUS_AREA = re.compile(r'<div id="status-area"(?P<attrs>[^>]*)>')


class _StubScanner(ScannerBackend):
    """Concrete scanner stub; these tests never run a scan."""

    def get_devices(self) -> list[DeviceInfo]:
        """Return an empty device list."""
        return []

    def get_capabilities(self, device_id: str) -> DeviceCapabilities:
        """Return default capabilities."""
        return DeviceCapabilities(
            sources=["Flatbed"], resolutions=[300], modes=["color"]
        )

    def scan_pages(self, device_id: str, settings: ScanSettings) -> ScanBatch:
        """Return a batch holding a single white test image."""
        return ScanBatch(
            pages=[Image.new("RGB", (100, 100), "white")],
            actual_resolution=settings.resolution,
            pages_rejected=0,
        )


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
    app = create_app(settings, _StubScanner())
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
