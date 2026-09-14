"""Tests for job model and worker thread."""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw

from saneless.config import ProfileConfig, Settings
from saneless.exceptions import (
    ConfigError,
    FeederEmptyError,
    PaperlessError,
    ScanError,
)
from saneless.job import ErrorCategory, Job, JobResult, JobState, JobStore
from saneless.pipeline import PipelineEvent, ScanResult
from saneless.scanner.base import (
    DeviceCapabilities,
    DeviceInfo,
    ScanBatch,
    ScannerBackend,
)
from saneless.vocabulary import TERMINAL_STATES, FlipOutcome, ScanOutcome
from saneless.worker import ScanWorker, WorkerFlipCoordinator
from tests.conftest import scan_batch

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from unittest.mock import MagicMock

    import pytest

    from saneless.pipeline import PipelineRequest
    from saneless.scanner.base import ScanSettings


def _get(store: JobStore, job_id: str) -> Job:
    """Retrieve a job, asserting it exists (narrows Job | None to Job)."""
    fetched = store.get_job(job_id)
    assert fetched is not None
    return fetched


class TestJobStateTransitions:
    """Job state machine tests."""

    def test_job_state_transitions(self) -> None:
        """Job starts as PENDING and transitions through all active states."""
        store = JobStore()
        try:
            job = store.create_job("default", "Test Doc")
            assert job.state == JobState.PENDING

            store.update_state(job.id, JobState.SCANNING)
            assert _get(store, job.id).state == JobState.SCANNING

            store.update_state(job.id, JobState.ASSEMBLING)
            assert _get(store, job.id).state == JobState.ASSEMBLING

            store.update_state(job.id, JobState.UPLOADING)
            assert _get(store, job.id).state == JobState.UPLOADING

            store.update_state(job.id, JobState.DONE)
            assert _get(store, job.id).state == JobState.DONE
        finally:
            store.close()

    def test_job_state_error(self) -> None:
        """Job can transition to ERROR from any active state."""
        store = JobStore()
        try:
            for start_state in [
                JobState.SCANNING,
                JobState.ASSEMBLING,
                JobState.UPLOADING,
            ]:
                job = store.create_job("default", "Test Doc")
                store.update_state(job.id, start_state)
                store.update_state(job.id, JobState.ERROR, error="Something broke")
                fetched = _get(store, job.id)
                assert fetched.state == JobState.ERROR
                assert fetched.error == "Something broke"
        finally:
            store.close()


class TestJobStore:
    """JobStore persistence tests."""

    def test_job_store_create_and_get(self) -> None:
        """JobStore.create_job returns Job with UUID, get_job returns same."""
        store = JobStore()
        try:
            job = store.create_job("default", "My Document", tags=[1, 2])
            assert job.id is not None
            assert len(job.id) > 0

            fetched = _get(store, job.id)
            assert fetched is not None
            assert fetched.id == job.id
            assert fetched.profile == "default"
            assert fetched.title == "My Document"
            assert fetched.tags == [1, 2]
        finally:
            store.close()

    def test_job_store_update_state(self) -> None:
        """update_state changes the job state."""
        store = JobStore()
        try:
            job = store.create_job("default", "Test")
            store.update_state(job.id, JobState.SCANNING)
            fetched = _get(store, job.id)
            assert fetched.state == JobState.SCANNING
        finally:
            store.close()

    def test_job_store_sqlite_persistence(self, tmp_path: Path) -> None:
        """Jobs survive JobStore close/reopen cycle."""
        db_path = str(tmp_path / "jobs.db")

        store = JobStore(db_path=db_path)
        job = store.create_job("default", "Persistent Doc")
        job_id = job.id
        store.close()

        store2 = JobStore(db_path=db_path)
        try:
            fetched = store2.get_job(job_id)
            assert fetched is not None
            assert fetched.title == "Persistent Doc"
        finally:
            store2.close()


class TestJobStateAwaitingFlip:
    """AWAITING_FLIP state tests."""

    def test_awaiting_flip_exists(self) -> None:
        """JobState.AWAITING_FLIP exists and equals 'AWAITING_FLIP'."""
        assert JobState.AWAITING_FLIP == "AWAITING_FLIP"
        assert JobState.AWAITING_FLIP.value == "AWAITING_FLIP"


class TestJobThumbnail:
    """Job thumbnail field tests."""

    def test_thumbnail_defaults_none(self) -> None:
        """Job.thumbnail field defaults to None."""
        job = Job(id="test", profile="default", title="Test")
        assert job.thumbnail is None

    def test_jobstore_persists_thumbnail(self) -> None:
        """JobStore persists and retrieves thumbnail field."""
        store = JobStore()
        try:
            job = store.create_job("default", "Thumb Test")
            store.update_thumbnail(job.id, "base64data")
            fetched = _get(store, job.id)
            assert fetched.thumbnail == "base64data"
        finally:
            store.close()


