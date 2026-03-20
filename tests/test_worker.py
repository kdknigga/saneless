"""Tests for job model and worker thread."""

import time

from saneless.exceptions import ScanError
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


class TestJobStateAwaitingFlip:
    """AWAITING_FLIP state tests."""

    def test_awaiting_flip_exists(self):
        """JobState.AWAITING_FLIP exists and equals 'AWAITING_FLIP'."""
        assert JobState.AWAITING_FLIP == "AWAITING_FLIP"
        assert JobState.AWAITING_FLIP.value == "AWAITING_FLIP"


class TestJobThumbnail:
    """Job thumbnail field tests."""

    def test_thumbnail_defaults_none(self):
        """Job.thumbnail field defaults to None."""
        from saneless.job import Job

        job = Job(id="test", profile="default", title="Test")
        assert job.thumbnail is None

    def test_jobstore_persists_thumbnail(self):
        """JobStore persists and retrieves thumbnail field."""
        store = JobStore()
        try:
            job = store.create_job("default", "Thumb Test")
            store.update_thumbnail(job.id, "base64data")
            fetched = store.get_job(job.id)
            assert fetched.thumbnail == "base64data"
        finally:
            store.close()


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


def _mock_manual_duplex_pipeline(_scanner, _paperless, _settings, request):
    """Simulate pipeline behavior for manual duplex tests."""
    if request.thumbnail_callback:
        request.thumbnail_callback("dGh1bWI=")  # base64 "thumb"
    if request.flip_event:
        if request.status_callback:
            request.status_callback("Awaiting flip...")
        request.flip_event.wait()
        if request.abort_event and request.abort_event.is_set():
            msg = "Manual duplex scan cancelled by user"
            raise ScanError(msg)
    return {"status": "SUCCESS"}


class TestScanWorkerManualDuplex:
    """Worker manual duplex coordination tests."""

    def test_worker_creates_flip_event_for_manual_duplex(
        self, mock_scanner, mock_paperless, default_settings, monkeypatch
    ):
        """Worker creates flip_event and abort_event for manual duplex jobs."""
        from saneless.config import ProfileConfig

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
                fetched = store.get_job(job.id)
                if fetched.state == JobState.AWAITING_FLIP:
                    break

            fetched = store.get_job(job.id)
            assert fetched.state == JobState.AWAITING_FLIP

            # Continue the flip
            worker.continue_flip()

            time.sleep(0.5)
            worker.stop()

            fetched = store.get_job(job.id)
            assert fetched.state == JobState.DONE
        finally:
            store.close()

    def test_worker_abort_flip(
        self, mock_scanner, mock_paperless, default_settings, monkeypatch
    ):
        """abort_flip() sets abort and flip events, causing pipeline to raise."""
        from saneless.config import ProfileConfig

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
                fetched = store.get_job(job.id)
                if fetched.state == JobState.AWAITING_FLIP:
                    break

            worker.abort_flip()

            time.sleep(0.5)
            worker.stop()

            fetched = store.get_job(job.id)
            assert fetched.state == JobState.ERROR
            assert "cancelled by user" in fetched.error
        finally:
            store.close()

    def test_worker_stores_thumbnail(
        self, mock_scanner, mock_paperless, default_settings, monkeypatch
    ):
        """Worker stores thumbnail on job via JobStore when thumbnail_callback fires."""
        from saneless.config import ProfileConfig

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
                fetched = store.get_job(job.id)
                if fetched.state == JobState.AWAITING_FLIP:
                    break

            fetched = store.get_job(job.id)
            assert fetched.thumbnail == "dGh1bWI="

            worker.continue_flip()
            time.sleep(0.5)
            worker.stop()
        finally:
            store.close()

    def test_non_duplex_no_flip_events(
        self, mock_scanner, mock_paperless, default_settings, monkeypatch
    ):
        """Non-duplex jobs do not create flip/abort events."""
        captured_request = {}

        def capturing_pipeline(_scanner, _paperless, _settings, request):
            captured_request["flip_event"] = request.flip_event
            captured_request["abort_event"] = request.abort_event
            return {"status": "SUCCESS"}

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

            assert captured_request["flip_event"] is None
            assert captured_request["abort_event"] is None
        finally:
            store.close()

    def test_current_job_id_tracked(
        self, mock_scanner, mock_paperless, default_settings, monkeypatch
    ):
        """Worker tracks current_job_id during processing."""
        from saneless.config import ProfileConfig

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


class TestScanWorkerQueuing:
    """Worker sequential queuing tests."""

    def test_jobs_processed_sequentially(
        self, mock_scanner, mock_paperless, default_settings, monkeypatch
    ):
        """Two submitted jobs are both processed to DONE (SCAN-11)."""
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            lambda *_args, **_kwargs: {"status": "SUCCESS"},
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

            assert store.get_job(job1.id).state == JobState.DONE
            assert store.get_job(job2.id).state == JobState.DONE
        finally:
            store.close()
