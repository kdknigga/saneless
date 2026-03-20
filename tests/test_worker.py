"""Tests for job model and worker thread."""

import time

from saneless.job import JobState, JobStore
from saneless.worker import ScanWorker


class TestJobStateTransitions:
    """Job state machine tests."""

    def test_job_state_transitions(self):
        """Job starts as PENDING and transitions through all active states."""
        store = JobStore()
        try:
            job = store.create_job("default", "Test Doc")
            assert job.state == JobState.PENDING

            store.update_state(job.id, JobState.SCANNING)
            assert store.get_job(job.id).state == JobState.SCANNING

            store.update_state(job.id, JobState.ASSEMBLING)
            assert store.get_job(job.id).state == JobState.ASSEMBLING

            store.update_state(job.id, JobState.UPLOADING)
            assert store.get_job(job.id).state == JobState.UPLOADING

            store.update_state(job.id, JobState.DONE)
            assert store.get_job(job.id).state == JobState.DONE
        finally:
            store.close()

    def test_job_state_error(self):
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
                fetched = store.get_job(job.id)
                assert fetched.state == JobState.ERROR
                assert fetched.error == "Something broke"
        finally:
            store.close()


class TestJobStore:
    """JobStore persistence tests."""

    def test_job_store_create_and_get(self):
        """JobStore.create_job returns Job with UUID, get_job returns same."""
        store = JobStore()
        try:
            job = store.create_job("default", "My Document", tags=[1, 2])
            assert job.id is not None
            assert len(job.id) > 0

            fetched = store.get_job(job.id)
            assert fetched is not None
            assert fetched.id == job.id
            assert fetched.profile == "default"
            assert fetched.title == "My Document"
            assert fetched.tags == [1, 2]
        finally:
            store.close()

    def test_job_store_update_state(self):
        """update_state changes the job state."""
        store = JobStore()
        try:
            job = store.create_job("default", "Test")
            store.update_state(job.id, JobState.SCANNING)
            fetched = store.get_job(job.id)
            assert fetched.state == JobState.SCANNING
        finally:
            store.close()

    def test_job_store_sqlite_persistence(self, tmp_path):
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


class TestScanWorker:
    """Worker thread tests."""

    def test_worker_starts_and_stops(
        self, mock_scanner, mock_paperless, default_settings
    ):
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
        self, mock_scanner, mock_paperless, default_settings, monkeypatch
    ):
        """Submit job to worker -> job reaches DONE state."""
        store = JobStore()
        try:
            # Mock run_pipeline to succeed
            monkeypatch.setattr(
                "saneless.worker.run_pipeline",
                lambda *_args, **_kwargs: {"status": "SUCCESS"},
            )

            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()

            job = store.create_job("default", "Worker Test")
            worker.submit(job)

            # Wait for processing
            time.sleep(0.5)
            worker.stop()

            fetched = store.get_job(job.id)
            assert fetched.state == JobState.DONE
        finally:
            store.close()

    def test_worker_sets_error_on_failure(
        self, mock_scanner, mock_paperless, default_settings, monkeypatch
    ):
        """Pipeline raises exception -> job state is ERROR with message."""
        store = JobStore()
        try:

            def failing_pipeline(*_args, **_kwargs):
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

            fetched = store.get_job(job.id)
            assert fetched.state == JobState.ERROR
            assert "Scanner on fire" in fetched.error
        finally:
            store.close()