class TestScanWorker:
    """Worker thread tests."""

    def test_worker_starts_and_stops(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
    ) -> None:
        """ScanWorker starts background thread and stop() joins it."""
        store = JobStore()
        try:
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()
            assert worker._thread.is_alive()
            worker.stop()
            assert not worker._thread.is_alive()
        finally:
            store.close()

    def test_worker_processes_job(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Submit job to worker -> job reaches DONE state."""
        store = JobStore()
        try:
            # Mock run_pipeline to succeed
            monkeypatch.setattr(
                "saneless.worker.run_pipeline",
                lambda *_args, **_kwargs: _success_result(),
            )

            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()

            job = store.create_job("default", "Worker Test")
            worker.submit(job)

            # Wait for processing
            time.sleep(0.5)
            worker.stop()

            fetched = _get(store, job.id)
            assert fetched.state == JobState.DONE
        finally:
            store.close()

    def test_worker_sets_error_on_failure(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Pipeline raises exception -> job state is ERROR with message."""
        store = JobStore()
        try:

            def failing_pipeline(*_args: object, **_kwargs: object) -> ScanResult:
                msg = "Scanner on fire"
                raise RuntimeError(msg)

            monkeypatch.setattr(
                "saneless.worker.run_pipeline",
                failing_pipeline,
            )

            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()

            job = store.create_job("default", "Failing Test")
            worker.submit(job)

            time.sleep(0.5)
            worker.stop()

            fetched = _get(store, job.id)
            assert fetched.state == JobState.ERROR
            assert fetched.error is not None
            assert "Scanner on fire" in fetched.error
        finally:
            store.close()


def _success_result() -> ScanResult:
    """Return the ScanResult a successful pipeline run would produce."""
    return ScanResult(
        outcome=ScanOutcome.SUCCESS,
        pages_scanned=1,
        pages_removed=0,
        pages_uploaded=1,
    )


# How long the simulated pipeline below waits for a flip.  Generous against
# the half-second polls the tests use to reach AWAITING_FLIP, and far below
# pytest-timeout's 60 s ceiling, so a test that never answers fails as a
# timed-out job rather than as a SIGALRM traceback.
_MOCK_FLIP_TIMEOUT = 5.0


def _mock_manual_duplex_pipeline(
    _scanner: object,
    _paperless: object,
    _settings: object,
    request: PipelineRequest,
) -> ScanResult:
    """Simulate pipeline behavior for manual duplex tests."""
    if request.thumbnail_callback:
        request.thumbnail_callback("dGh1bWI=")  # base64 "thumb"
    coordinator = request.flip_coordinator
    if coordinator is not None:
        if request.status_callback:
            request.status_callback(PipelineEvent.AWAITING_FLIP)
        outcome = coordinator.wait_for_flip(_MOCK_FLIP_TIMEOUT)
        if outcome is FlipOutcome.ABORTED:
            msg = "Manual duplex scan aborted at the flip prompt"
            raise ScanError(msg)
        if outcome is FlipOutcome.TIMED_OUT:
            msg = "Manual duplex flip wait timed out"
            raise ScanError(msg)
    return _success_result()


def _captured_flip_coordinator(
    scanner: MagicMock,
    paperless: MagicMock,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    profile_name: str,
) -> object:
    """
    Run one job on ``profile_name`` and return the coordinator its request carried.

    The pipeline is replaced by a stub that records the request's
    ``flip_coordinator`` and returns at once, so only the worker's own decision
    about building flip machinery is observed.
    """
    captured: list[object] = []

    def capturing_pipeline(
        _scanner: object,
        _paperless: object,
        _settings: object,
        request: PipelineRequest,
    ) -> ScanResult:
        """Record the request's flip coordinator."""
        captured.append(request.flip_coordinator)
        return _success_result()

    monkeypatch.setattr("saneless.worker.run_pipeline", capturing_pipeline)

    store = JobStore()
    try:
        worker = ScanWorker(scanner, paperless, settings, store)
        worker.start()
        job = store.create_job(profile_name, "Coordinator Capture")
        worker.submit(job)
        for _ in range(100):
            if captured and _get(store, job.id).state in TERMINAL_STATES:
                break
            time.sleep(0.02)
        worker.stop()
    finally:
        store.close()

    assert len(captured) == 1
    return captured[0]


class TestScanWorkerManualDuplex:
    """Worker manual duplex coordination tests."""

    def test_worker_supplies_a_flip_coordinator_for_manual_duplex(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A manual duplex job waits on a coordinator that continue_flip answers."""
        default_settings.profiles["duplex"] = ProfileConfig(source="ADF Manual Duplex")

        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            _mock_manual_duplex_pipeline,
        )

        store = JobStore()
        try:
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()

            job = store.create_job("duplex", "Duplex Test")
            worker.submit(job)

            # Wait for the pipeline to reach AWAITING_FLIP
            for _ in range(50):
                time.sleep(0.05)
                fetched = _get(store, job.id)
                if fetched.state == JobState.AWAITING_FLIP:
                    break

            fetched = _get(store, job.id)
            assert fetched.state == JobState.AWAITING_FLIP

            # Continue the flip
            worker.continue_flip()

            time.sleep(0.5)
            worker.stop()

            fetched = _get(store, job.id)
            assert fetched.state == JobState.DONE
        finally:
            store.close()

    def test_worker_abort_flip(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """abort_flip() answers the coordinator ABORTED, so the pipeline raises."""
        default_settings.profiles["duplex"] = ProfileConfig(source="ADF Manual Duplex")

        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            _mock_manual_duplex_pipeline,
        )

        store = JobStore()
        try:
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()

            job = store.create_job("duplex", "Abort Test")
            worker.submit(job)

            # Wait for AWAITING_FLIP
            for _ in range(50):
                time.sleep(0.05)
                fetched = _get(store, job.id)
                if fetched.state == JobState.AWAITING_FLIP:
                    break

            worker.abort_flip()

            time.sleep(0.5)
            worker.stop()

            fetched = _get(store, job.id)
            assert fetched.state == JobState.ERROR
            assert fetched.error is not None
            assert "flip prompt" in fetched.error
        finally:
            store.close()

    def test_worker_stores_thumbnail(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Worker stores thumbnail on job via JobStore when thumbnail_callback fires."""
        default_settings.profiles["duplex"] = ProfileConfig(source="ADF Manual Duplex")

        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            _mock_manual_duplex_pipeline,
        )

        store = JobStore()
        try:
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()

            job = store.create_job("duplex", "Thumb Store Test")
            worker.submit(job)

            # Wait for AWAITING_FLIP (thumbnail should be stored by now)
            for _ in range(50):
                time.sleep(0.05)
                fetched = _get(store, job.id)
                if fetched.state == JobState.AWAITING_FLIP:
                    break

            fetched = _get(store, job.id)
            assert fetched.thumbnail == "dGh1bWI="

            worker.continue_flip()
            time.sleep(0.5)
            worker.stop()
        finally:
            store.close()

    def test_non_duplex_no_flip_coordinator(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Non-duplex jobs carry no flip coordinator."""
        captured_request: dict[str, object] = {}

        def capturing_pipeline(
            _scanner: object,
            _paperless: object,
            _settings: object,
            request: PipelineRequest,
        ) -> ScanResult:
            """Capture the request's flip coordinator."""
            captured_request["flip_coordinator"] = request.flip_coordinator
            return _success_result()

        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            capturing_pipeline,
        )

        store = JobStore()
        try:
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()

            job = store.create_job("default", "Simplex Test")
            worker.submit(job)

            time.sleep(0.5)
            worker.stop()

            assert captured_request["flip_coordinator"] is None
        finally:
            store.close()

    def test_flip_coordinator_follows_profile_duplex_on_a_plain_source(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ``duplex = "manual"`` profile gets a coordinator whatever its source."""
        default_settings.profiles["duplex"] = ProfileConfig(
            source="ADF Front", duplex="manual"
        )

        coordinator = _captured_flip_coordinator(
            mock_scanner, mock_paperless, default_settings, monkeypatch, "duplex"
        )

        assert isinstance(coordinator, WorkerFlipCoordinator)

    def test_a_manual_duplex_looking_source_alone_gets_no_coordinator(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An explicit ``duplex = "none"`` is not overruled by the source name."""
        default_settings.profiles["looks"] = ProfileConfig(
            source="ADF Manual Duplex", duplex="none"
        )

        coordinator = _captured_flip_coordinator(
            mock_scanner, mock_paperless, default_settings, monkeypatch, "looks"
        )

        assert coordinator is None

    def test_current_job_id_tracked(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Worker tracks current_job_id during processing."""
        default_settings.profiles["duplex"] = ProfileConfig(source="ADF Manual Duplex")

        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            _mock_manual_duplex_pipeline,
        )

        store = JobStore()
        try:
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()

            assert worker.current_job_id is None

            job = store.create_job("duplex", "Track Test")
            worker.submit(job)

            # Wait for AWAITING_FLIP
            for _ in range(50):
                time.sleep(0.05)
                if worker.current_job_id is not None:
                    break

            assert worker.current_job_id == job.id

            worker.continue_flip()
            time.sleep(0.5)
            worker.stop()

            assert worker.current_job_id is None
        finally:
            store.close()


class TestWorkerFlipCoordinator:
    """
    The web flip coordinator answers once, and its answer is final (D-16).

    Every wait here is bounded by ``0``: ``threading.Event().wait(0)`` returns
    in microseconds, so nothing in this class waits on a wall clock.
    """

    def test_continue_before_the_wait_resolves_continued(self) -> None:
        """A Continue that arrives first is the answer the wait returns."""
        coordinator = WorkerFlipCoordinator()
        coordinator.signal_continue()
        assert coordinator.wait_for_flip(0) is FlipOutcome.CONTINUED

    def test_abort_before_the_wait_resolves_aborted(self) -> None:
        """An Abort that arrives first is the answer the wait returns."""
        coordinator = WorkerFlipCoordinator()
        coordinator.signal_abort()
        assert coordinator.wait_for_flip(0) is FlipOutcome.ABORTED

    def test_a_later_abort_after_continue_is_dropped(self) -> None:
        """
        D-16: once Continue has answered, a late Abort changes nothing.

        Pass B has genuinely started by then, and stopping it mid-pass is
        Phase 29's HARD-02, so the honest answer is the first one.
        """
        coordinator = WorkerFlipCoordinator()
        coordinator.signal_continue()
        coordinator.signal_abort()
        assert coordinator.wait_for_flip(0) is FlipOutcome.CONTINUED

    def test_an_unanswered_wait_times_out(self) -> None:
        """Nothing signalled within the bound resolves TIMED_OUT (DPLX-05)."""
        coordinator = WorkerFlipCoordinator()
        assert coordinator.wait_for_flip(0) is FlipOutcome.TIMED_OUT

    def test_a_continue_after_a_timeout_does_not_revive_the_wait(self) -> None:
        """The timeout is an answer too; a Continue arriving after it is dropped."""
        coordinator = WorkerFlipCoordinator()
        assert coordinator.wait_for_flip(0) is FlipOutcome.TIMED_OUT
        coordinator.signal_continue()
        assert coordinator.wait_for_flip(0) is FlipOutcome.TIMED_OUT

    def test_a_continue_racing_the_timeout_is_honoured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A Continue landing between the wait expiring and the timeout's claim wins.

        The race is staged rather than timed: the event's ``wait`` is replaced
        by one that delivers Continue and then reports expiry, which is exactly
        the interleaving a real race produces.  The coordinator must return the
        answer already claimed, not overwrite it with ``TIMED_OUT``.
        """
        coordinator = WorkerFlipCoordinator()

        def _continue_then_expire(timeout: float | None = None) -> bool:
            """Deliver Continue, then report the wait as expired."""
            coordinator.signal_continue()
            return False

        monkeypatch.setattr(coordinator._event, "wait", _continue_then_expire)
        assert coordinator.wait_for_flip(0) is FlipOutcome.CONTINUED


class TestWorkerIntermediateStates:
    """Worker emits ASSEMBLING and UPLOADING intermediate states (UI-02)."""

    def test_worker_assembling_state(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Worker sets ASSEMBLING when pipeline emits 'Assembling PDF...'."""
        states_seen: list[str] = []
        store = JobStore()
        try:
            original_update = store.update_state

            def tracking_update(
                job_id: str,
                state: JobState,
                error: str | None = None,
                error_category: ErrorCategory | None = None,
            ) -> None:
                states_seen.append(state)
                original_update(
                    job_id, state, error=error, error_category=error_category
                )

            monkeypatch.setattr(store, "update_state", tracking_update)

            def fake_pipeline(
                _scanner: object,
                _paperless: object,
                _settings: object,
                request: PipelineRequest,
            ) -> ScanResult:
                if request.status_callback:
                    request.status_callback(PipelineEvent.ASSEMBLING)
                return _success_result()

            monkeypatch.setattr("saneless.worker.run_pipeline", fake_pipeline)
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()
            job = store.create_job("default", "Assembling Test")
            worker.submit(job)
            time.sleep(0.5)
            worker.stop()
            assert JobState.ASSEMBLING in states_seen
        finally:
            store.close()

    def test_worker_uploading_state(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Worker sets UPLOADING when pipeline emits 'Uploading to paperless-ngx...'."""
        states_seen: list[str] = []
        store = JobStore()
        try:
            original_update = store.update_state

            def tracking_update(
                job_id: str,
                state: JobState,
                error: str | None = None,
                error_category: ErrorCategory | None = None,
            ) -> None:
                states_seen.append(state)
                original_update(
                    job_id, state, error=error, error_category=error_category
                )

            monkeypatch.setattr(store, "update_state", tracking_update)

            def fake_pipeline(
                _scanner: object,
                _paperless: object,
                _settings: object,
                request: PipelineRequest,
            ) -> ScanResult:
                if request.status_callback:
                    request.status_callback(PipelineEvent.UPLOADING)
                return _success_result()

            monkeypatch.setattr("saneless.worker.run_pipeline", fake_pipeline)
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()
            job = store.create_job("default", "Uploading Test")
            worker.submit(job)
            time.sleep(0.5)
            worker.stop()
            assert JobState.UPLOADING in states_seen
        finally:
            store.close()


class TestWorkerErrorCategories:
    """Worker sets correct ErrorCategory for each exception type."""

    def test_feeder_empty_error_category(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """FeederEmptyError sets ErrorCategory.FEEDER."""

        def failing(*_a: object, **_k: object) -> ScanResult:
            msg = "No paper"
            raise FeederEmptyError(msg)

        monkeypatch.setattr("saneless.worker.run_pipeline", failing)
        store = JobStore()
        try:
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()
            job = store.create_job("default", "Feeder Test")
            worker.submit(job)
            time.sleep(0.5)
            worker.stop()
            fetched = _get(store, job.id)
            assert fetched.error_category == ErrorCategory.FEEDER
        finally:
            store.close()

    def test_scan_error_category(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ScanError sets ErrorCategory.SCANNER."""

        def failing(*_a: object, **_k: object) -> ScanResult:
            msg = "Scanner jam"
            raise ScanError(msg)

        monkeypatch.setattr("saneless.worker.run_pipeline", failing)
        store = JobStore()
        try:
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()
            job = store.create_job("default", "Scanner Test")
            worker.submit(job)
            time.sleep(0.5)
            worker.stop()
            fetched = _get(store, job.id)
            assert fetched.error_category == ErrorCategory.SCANNER
        finally:
            store.close()

    def test_paperless_error_category(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """PaperlessError sets ErrorCategory.UPLOAD."""

        def failing(*_a: object, **_k: object) -> ScanResult:
            msg = "Upload failed"
            raise PaperlessError(msg)

        monkeypatch.setattr("saneless.worker.run_pipeline", failing)
        store = JobStore()
        try:
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()
            job = store.create_job("default", "Upload Test")
            worker.submit(job)
            time.sleep(0.5)
            worker.stop()
            fetched = _get(store, job.id)
            assert fetched.error_category == ErrorCategory.UPLOAD
        finally:
            store.close()

    def test_config_error_category(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """ConfigError sets ErrorCategory.CONFIG."""

        def failing(*_a: object, **_k: object) -> ScanResult:
            msg = "Bad config"
            raise ConfigError(msg)

        monkeypatch.setattr("saneless.worker.run_pipeline", failing)
        store = JobStore()
        try:
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()
            job = store.create_job("default", "Config Test")
            worker.submit(job)
            time.sleep(0.5)
            worker.stop()
            fetched = _get(store, job.id)
            assert fetched.error_category == ErrorCategory.CONFIG
        finally:
            store.close()

    def test_unknown_error_category(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Generic Exception sets ErrorCategory.UNKNOWN."""

        def failing(*_a: object, **_k: object) -> ScanResult:
            msg = "Mystery"
            raise RuntimeError(msg)

        monkeypatch.setattr("saneless.worker.run_pipeline", failing)
        store = JobStore()
        try:
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()
            job = store.create_job("default", "Unknown Test")
            worker.submit(job)
            time.sleep(0.5)
            worker.stop()
            fetched = _get(store, job.id)
            assert fetched.error_category == ErrorCategory.UNKNOWN
        finally:
            store.close()


# How long the gated scanner below holds pass B if a test never releases it.
# A safety net only: every test here releases the gate itself.  It is kept
# below ScanWorker.stop()'s 5 s join and far below pytest-timeout's 60 s
# SIGALRM, so a broken test fails with its own wait_for_state message rather
# than a traceback delivered to the main thread.
_PASS_B_GATE_CEILING = 4.0

# The wait_for_state budget for the tests below: generous against a worker that
# only has to write a row, and far below pytest-timeout's 60 s ceiling.
_STATE_BUDGET = 2.0


def _inked_page() -> Image.Image:
    """
    Draw a page with enough ink that the real empty-page filter keeps it.

    Returns:
        A clearly non-blank RGB image.

    """
    page = Image.new("RGB", (120, 160), "white")
    ImageDraw.Draw(page).rectangle((10, 10, 110, 150), fill="black")
    return page


class _PassBGatedScanner(ScannerBackend):
    """
    A scanner whose second ``scan_pages`` call waits for the test to release it.

    Without the gate, pass B returns in microseconds and ``SCANNING_REVERSE`` is
    an instantaneous transition no poll can ever observe.  Holding pass B open
    on a test-held ``Event`` turns it into a state the store genuinely records
    for as long as the test needs to look at it.

    A concrete class rather than a ``MagicMock``, as this suite's fakes are: a
    subclass of the ABC is caught by the type checkers when the backend
    contract changes, and a mock is not.

    Attributes:
        release_pass_b: Set by the test to let pass B return.
        scan_calls: How many times ``scan_pages`` has been entered.

    """

    def __init__(self) -> None:
        """Start with pass B held."""
        self.release_pass_b = threading.Event()
        self.scan_calls = 0

    def get_devices(self) -> list[DeviceInfo]:
        """Report no devices; the worker never asks when a device is configured."""
        return []

    def get_capabilities(self, device_id: str) -> DeviceCapabilities:
        """
        Report a feeder-only device.

        Args:
            device_id: Ignored.

        Returns:
            Capabilities naming a single feeder source.

        """
        return DeviceCapabilities(sources=["ADF"], resolutions=[300], modes=["color"])

    def scan_pages(self, device_id: str, settings: ScanSettings) -> ScanBatch:
        """
        Return one inked page, holding the second and later calls on the gate.

        Args:
            device_id: Ignored.
            settings: Ignored.

        Returns:
            A batch of one non-blank page.

        """
        self.scan_calls += 1
        if self.scan_calls >= 2:
            self.release_pass_b.wait(_PASS_B_GATE_CEILING)
        return scan_batch([_inked_page()])


def _manual_duplex_settings(settings: Settings) -> Settings:
    """
    Add a ``duplex = "manual"`` profile named ``duplex`` to ``settings``.

    ``source`` is a real feeder name: it no longer selects the strategy.

    Args:
        settings: The fixture settings to extend in place.

    Returns:
        The same settings, for chaining.

    """
    settings.profiles["duplex"] = ProfileConfig(source="ADF", duplex="manual")
    return settings


class TestWorkerPassB:
    """
    Pass B through the real pipeline, observed from outside the worker (DPLX-06).

    These run the real ``run_pipeline``: the flip wait, the pass-B event and the
    state the worker persists for it are exactly what is under test, so stubbing
    the pipeline would stub the subject.  Only the scanner and the paperless
    client are fakes.
    """

    def test_pass_b_is_persisted_as_scanning_reverse(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        wait_for_state: Callable[..., Job],
    ) -> None:
        """While pass B is in flight the job row reads SCANNING_REVERSE."""
        scanner = _PassBGatedScanner()
        settings = _manual_duplex_settings(default_settings)
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, settings, store)
        try:
            worker.start()
            job = store.create_job("duplex", "Pass B Visible")
            worker.submit(job)

            wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
            worker.continue_flip()

            # Held open by the gate, so this is a state, not a blink.
            during = wait_for_state(
                store, job.id, JobState.SCANNING_REVERSE, _STATE_BUDGET
            )
            assert during.state is JobState.SCANNING_REVERSE

            scanner.release_pass_b.set()
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            scanner.release_pass_b.set()
            worker.stop()
            store.close()

        assert finished.state is JobState.DONE
        assert scanner.scan_calls == 2

    def test_abort_at_the_flip_prompt_fails_the_job_before_pass_b(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        wait_for_state: Callable[..., Job],
    ) -> None:
        """An Abort at the prompt ends the job ERROR, naming the flip prompt."""
        scanner = _PassBGatedScanner()
        settings = _manual_duplex_settings(default_settings)
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, settings, store)
        try:
            worker.start()
            job = store.create_job("duplex", "Aborted At Prompt")
            worker.submit(job)

            wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
            worker.abort_flip()
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            scanner.release_pass_b.set()
            worker.stop()
            store.close()

        assert finished.state is JobState.ERROR
        assert finished.error is not None
        assert "flip prompt" in finished.error
        # The abort was answered before pass B: the backs were never fed.
        assert scanner.scan_calls == 1

    def test_a_late_abort_after_continue_is_dropped(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        wait_for_state: Callable[..., Job],
    ) -> None:
        """
        D-16: an Abort arriving once pass B has begun changes nothing.

        The coordinator's first answer is final.  Pass B has genuinely started,
        stopping it mid-pass is Phase 29's HARD-02, and so the job completes.
        """
        scanner = _PassBGatedScanner()
        settings = _manual_duplex_settings(default_settings)
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, settings, store)
        try:
            worker.start()
            job = store.create_job("duplex", "Late Abort")
            worker.submit(job)

            wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
            worker.continue_flip()
            wait_for_state(store, job.id, JobState.SCANNING_REVERSE, _STATE_BUDGET)

            worker.abort_flip()
            scanner.release_pass_b.set()
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            scanner.release_pass_b.set()
            worker.stop()
            store.close()

        assert finished.state is JobState.DONE
        assert finished.error is None
        assert scanner.scan_calls == 2


class TestScanWorkerQueuing:
    """Worker sequential queuing tests."""

    def test_jobs_processed_sequentially(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Two submitted jobs are both processed to DONE (SCAN-11)."""
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            lambda *_args, **_kwargs: _success_result(),
        )

        store = JobStore()
        try:
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()

            job1 = store.create_job("default", "Job 1")
            job2 = store.create_job("default", "Job 2")
            worker.submit(job1)
            worker.submit(job2)

            time.sleep(1.0)
            worker.stop()

            assert _get(store, job1.id).state == JobState.DONE
            assert _get(store, job2.id).state == JobState.DONE
        finally:
            store.close()


class TestLazyAutoGenerate:
    """Worker lazy auto-profile generation tests."""

    @staticmethod
    def _mock_caps_scanner(mock_scanner: MagicMock) -> None:
        """Configure mock scanner with devices and capabilities."""
        mock_scanner.get_devices.return_value = [
            DeviceInfo(
                name="test:device:001",
                vendor="Test",
                model="Scanner",
                device_type="scanner",
            ),
        ]
        mock_scanner.get_capabilities.return_value = DeviceCapabilities(
            sources=["Flatbed", "ADF"],
            resolutions=[150, 300, 600],
            modes=["Color", "Gray"],
        )

    def test_lazy_auto_generate_on_bare_default(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Worker auto-generates profiles when only bare default exists."""
        self._mock_caps_scanner(mock_scanner)
        # Ensure settings have only bare default
        assert len(default_settings.profiles) == 1
        assert "default" in default_settings.profiles

        # Patch resolve_config_path to use tmp dir
        config_file = tmp_path / "saneless.toml"
        monkeypatch.setattr(
            "saneless.worker.resolve_config_path",
            lambda *_a, **_k: config_file,
        )
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            lambda *_args, **_kwargs: _success_result(),
        )

        store = JobStore()
        try:
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()

            job = store.create_job("default", "Auto Gen Test")
            worker.submit(job)

            time.sleep(0.5)
            worker.stop()

            # Profiles should now include generated ones
            assert len(default_settings.profiles) > 1
            assert "flatbed" in default_settings.profiles
        finally:
            store.close()

    def test_lazy_auto_generate_skips_customized(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Worker does not auto-generate when profiles are already customized."""
        # Add a custom profile so it's no longer bare default
        default_settings.profiles["photo"] = ProfileConfig(
            source="Flatbed", resolution=600, mode="Color"
        )

        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            lambda *_args, **_kwargs: _success_result(),
        )

        store = JobStore()
        try:
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()

            job = store.create_job("default", "Custom Test")
            worker.submit(job)

            time.sleep(0.5)
            worker.stop()

            # get_capabilities should not have been called
            mock_scanner.get_capabilities.assert_not_called()
        finally:
            store.close()

    def test_lazy_auto_generate_scanner_unreachable(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Worker falls back to bare default if scanner is unreachable."""
        mock_scanner.get_devices.side_effect = RuntimeError("Connection refused")

        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            lambda *_args, **_kwargs: _success_result(),
        )

        store = JobStore()
        try:
            with caplog.at_level(logging.WARNING):
                worker = ScanWorker(
                    mock_scanner, mock_paperless, default_settings, store
                )
                worker.start()

                job = store.create_job("default", "Unreachable Test")
                worker.submit(job)

                time.sleep(0.5)
                worker.stop()

            # Job should still complete (fallback to bare default)
            fetched = _get(store, job.id)
            assert fetched.state == JobState.DONE
            assert "scanner unreachable" in caplog.text
        finally:
            store.close()

    def test_lazy_auto_generate_only_once(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Worker only attempts auto-generation once across multiple jobs."""
        self._mock_caps_scanner(mock_scanner)

        config_file = tmp_path / "saneless.toml"
        monkeypatch.setattr(
            "saneless.worker.resolve_config_path",
            lambda *_a, **_k: config_file,
        )
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            lambda *_args, **_kwargs: _success_result(),
        )

        store = JobStore()
        try:
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()

            job1 = store.create_job("default", "Job 1")
            job2 = store.create_job("default", "Job 2")
            worker.submit(job1)
            worker.submit(job2)

            time.sleep(1.0)
            worker.stop()

            # get_capabilities called exactly once, not twice
            assert mock_scanner.get_capabilities.call_count == 1
        finally:
            store.close()


class TestWorkerEnumDispatch:
    """Worker dispatches on PipelineEvent enum, not strings."""

    def test_worker_status_cb_dispatches_on_pipeline_event(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Worker applies only ACTIVE_STATES from inside the pipeline callback."""
        states_seen: list[JobState] = []
        from_callback: list[JobState] = []
        finished_states: list[JobState] = []
        store = JobStore()
        try:
            original_update = store.update_state
            original_finish = store.finish_job

            def tracking_update(
                job_id: str,
                state: JobState,
                error: str | None = None,
                error_category: ErrorCategory | None = None,
            ) -> None:
                states_seen.append(state)
                original_update(
                    job_id, state, error=error, error_category=error_category
                )

            def tracking_finish(
                job_id: str,
                state: JobState,
                result: JobResult | None = None,
                error: str | None = None,
                error_category: ErrorCategory | None = None,
            ) -> None:
                finished_states.append(state)
                original_finish(
                    job_id,
                    state,
                    result=result,
                    error=error,
                    error_category=error_category,
                )

            monkeypatch.setattr(store, "update_state", tracking_update)
            monkeypatch.setattr(store, "finish_job", tracking_finish)

            def fake_pipeline(
                _scanner: object,
                _paperless: object,
                _settings: object,
                request: PipelineRequest,
            ) -> ScanResult:
                # Snapshot only what the callback itself writes, so the
                # worker's own pre-pipeline SCANNING and post-pipeline DONE
                # writes cannot be mistaken for callback output.
                start = len(states_seen)
                if request.status_callback:
                    for event in PipelineEvent:
                        request.status_callback(event)
                from_callback.extend(states_seen[start:])
                return _success_result()

            monkeypatch.setattr("saneless.worker.run_pipeline", fake_pipeline)
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()
            job = store.create_job("default", "Enum Dispatch Test")
            worker.submit(job)
            time.sleep(0.5)
            worker.stop()

            assert JobState.ASSEMBLING in states_seen

            # A job must never be marked DONE from inside run_pipeline: that
            # happens before the pipeline's temporary directory is cleaned up
            # and is visible to the one-second web poll.
            assert JobState.DONE not in from_callback

            # The states applied are exactly the in-flight ones, in the order
            # the events were emitted.  SCANNING_REVERSE is persisted as its own
            # busy state, so pass B takes the job out of AWAITING_FLIP (DPLX-06).
            # DONE (terminal) contributes nothing -- and neither does SCANNING,
            # which the worker persisted before starting the pipeline and which
            # run_pipeline merely re-announces as its first event.  Rewriting it
            # would blank error/error_category a second time and signal a
            # transition that did not occur.
            assert from_callback == [
                JobState.AWAITING_FLIP,
                JobState.SCANNING_REVERSE,
                JobState.ASSEMBLING,
                JobState.UPLOADING,
            ]
            assert JobState.SCANNING not in from_callback

            # DONE is still written by the worker, after run_pipeline returned
            # -- but through finish_job now, not update_state.  update_state's
            # SQL is an unconditional SET that never names the six result
            # columns (D-04), so the terminal write cannot go through it
            # without blanking the outcome and page counts it just recorded.
            assert JobState.DONE not in states_seen
            assert finished_states == [JobState.DONE]
        finally:
            store.close()

    def test_worker_prunes_after_job_completion(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Worker calls job_store.prune() after each job completes."""
        prune_calls: list[tuple[int, int]] = []
        store = JobStore()
        try:
            original_prune = store.prune

            def tracking_prune(max_age_days: int = 7, max_rows: int = 500) -> int:
                prune_calls.append((max_age_days, max_rows))
                return original_prune(max_age_days, max_rows)

            monkeypatch.setattr(store, "prune", tracking_prune)
            monkeypatch.setattr(
                "saneless.worker.run_pipeline",
                lambda *_args, **_kwargs: _success_result(),
            )

            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()
            job = store.create_job("default", "Prune Test")
            worker.submit(job)
            time.sleep(0.5)
            worker.stop()

            assert len(prune_calls) >= 1
            # Verify it used settings values
            assert prune_calls[0] == (
                default_settings.output.history_retention_days,
                default_settings.output.history_max_rows,
            )
        finally:
            store.close()


FALLBACK_WARNING = "Saved to the consume directory; title and tags not applied"
"""The warning a consume-directory delivery carries back to the worker."""

MISMATCH_WARNING = "Front pass had 5 pages, back pass had 4"
"""The warning a duplex page-count mismatch carries back to the worker."""


def _fallback_result() -> ScanResult:
    """Return the ScanResult a consume-directory delivery would produce."""
    return ScanResult(
        outcome=ScanOutcome.FALLBACK,
        pages_scanned=3,
        pages_removed=1,
        pages_uploaded=2,
        warning=FALLBACK_WARNING,
    )


def _mismatch_result() -> ScanResult:
    """Return the ScanResult a duplex page-count mismatch would produce."""
    return ScanResult(
        outcome=ScanOutcome.SUCCESS,
        pages_scanned=9,
        pages_removed=1,
        pages_uploaded=8,
        warning=MISMATCH_WARNING,
    )


class TestWorkerFinish:
    """The worker consumes the pipeline's ScanResult instead of discarding it."""

    # Every assertion here reads the persisted row back through the store
    # rather than inspecting mock call arguments.  The bug being fixed was a
    # discarded return value, and only the row proves it was consumed.

    def test_finish_passes_the_job_id_into_the_pipeline_request(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        wait_for_state: Callable[..., Job],
    ) -> None:
        """The assembled PDF is named from this job's id (OUTC-05)."""
        store = JobStore()
        captured: list[PipelineRequest] = []

        def capturing_pipeline(
            _scanner: object,
            _paperless: object,
            _settings: object,
            request: PipelineRequest,
        ) -> ScanResult:
            captured.append(request)
            return _success_result()

        try:
            monkeypatch.setattr("saneless.worker.run_pipeline", capturing_pipeline)
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()

            job = store.create_job("default", "Named Doc")
            worker.submit(job)
            wait_for_state(store, job.id, TERMINAL_STATES)
            worker.stop()

            assert len(captured) == 1
            assert captured[0].job_id == job.id
        finally:
            store.close()

    def test_finish_persists_done_for_a_success_outcome(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        wait_for_state: Callable[..., Job],
    ) -> None:
        """A SUCCESS outcome becomes DONE, with its counts (OUTC-01)."""
        store = JobStore()
        try:
            monkeypatch.setattr(
                "saneless.worker.run_pipeline",
                lambda *_args, **_kwargs: _success_result(),
            )
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()

            job = store.create_job("default", "Success Doc")
            worker.submit(job)
            finished = wait_for_state(store, job.id, TERMINAL_STATES)
            worker.stop()

            assert finished.state is JobState.DONE
            assert finished.outcome is ScanOutcome.SUCCESS
            assert finished.pages_scanned == 1
            assert finished.pages_removed == 0
            assert finished.pages_uploaded == 1
            assert finished.warning is None
            assert finished.error is None
        finally:
            store.close()

    def test_finish_persists_fallback_for_a_fallback_outcome(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        wait_for_state: Callable[..., Job],
    ) -> None:
        """A FALLBACK outcome becomes FALLBACK, never DONE (OUTC-02)."""
        store = JobStore()
        try:
            monkeypatch.setattr(
                "saneless.worker.run_pipeline",
                lambda *_args, **_kwargs: _fallback_result(),
            )
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()

            job = store.create_job("default", "Fallback Doc")
            worker.submit(job)
            finished = wait_for_state(store, job.id, TERMINAL_STATES)
            worker.stop()

            # FALLBACK, and therefore not DONE: pyrefly narrows the state
            # after this assertion, so spelling "not DONE" out as a second
            # assert is an always-true comparison it rejects.
            assert finished.state is JobState.FALLBACK
            assert finished.outcome is ScanOutcome.FALLBACK
            assert finished.warning == FALLBACK_WARNING
            assert finished.pages_scanned == 3
            assert finished.pages_removed == 1
            assert finished.pages_uploaded == 2
        finally:
            store.close()

    def test_finish_persists_a_duplex_mismatch_warning(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        wait_for_state: Callable[..., Job],
    ) -> None:
        """A mismatch warning survives alongside a SUCCESS outcome (OUTC-03)."""
        store = JobStore()
        try:
            monkeypatch.setattr(
                "saneless.worker.run_pipeline",
                lambda *_args, **_kwargs: _mismatch_result(),
            )
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()

            job = store.create_job("default", "Mismatch Doc")
            worker.submit(job)
            finished = wait_for_state(store, job.id, TERMINAL_STATES)
            worker.stop()

            assert finished.state is JobState.DONE
            assert finished.outcome is ScanOutcome.SUCCESS
            assert finished.warning == MISMATCH_WARNING
            assert finished.pages_scanned == 9
            assert finished.pages_removed == 1
            assert finished.pages_uploaded == 8
        finally:
            store.close()

    def test_finish_persists_error_and_leaves_the_counts_null(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        wait_for_state: Callable[..., Job],
    ) -> None:
        """A raising pipeline records ERROR and records no counts (OUTC-01)."""
        store = JobStore()

        def failing_pipeline(*_args: object, **_kwargs: object) -> ScanResult:
            msg = "Paperless said no"
            raise PaperlessError(msg)

        try:
            monkeypatch.setattr("saneless.worker.run_pipeline", failing_pipeline)
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()

            job = store.create_job("default", "Doomed Doc")
            worker.submit(job)
            finished = wait_for_state(store, job.id, TERMINAL_STATES)
            worker.stop()

            assert finished.state is JobState.ERROR
            assert finished.error == "Paperless said no"
            assert finished.error_category is ErrorCategory.UPLOAD
            # NULL, not 0.  A job that failed before the scanner opened has
            # not measured zero pages (Phase 22 D-08).
            assert finished.outcome is None
            assert finished.warning is None
            assert finished.pages_scanned is None
            assert finished.pages_removed is None
            assert finished.pages_uploaded is None
        finally:
            store.close()

    def test_finish_releases_a_poller_on_a_fallback_outcome(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        wait_for_state: Callable[..., Job],
    ) -> None:
        """
        A UI poller is released on FALLBACK as it is on DONE (OUTC-02).

        The web UI learns a job has ended only by re-reading its row, and stops
        polling once that row is no longer active.  So the row is what this
        waits on -- there is no worker-side signal to wait for any more.
        """
        store = JobStore()
        try:
            monkeypatch.setattr(
                "saneless.worker.run_pipeline",
                lambda *_args, **_kwargs: _fallback_result(),
            )
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()

            job = store.create_job("default", "Fallback Waiter")
            worker.submit(job)
            finished = wait_for_state(store, job.id, TERMINAL_STATES, 2.0)
            worker.stop()

            assert finished.state is JobState.FALLBACK
            assert not finished.is_active
        finally:
            store.close()

    def test_finish_prunes_history_after_a_fallback_outcome(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        wait_for_state: Callable[..., Job],
    ) -> None:
        """The finally block still runs on the new terminal path (STOR-05)."""
        store = JobStore()
        try:
            monkeypatch.setattr(
                "saneless.worker.run_pipeline",
                lambda *_args, **_kwargs: _fallback_result(),
            )
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()

            job = store.create_job("default", "Pruned Doc")
            worker.submit(job)
            wait_for_state(store, job.id, TERMINAL_STATES)
            worker.stop()

            assert worker.current_job_id is None
        finally:
            store.close()
