"""Tests for job model and worker thread."""

from __future__ import annotations

import ast
import errno
import logging
import os
import sqlite3
import threading
import time
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from PIL import Image, ImageDraw

from saneless import auto_profiles as auto_profiles_module
from saneless import preservation as preservation_module
from saneless import worker as worker_module
from saneless.auto_profiles import (
    ProfileWriteResult,
    generate_profiles,
    is_bare_default,
)
from saneless.checks import CheckContext, CheckKey, CheckState, run_checks
from saneless.config import (
    CONFIG_FILENAME,
    LEGACY_CONFIG_FILENAME,
    ProfileConfig,
    Settings,
    config_search_paths,
    discover_config,
)
from saneless.exceptions import (
    AllPagesBlankError,
    ConfigError,
    FeederEmptyError,
    PaperlessError,
    PdfError,
    ScanCancelledError,
    ScanError,
    ScanInterrupted,
)
from saneless.job import (
    REJECTED_HISTORY_ROWS,
    ErrorCategory,
    Job,
    JobResult,
    JobState,
    JobStore,
)
from saneless.paperless import ApiDelivery, UploadResult
from saneless.pipeline import DeviceMemory, PipelineEvent, ScanResult
from saneless.scanner.base import DeviceCapabilities, DeviceInfo, ScanBatch
from saneless.text_safety import has_control_characters
from saneless.vocabulary import (
    HIDDEN_PRESERVED_ERROR,
    RESTART_REASON,
    RESTART_UPLOADING_REASON,
    TERMINAL_STATES,
    FlipOutcome,
    ProfileStorage,
    ScanOutcome,
    SubmitResult,
    WorkerHealth,
    classify_error,
    restart_category,
    restart_error,
)
from saneless.web.cache import CachedMetadataLookup, MetadataCache
from saneless.web.job_view import build_job_view
from saneless.worker import ScanWorker, WorkerFlipCoordinator
from tests.conftest import (
    StubScannerBackend,
    build_settings,
    poll_until,
    quiet_window,
    scan_batch,
    wait_for_state,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from unittest.mock import MagicMock

    from saneless.pipeline import PipelineRequest
    from saneless.scanner.base import PageSink, ScanSettings


@pytest.fixture(autouse=True)
def _mock_scanner_reports_no_devices(mock_scanner: MagicMock) -> None:
    """
    Answer the shared mock scanner's ``get_devices`` with a deliberate ``[]``.

    Every worker started over ``default_settings`` -- a bare default profile
    set -- runs startup profile generation first.  Left to itself the
    ``MagicMock`` hands back a truthy mock and generation fails somewhere inside
    ``generate_profiles``, so whether profiles were swapped under a test would
    hang on how that function treats a mock.  "No scanners found" is a real
    answer instead: generation logs its WARNING and keeps the bare default.
    ``TestStartupProfileGeneration`` overrides this per test.
    """
    mock_scanner.get_devices.return_value = []


def _get(store: JobStore, job_id: str) -> Job:
    """Retrieve a job, asserting it exists (narrows Job | None to Job)."""
    fetched = store.get_job(job_id)
    assert fetched is not None
    return fetched


class TestJobStateTransitions:
    """A job moves through its states and reads back as it was written."""

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
    """The job store creates, updates and persists job rows."""

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
    """A job can wait at the flip prompt in its own state."""

    def test_awaiting_flip_exists(self) -> None:
        """JobState.AWAITING_FLIP exists and equals 'AWAITING_FLIP'."""
        assert JobState.AWAITING_FLIP == "AWAITING_FLIP"
        assert JobState.AWAITING_FLIP.value == "AWAITING_FLIP"


class TestJobThumbnail:
    """A job carries an optional thumbnail that the store persists."""

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
    """The worker runs jobs on its own thread and records how each ends."""

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
        """A job submitted to the worker reaches DONE."""
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

            # stop() abandons unstarted work, so wait for the row itself.
            wait_for_state(store, job.id, TERMINAL_STATES)
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
        """A pipeline that raises ends the job ERROR with the exception's message."""
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

            wait_for_state(store, job.id, TERMINAL_STATES)
            worker.stop()

            fetched = _get(store, job.id)
            assert fetched.state == JobState.ERROR
            assert fetched.error is not None
            assert "Scanner on fire" in fetched.error
        finally:
            store.close()

    def test_device_change_memory_is_shared_by_one_workers_jobs(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Every job of one worker carries the same device memory.

        The pipeline compares each job's auto-detected device with the last
        one through that object, so a fresh one per job would never see a
        change and a module-level one would leak between workers.
        """
        captured: list[object] = []

        def capturing_pipeline(
            _scanner: object,
            _paperless: object,
            _settings: object,
            request: PipelineRequest,
        ) -> ScanResult:
            """Record the request's device memory."""
            captured.append(request.device_memory)
            return _success_result()

        monkeypatch.setattr("saneless.worker.run_pipeline", capturing_pipeline)
        store = JobStore()
        try:
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()
            first = store.create_job("default", "First")
            worker.submit(first)
            wait_for_state(store, first.id, TERMINAL_STATES)
            second = store.create_job("default", "Second")
            worker.submit(second)
            wait_for_state(store, second.id, TERMINAL_STATES)
            worker.stop()
        finally:
            store.close()

        assert len(captured) == 2
        assert isinstance(captured[0], DeviceMemory)
        assert captured[0] is captured[1]


def _success_result() -> ScanResult:
    """Return the ScanResult a successful pipeline run would produce."""
    return ScanResult(
        outcome=ScanOutcome.SUCCESS,
        pages_scanned=1,
        pages_removed=0,
        pages_uploaded=1,
    )


class TestJobMetadataReachesTheRequest:
    """The job row's metadata is the scan's, whatever the profile defaults say."""

    def test_the_row_is_the_answer_not_the_profile(
        self,
        tmp_path: Path,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A row holding tag 3 and no correspondent runs with exactly those.

        The scan route resolved the profile's defaults when it wrote the row,
        so by the time the worker runs the job they have been applied or
        answered away, and applying them again would re-tag a submit whose
        operator cleared them.
        """
        settings = build_settings(
            tmp_path,
            profiles={
                "default": ProfileConfig.model_validate(
                    {"default_tags": [5, 9], "default_correspondent": 4}
                )
            },
        )
        captured: list[PipelineRequest] = []

        def capturing_pipeline(
            _scanner: object,
            _paperless: object,
            _settings: object,
            request: PipelineRequest,
        ) -> ScanResult:
            """Record the request."""
            captured.append(request)
            return _success_result()

        monkeypatch.setattr("saneless.worker.run_pipeline", capturing_pipeline)
        store = JobStore()
        try:
            worker = ScanWorker(mock_scanner, mock_paperless, settings, store)
            worker.start()
            job = store.create_job("default", "Row", tags=[3], correspondent=None)
            worker.submit(job)
            wait_for_state(store, job.id, TERMINAL_STATES)
            worker.stop()
        finally:
            store.close()

        assert len(captured) == 1
        assert captured[0].tags == [3]
        assert captured[0].correspondent is None
        assert captured[0].job_id == job.id

    def test_the_worker_lookup_reaches_every_request(
        self,
        tmp_path: Path,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The lookup the web app built is the one each job checks its ids with."""
        lookup = CachedMetadataLookup(MetadataCache(ttl=60), mock_paperless)
        captured: list[PipelineRequest] = []

        def capturing_pipeline(
            _scanner: object,
            _paperless: object,
            _settings: object,
            request: PipelineRequest,
        ) -> ScanResult:
            """Record the request."""
            captured.append(request)
            return _success_result()

        monkeypatch.setattr("saneless.worker.run_pipeline", capturing_pipeline)
        store = JobStore()
        try:
            worker = ScanWorker(
                mock_scanner,
                mock_paperless,
                build_settings(tmp_path),
                store,
                metadata_lookup=lookup,
            )
            worker.start()
            job = store.create_job("default", "Row", tags=[3], correspondent=None)
            worker.submit(job)
            wait_for_state(store, job.id, TERMINAL_STATES)
            worker.stop()
        finally:
            store.close()

        assert len(captured) == 1
        assert captured[0].metadata_lookup is lookup


def _pipeline_request_builders() -> list[tuple[str, str]]:
    """
    Find every construction of ``PipelineRequest`` in the saneless package.

    The walk is over the syntax tree, so a docstring or comment naming the
    class is not mistaken for a call.

    Returns:
        One ``(file name, enclosing function name)`` pair per construction,
        with ``""`` for one outside any function.

    """
    package = Path(worker_module.__file__).parent
    sites: list[tuple[str, str]] = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner: dict[int, str] = {}
        # ast.walk visits an outer function before the ones nested in it, so
        # the innermost enclosing function is the last one written here.
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                for inner in ast.walk(node):
                    owner[id(inner)] = node.name
        sites.extend(
            (path.relative_to(package).as_posix(), owner.get(id(node), ""))
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "PipelineRequest"
        )
    return sites


@pytest.mark.source_structure
def test_every_pipeline_request_comes_from_the_one_builder() -> None:
    """
    The worker and the CLI build their requests one way.

    Two builders are how the web and the command line came to file the same
    profile's scan with different metadata; with one, they cannot drift.
    """
    assert _pipeline_request_builders() == [("pipeline.py", "build_pipeline_request")]


# How long the simulated pipeline below waits for a flip.  Generous against
# the half-second polls the tests use to reach AWAITING_FLIP, and far below
# pytest-timeout's 60 s ceiling, so a test that never answers fails as a
# timed-out job rather than as a SIGALRM traceback.
_MOCK_FLIP_TIMEOUT = 5.0

# The text a flip-prompt cancel carries into the job row: the real
# pipeline's message for an operator's abort, so the stubs raise it verbatim.
_CANCEL_MESSAGE = "Manual duplex scan cancelled at the flip prompt"


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
            # As the real pipeline does for an operator's abort.
            raise ScanCancelledError(_CANCEL_MESSAGE)
        if outcome is FlipOutcome.TIMED_OUT:
            msg = "Manual duplex flip wait timed out"
            raise ScanError(msg)
    return _success_result()


def _assert_cancelled_at_the_flip_prompt(
    finished: Job, caplog: pytest.LogCaptureFixture
) -> None:
    """
    Assert ``finished`` ended as an operator's cancel at the flip prompt.

    CANCELLED with no category and the flip-prompt message, logged once at
    INFO without a traceback.  Call only after the worker has been stopped,
    which joins its thread, so the ending's log record exists.
    """
    assert finished.state is JobState.CANCELLED
    assert finished.error_category is None
    assert finished.error is not None
    assert "flip prompt" in finished.error
    cancelled = _worker_records(caplog, logging.INFO, f"Job {finished.id} cancelled")
    assert len(cancelled) == 1
    assert cancelled[0].exc_info is None


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
        assert poll_until(
            lambda: bool(captured) and _get(store, job.id).state in TERMINAL_STATES,
            2.0,
        ), "the job never finished with its flip coordinator captured"
        worker.stop()
    finally:
        store.close()

    assert len(captured) == 1
    return captured[0]


class TestScanWorkerManualDuplex:
    """A manual duplex job's flip prompt is answered through the worker."""

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

            fetched = wait_for_state(
                store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET
            )
            assert fetched.state == JobState.AWAITING_FLIP

            # Continue the flip
            worker.continue_flip(job.id)

            wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
            worker.stop()

            fetched = _get(store, job.id)
            assert fetched.state == JobState.DONE
        finally:
            store.close()

    def test_worker_abort_flip(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        abort_flip(job.id) answers ABORTED, and the job ends CANCELLED.

        The Abort button is the operator choosing to stop, so the job is a
        cancel rather than a scanner failure.
        """
        caplog.set_level(logging.INFO, logger="saneless.worker")
        # worker_for builds over this same fixture instance.
        default_settings.profiles["duplex"] = ProfileConfig(source="ADF Manual Duplex")

        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            _mock_manual_duplex_pipeline,
        )

        store = JobStore()
        try:
            worker = worker_for(store)
            worker.start()

            job = store.create_job("duplex", "Abort Test")
            worker.submit(job)

            wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
            worker.abort_flip(job.id)

            wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
            worker.stop()

            fetched = _get(store, job.id)
        finally:
            store.close()

        _assert_cancelled_at_the_flip_prompt(fetched, caplog)

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

            # Thumbnail should be stored by the time AWAITING_FLIP is written.
            fetched = wait_for_state(
                store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET
            )
            assert fetched.thumbnail == "dGh1bWI="

            worker.continue_flip(job.id)
            wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
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

            wait_for_state(store, job.id, TERMINAL_STATES)
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

            assert poll_until(lambda: worker.current_job_id is not None, 2.5), (
                "the worker never picked up the job"
            )
            assert worker.current_job_id == job.id

            # Continue counts only at the flip prompt, so reach it first.
            wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
            worker.continue_flip(job.id)
            wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
            worker.stop()

            assert worker.current_job_id is None
        finally:
            store.close()

    def test_the_coordinator_is_armed_before_awaiting_flip_is_persisted(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Whoever reads AWAITING_FLIP from the store finds the prompt armed.

        The status poll renders Continue and Abort from the persisted row, so
        the coordinator must already accept an answer by the time that row is
        written.  The store's ``update_state`` is wrapped to record, at the
        moment AWAITING_FLIP is written, whether the worker's coordinator is
        armed.
        """
        default_settings.profiles["duplex"] = ProfileConfig(
            source="ADF", duplex="manual"
        )
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            _mock_manual_duplex_pipeline,
        )

        armed_at_persist: list[bool] = []
        store = JobStore()
        worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
        original_update = store.update_state

        def recording_update(
            job_id: str,
            state: JobState,
            error: str | None = None,
            error_category: ErrorCategory | None = None,
        ) -> None:
            """Record the coordinator's arming as AWAITING_FLIP is written."""
            if state is JobState.AWAITING_FLIP:
                coordinator = worker._flip_coordinator
                armed_at_persist.append(coordinator is not None and coordinator.armed)
            original_update(job_id, state, error=error, error_category=error_category)

        monkeypatch.setattr(store, "update_state", recording_update)
        try:
            worker.start()
            job = store.create_job("duplex", "Armed Before Persist")
            worker.submit(job)
            wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
            worker.continue_flip(job.id)
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            worker.stop()
            store.close()

        assert armed_at_persist == [True]
        assert finished.state is JobState.DONE


class TestWorkerFlipCoordinator:
    """
    The web flip coordinator answers once, and its answer is final.

    It is also bound to one job and accepts an answer only once armed, which
    the worker does when the job announces ``AWAITING_FLIP``.

    Every wait here is bounded by ``0``: an Event wait with a zero timeout
    returns in microseconds, so nothing in this class waits on a wall clock.
    """

    def test_the_coordinator_is_bound_to_one_job(self) -> None:
        """A coordinator carries the id of the job whose flip it answers."""
        assert WorkerFlipCoordinator("job-1").job_id == "job-1"

    def test_continue_at_the_flip_prompt_resolves_continued(self) -> None:
        """A Continue that arrives first at the prompt is the answer returned."""
        coordinator = WorkerFlipCoordinator("job-1")
        coordinator.arm()
        assert coordinator.signal_continue() is True
        assert coordinator.wait_for_flip(0) is FlipOutcome.CONTINUED

    def test_abort_at_the_flip_prompt_resolves_aborted(self) -> None:
        """An Abort that arrives first at the prompt is the answer returned."""
        coordinator = WorkerFlipCoordinator("job-1")
        coordinator.arm()
        assert coordinator.signal_abort() is True
        assert coordinator.wait_for_flip(0) is FlipOutcome.ABORTED

    def test_a_later_abort_after_continue_is_dropped(self) -> None:
        """
        Once Continue has answered, a late Abort changes nothing.

        Pass B has genuinely started by then, and an Abort does not stop a
        pass midway, so the honest answer is the first one.
        """
        coordinator = WorkerFlipCoordinator("job-1")
        coordinator.arm()
        assert coordinator.signal_continue() is True
        assert coordinator.signal_abort() is False
        assert coordinator.wait_for_flip(0) is FlipOutcome.CONTINUED

    def test_an_unarmed_signal_is_dropped(self) -> None:
        """
        A signal before the flip prompt exists claims nothing.

        Both signals report that they were dropped and leave the answer slot
        empty, so a click meant for an earlier prompt cannot pre-answer this one.
        """
        coordinator = WorkerFlipCoordinator("job-1")
        assert coordinator.armed is False
        assert coordinator.signal_continue() is False
        assert coordinator.signal_abort() is False
        assert coordinator.answer is None

    def test_an_early_signal_is_dropped_not_queued(self) -> None:
        """
        An Abort sent during pass A is not held back for the prompt.

        Once the prompt is armed, the operator's real Continue is the answer.
        """
        coordinator = WorkerFlipCoordinator("job-1")
        assert coordinator.signal_abort() is False
        coordinator.arm()
        assert coordinator.signal_continue() is True
        assert coordinator.wait_for_flip(0) is FlipOutcome.CONTINUED

    def test_the_first_armed_answer_wins_and_later_ones_are_dropped(self) -> None:
        """The first armed signal claims the answer; every later one is False."""
        coordinator = WorkerFlipCoordinator("job-1")
        coordinator.arm()
        assert coordinator.signal_abort() is True
        assert coordinator.signal_abort() is False
        assert coordinator.signal_continue() is False
        assert coordinator.answer is FlipOutcome.ABORTED
        assert coordinator.wait_for_flip(0) is FlipOutcome.ABORTED

    def test_arming_is_idempotent(self) -> None:
        """Arming twice is the same as arming once."""
        coordinator = WorkerFlipCoordinator("job-1")
        coordinator.arm()
        coordinator.arm()
        assert coordinator.armed is True
        assert coordinator.signal_continue() is True

    def test_an_unanswered_armed_wait_times_out(self) -> None:
        """Nothing signalled within the bound resolves TIMED_OUT."""
        coordinator = WorkerFlipCoordinator("job-1")
        coordinator.arm()
        assert coordinator.wait_for_flip(0) is FlipOutcome.TIMED_OUT

    def test_an_unanswered_unarmed_wait_times_out(self) -> None:
        """A wait on a never-armed coordinator still times out."""
        coordinator = WorkerFlipCoordinator("job-1")
        assert coordinator.wait_for_flip(0) is FlipOutcome.TIMED_OUT

    def test_waiting_arms_the_coordinator(self) -> None:
        """
        ``wait_for_flip`` arms the coordinator itself, as a backstop.

        A caller that never announces ``AWAITING_FLIP`` still gets a
        coordinator its operator can answer.
        """
        coordinator = WorkerFlipCoordinator("job-1")
        coordinator.wait_for_flip(0)
        assert coordinator.armed is True

    def test_a_continue_after_a_timeout_does_not_revive_the_wait(self) -> None:
        """The timeout is an answer too; a Continue arriving after it is dropped."""
        coordinator = WorkerFlipCoordinator("job-1")
        assert coordinator.wait_for_flip(0) is FlipOutcome.TIMED_OUT
        assert coordinator.signal_continue() is False
        assert coordinator.wait_for_flip(0) is FlipOutcome.TIMED_OUT

    def test_a_continue_racing_the_timeout_is_honoured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A Continue landing between the wait expiring and the timeout's claim wins.

        The race is staged rather than timed: the answer slot's ``wait`` is
        replaced by one that delivers Continue and then returns as an expired
        wait would, which is exactly the interleaving a real race produces.  The
        coordinator must return the answer already claimed, not overwrite it
        with ``TIMED_OUT``.
        """
        coordinator = WorkerFlipCoordinator("job-1")
        coordinator.arm()

        def _continue_then_expire(timeout: float) -> None:
            """Deliver Continue, then return as an expired wait does."""
            coordinator.signal_continue()

        monkeypatch.setattr(coordinator._slot, "wait", _continue_then_expire)
        assert coordinator.wait_for_flip(0) is FlipOutcome.CONTINUED

    def test_shutdown_answers_even_an_unarmed_wait_with_interrupted(self) -> None:
        """
        A stop claimed during pass A is the answer once the prompt opens.

        Shutdown arms the coordinator itself, so its answer lands whether the
        job is at the prompt or still scanning pass A, and it is its own
        outcome rather than an operator's Abort.
        """
        coordinator = WorkerFlipCoordinator("job-1")
        assert coordinator.interrupt_for_shutdown() is True
        assert coordinator.armed is True
        assert coordinator.answer is FlipOutcome.INTERRUPTED
        assert coordinator.wait_for_flip(0) is FlipOutcome.INTERRUPTED

    def test_an_operator_answer_already_claimed_outlasts_shutdown(self) -> None:
        """
        Shutdown never overwrites an answer the operator already gave.

        An Abort claimed first stays an Abort, so the job is recorded as the
        operator's cancel and keeps nothing, as the operator chose.
        """
        coordinator = WorkerFlipCoordinator("job-1")
        coordinator.arm()
        assert coordinator.signal_abort() is True
        assert coordinator.interrupt_for_shutdown() is False
        assert coordinator.wait_for_flip(0) is FlipOutcome.ABORTED


class TestWorkerIntermediateStates:
    """The worker records ASSEMBLING and UPLOADING as the pipeline reaches them."""

    def test_worker_assembling_state(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Worker sets ASSEMBLING when the pipeline emits PipelineEvent.ASSEMBLING."""
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
            wait_for_state(store, job.id, TERMINAL_STATES)
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
        """Worker sets UPLOADING when the pipeline emits PipelineEvent.UPLOADING."""
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
            wait_for_state(store, job.id, TERMINAL_STATES)
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
            wait_for_state(store, job.id, TERMINAL_STATES)
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
            wait_for_state(store, job.id, TERMINAL_STATES)
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
            wait_for_state(store, job.id, TERMINAL_STATES)
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
            wait_for_state(store, job.id, TERMINAL_STATES)
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
            wait_for_state(store, job.id, TERMINAL_STATES)
            worker.stop()
            fetched = _get(store, job.id)
            assert fetched.error_category == ErrorCategory.UNKNOWN
        finally:
            store.close()

    def test_all_pages_blank_error_category(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """AllPagesBlankError sets ErrorCategory.ALL_BLANK, never SCANNER."""

        def failing(*_a: object, **_k: object) -> ScanResult:
            msg = "All pages were blank"
            raise AllPagesBlankError(msg)

        monkeypatch.setattr("saneless.worker.run_pipeline", failing)
        store = JobStore()
        try:
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()
            job = store.create_job("default", "Blank Test")
            worker.submit(job)
            wait_for_state(store, job.id, TERMINAL_STATES)
            worker.stop()
            fetched = _get(store, job.id)
            assert fetched.error_category is ErrorCategory.ALL_BLANK
        finally:
            store.close()

    def test_job_error_includes_the_exception_notes(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        What ``add_note`` attached reaches ``job.error``.

        ``str(exc)`` drops notes, and a preserved scan says where its pages
        went in one, so the job record would otherwise lose the path.
        """

        def failing(*_a: object, **_k: object) -> ScanResult:
            exc = ScanError("Scanner jam")
            exc.add_note("The 3 page(s) were preserved at /d/x.pdf")
            raise exc

        monkeypatch.setattr("saneless.worker.run_pipeline", failing)
        store = JobStore()
        try:
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()
            job = store.create_job("default", "Note Test")
            worker.submit(job)
            wait_for_state(store, job.id, TERMINAL_STATES)
            worker.stop()
            fetched = _get(store, job.id)
            assert fetched.state is JobState.ERROR
            assert fetched.error == (
                "Scanner jam. The 3 page(s) were preserved at /d/x.pdf"
            )
            assert fetched.error_category is ErrorCategory.SCANNER
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

# The owner token of the job a stop interrupts, for the owner-gated view.
_OWNER = "owner-of-the-stopped-scan"


def _inked_page() -> Image.Image:
    """
    Draw a page with enough ink that the real empty-page filter keeps it.

    Returns:
        A clearly non-blank RGB image.

    """
    page = Image.new("RGB", (120, 160), "white")
    ImageDraw.Draw(page).rectangle((10, 10, 110, 150), fill="black")
    return page


class _PassBGatedScanner(StubScannerBackend):
    """
    A scanner whose second ``scan_pages`` call waits for the test to release it.

    Without the gate, pass B returns in microseconds and ``SCANNING_REVERSE`` is
    an instantaneous transition no poll can ever observe.  Holding pass B open
    on a test-held ``Event`` turns it into a state the store genuinely records
    for as long as the test needs to look at it.

    A concrete class rather than a ``MagicMock``, as this suite's fakes are: a
    subclass of the ABC is caught by the type checkers when the backend
    contract changes, and a mock is not.

    ``get_devices`` comes from ``StubScannerBackend`` and answers ``[]``, which
    is what keeps startup profile generation from swapping the settings under
    these tests.  Only ``get_capabilities`` is overridden, because these
    tests need a feeder rather than the base's flatbed.

    Attributes:
        release_pass_b: Set by the test to let pass B return.
        scan_calls: How many times ``scan_pages`` has been entered.

    """

    def __init__(self) -> None:
        """Start with pass B held."""
        self.release_pass_b = threading.Event()
        self.scan_calls = 0

    def get_capabilities(self, device_id: str) -> DeviceCapabilities:
        """
        Report a feeder-only device.

        Args:
            device_id: Ignored.

        Returns:
            Capabilities naming a single feeder source.

        """
        return DeviceCapabilities(sources=["ADF"], resolutions=[300], modes=["color"])

    def scan_pages(
        self, device_id: str, settings: ScanSettings, sink: PageSink
    ) -> ScanBatch:
        """
        Spool one inked page, holding the second and later calls on the gate.

        Args:
            device_id: Ignored.
            settings: Only ``resolution`` is used, as the pages' dpi and the batch's.
            sink: The pipeline's own sink, which receives the page.

        Returns:
            A batch of the single record the sink returned.

        """
        self.scan_calls += 1
        if self.scan_calls >= 2:
            self.release_pass_b.wait(_PASS_B_GATE_CEILING)
        record = sink.add(_inked_page(), dpi=settings.resolution)
        return scan_batch([record], resolution=settings.resolution)


def _manual_duplex_settings(settings: Settings) -> Settings:
    """
    Add a ``duplex = "manual"`` profile named ``duplex`` to ``settings``.

    ``source`` is a real feeder name; the ``duplex`` key alone selects the
    strategy.

    Args:
        settings: The fixture settings to extend in place.

    Returns:
        The same settings, for chaining.

    """
    settings.profiles["duplex"] = ProfileConfig(source="ADF", duplex="manual")
    return settings


@pytest.fixture
def isolated_duplex_settings(default_settings: Settings) -> Settings:
    """
    Manual-duplex settings whose scratch and durable directories are per-test.

    ``default_settings`` already puts ``tmp_dir`` and ``data_dir`` on separate
    subtrees of the test's own ``tmp_path``, so whatever a job preserves into
    ``failed/`` disappears with the test instead of accumulating where
    ``preservation.warn_if_failed_dir_growing`` would start warning inside
    unrelated tests.

    Args:
        default_settings: The shared fixture settings, mutated in place.

    Returns:
        The same settings, with a ``duplex`` profile.

    """
    return _manual_duplex_settings(default_settings)


class TestWorkerPassB:
    """
    Pass B through the real pipeline, observed from outside the worker.

    These run the real ``run_pipeline``: the flip wait, the pass-B event and the
    state the worker persists for it are exactly what is under test, so stubbing
    the pipeline would stub the subject.  Only the scanner and the paperless
    client are fakes.
    """

    def test_pass_b_is_persisted_as_scanning_reverse(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
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
            worker.continue_flip(job.id)

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

    def test_abort_at_the_flip_prompt_cancels_the_job_before_pass_b(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        An Abort at the prompt ends the job CANCELLED through the real pipeline.

        The pipeline raises ``ScanCancelledError`` for the operator's Abort,
        and the worker records a cancel, not an ERROR.
        """
        caplog.set_level(logging.INFO, logger="saneless.worker")
        scanner = _PassBGatedScanner()
        settings = _manual_duplex_settings(default_settings)
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, settings, store)
        try:
            worker.start()
            job = store.create_job("duplex", "Aborted At Prompt")
            worker.submit(job)

            wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
            worker.abort_flip(job.id)
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            scanner.release_pass_b.set()
            worker.stop()
            store.close()

        _assert_cancelled_at_the_flip_prompt(finished, caplog)
        # The abort was answered before pass B: the backs were never fed.
        assert scanner.scan_calls == 1

    def test_a_late_abort_after_continue_is_dropped(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
    ) -> None:
        """
        An Abort arriving once pass B has begun changes nothing.

        The coordinator's first answer is final.  Pass B has genuinely started
        and an Abort does not stop a pass midway, so the job completes.
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
            worker.continue_flip(job.id)
            wait_for_state(store, job.id, JobState.SCANNING_REVERSE, _STATE_BUDGET)

            worker.abort_flip(job.id)
            scanner.release_pass_b.set()
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            scanner.release_pass_b.set()
            worker.stop()
            store.close()

        assert finished.state is JobState.DONE
        assert finished.error is None
        assert scanner.scan_calls == 2


class _GatedScanner(StubScannerBackend):
    """
    A scanner that holds chosen ``scan_pages`` calls until the test releases them.

    Call numbers count every pass of every job the worker runs, starting at 1.
    For each held call there is a ``gate`` the test sets to let it return and
    an ``entered`` event the scanner sets on arrival, so the test knows the
    pass is genuinely in flight before it sends a signal.  That is what makes
    "a click during pass A" a staged state rather than a timing guess.

    A concrete class rather than a ``MagicMock``, like ``_PassBGatedScanner``,
    and inheriting the same ``get_devices`` answer of ``[]`` from
    ``StubScannerBackend`` for the same reason: startup profile generation
    must leave these tests' settings alone.

    Attributes:
        gates: Per held call number, set by the test to let that call return.
        entered: Per held call number, set by the scanner when the call begins.
        scan_calls: How many times ``scan_pages`` has been entered.

    """

    def __init__(self, held_calls: frozenset[int]) -> None:
        """
        Hold the given call numbers until released.

        Args:
            held_calls: The 1-based ``scan_pages`` call numbers to hold.

        """
        self.gates = {call: threading.Event() for call in held_calls}
        self.entered = {call: threading.Event() for call in held_calls}
        self.scan_calls = 0

    def release_all(self) -> None:
        """Release every held call, for ``finally`` blocks."""
        for gate in self.gates.values():
            gate.set()

    def get_capabilities(self, device_id: str) -> DeviceCapabilities:
        """
        Report a feeder-only device.

        Args:
            device_id: Ignored.

        Returns:
            Capabilities naming a single feeder source.

        """
        return DeviceCapabilities(sources=["ADF"], resolutions=[300], modes=["color"])

    def scan_pages(
        self, device_id: str, settings: ScanSettings, sink: PageSink
    ) -> ScanBatch:
        """
        Spool one inked page, holding the configured calls on their gates.

        Args:
            device_id: Ignored.
            settings: Only ``resolution`` is used, as the pages' dpi and the batch's.
            sink: The pipeline's own sink, which receives the page.

        Returns:
            A batch of the single record the sink returned.

        """
        self.scan_calls += 1
        call = self.scan_calls
        if call in self.gates:
            self.entered[call].set()
            self.gates[call].wait(_PASS_B_GATE_CEILING)
        record = sink.add(_inked_page(), dpi=settings.resolution)
        return scan_batch([record], resolution=settings.resolution)


class _JammingGatedScanner(_GatedScanner):
    """A ``_GatedScanner`` whose held calls jam with a ``ScanError`` once released."""

    def scan_pages(
        self, device_id: str, settings: ScanSettings, sink: PageSink
    ) -> ScanBatch:
        """
        Wait on the gate like ``_GatedScanner``, then fail as a paper jam.

        The page is spooled before the raise, exactly as a real backend that
        jammed partway through a feed would leave it: the sink already holds
        whatever arrived before the failure.

        Args:
            device_id: Ignored.
            settings: Passed through to ``_GatedScanner``.
            sink: Passed through to ``_GatedScanner``.

        Raises:
            ScanError: Always, after the gate is released.

        """
        super().scan_pages(device_id, settings, sink)
        raise ScanError(_JAM_MESSAGE)


# The scanner's own text for the jam above, asserted verbatim on the job row.
_JAM_MESSAGE = "Scanner error on test:device:001: Document feeder jammed"


class TestFlipSignalsAreJobScoped:
    """
    A flip answer belongs to one job, and counts only at that job's prompt.

    These run the real ``run_pipeline``: the flip wait is the subject.  Only
    the scanner and the paperless client are fakes.  "Not answered early" is
    asserted as ``flip_answer(job.id) is None`` at AWAITING_FLIP, which reads
    the coordinator's slot directly rather than inferring it from timing.
    """

    def test_signals_during_pass_a_are_dropped(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
    ) -> None:
        """
        A Continue or Abort sent while pass A runs is dropped, not queued.

        A kept early Continue would start pass B on a stack nobody had
        flipped.
        """
        scanner = _GatedScanner(frozenset({1}))
        settings = _manual_duplex_settings(default_settings)
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, settings, store)
        try:
            worker.start()
            job = store.create_job("duplex", "Early Signals")
            worker.submit(job)

            assert scanner.entered[1].wait(_PASS_B_GATE_CEILING)
            assert worker.continue_flip(job.id) is False
            assert worker.abort_flip(job.id) is False

            scanner.gates[1].set()
            wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
            assert worker.flip_answer(job.id) is None

            assert worker.continue_flip(job.id) is True
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            scanner.release_all()
            worker.stop()
            store.close()

        assert finished.state is JobState.DONE
        assert scanner.scan_calls == 2

    def test_a_double_clicked_abort_cannot_abort_the_next_job(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
    ) -> None:
        """
        A double-clicked Abort cannot answer the next job's flip: job 2 ends DONE.

        Two manual-duplex jobs are queued.  Abort is clicked twice at job 1's
        prompt: the first click aborts job 1, the second is dropped.  Clicks
        meant for job 1 then land while job 2's pass A is held open, and again
        at job 2's own prompt.  None of them may answer job 2's flip; only
        job 2's own Continue does.
        """
        # Job 1 aborts at its prompt, so it makes one scan call; call 2 is
        # job 2's pass A, and call 3 (job 2's pass B) runs freely.
        scanner = _GatedScanner(frozenset({2}))
        settings = _manual_duplex_settings(default_settings)
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, settings, store)
        try:
            worker.start()
            job1 = store.create_job("duplex", "Job One")
            job2 = store.create_job("duplex", "Job Two")
            worker.submit(job1)
            worker.submit(job2)

            wait_for_state(store, job1.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
            assert worker.abort_flip(job1.id) is True
            assert worker.abort_flip(job1.id) is False
            first = wait_for_state(store, job1.id, TERMINAL_STATES, _STATE_BUDGET)

            # Job 2's pass A is in flight.
            assert scanner.entered[2].wait(_PASS_B_GATE_CEILING)
            assert worker.abort_flip(job1.id) is False
            assert worker.abort_flip(job2.id) is False
            assert worker.continue_flip(job2.id) is False

            scanner.gates[2].set()
            wait_for_state(store, job2.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
            assert worker.flip_answer(job2.id) is None
            # A stale click for job 1 at job 2's own prompt is still dropped.
            assert worker.abort_flip(job1.id) is False
            assert worker.flip_answer(job2.id) is None

            assert worker.continue_flip(job2.id) is True
            second = wait_for_state(store, job2.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            scanner.release_all()
            worker.stop()
            store.close()

        # Job 1's Abort is the operator's cancel.
        assert first.state is JobState.CANCELLED
        assert first.error is not None
        assert "flip prompt" in first.error
        assert second.state is JobState.DONE
        assert second.error is None
        assert scanner.scan_calls == 3

    def test_with_no_job_waiting_every_signal_is_dropped(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
    ) -> None:
        """With no job at a flip prompt, no job has an answer and none is taken."""
        store = JobStore()
        try:
            worker = ScanWorker(
                _GatedScanner(frozenset()), mock_paperless, default_settings, store
            )
            assert worker.flip_answer("no-such-job") is None
            assert worker.continue_flip("no-such-job") is False
            assert worker.abort_flip("no-such-job") is False
        finally:
            store.close()

    def test_the_log_says_whether_a_signal_was_claimed_or_dropped(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A dropped signal is logged as dropped, with the reason.

        Pass A and pass B are both held, so each signal meets a coordinator in
        a known state: unarmed, armed and open, and already answered.
        """
        scanner = _GatedScanner(frozenset({1, 2}))
        settings = _manual_duplex_settings(default_settings)
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, settings, store)
        caplog.set_level(logging.INFO, logger="saneless.worker")
        try:
            worker.start()
            job = store.create_job("duplex", "Logged Signals")
            worker.submit(job)

            assert scanner.entered[1].wait(_PASS_B_GATE_CEILING)
            worker.continue_flip(job.id)
            worker.abort_flip("some-other-job")

            scanner.gates[1].set()
            wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
            worker.continue_flip(job.id)

            assert scanner.entered[2].wait(_PASS_B_GATE_CEILING)
            worker.abort_flip(job.id)

            scanner.gates[2].set()
            wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            scanner.release_all()
            worker.stop()
            store.close()

        messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == "saneless.worker"
            and record.levelno == logging.INFO
            and record.getMessage().startswith("Manual duplex: ")
        ]
        assert messages == [
            f"Manual duplex: continue for job {job.id} dropped: "
            "not yet at the flip prompt",
            "Manual duplex: abort for job some-other-job dropped: "
            "not the job waiting at the flip prompt",
            f"Manual duplex: continue for job {job.id} claimed",
            f"Manual duplex: abort for job {job.id} dropped: already answered: "
            "CONTINUED",
        ]


class TestFlipDeadline:
    """
    The flip wait remembers when it began, and the worker reports its deadline.

    The deadline is what the status area states to an owner and to anyone
    else watching, so it is reported for the job at the flip prompt only.
    """

    def test_armed_at_starts_unset(self) -> None:
        """An unarmed coordinator has no wait under way."""
        assert WorkerFlipCoordinator("j").armed_at is None

    def test_arm_records_an_aware_utc_time(self) -> None:
        """Arming records the moment, in UTC, between two readings of the clock."""
        coordinator = WorkerFlipCoordinator("j")
        before = datetime.now(tz=UTC)
        coordinator.arm()
        after = datetime.now(tz=UTC)

        armed_at = coordinator.armed_at
        assert armed_at is not None
        assert armed_at.tzinfo is UTC
        assert before <= armed_at <= after

    def test_a_second_arm_keeps_the_first_time(self) -> None:
        """Arming again, as the wait itself does, does not restart the clock."""
        coordinator = WorkerFlipCoordinator("j")
        coordinator.arm()
        first = coordinator.armed_at
        assert first is not None
        # Let the clock move on, so a second reading would differ.
        assert poll_until(lambda: datetime.now(tz=UTC) > first, _STATE_BUDGET)

        coordinator.arm()

        assert coordinator.armed_at == first

    def test_flip_deadline_is_armed_at_plus_the_operator_wait(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
    ) -> None:
        """At the flip prompt the deadline is the wait's start plus its timeout."""
        scanner = _GatedScanner(frozenset({1}))
        settings = _manual_duplex_settings(default_settings)
        wait = timedelta(seconds=settings.output.operator_wait_timeout_seconds)
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, settings, store)
        try:
            worker.start()
            job = store.create_job("duplex", "Deadline")
            worker.submit(job)

            assert scanner.entered[1].wait(_PASS_B_GATE_CEILING)
            before = datetime.now(tz=UTC)
            scanner.gates[1].set()
            wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
            after = datetime.now(tz=UTC)

            coordinator = worker._flip_coordinator
            assert coordinator is not None
            armed_at = coordinator.armed_at
            assert armed_at is not None
            deadline = worker.flip_deadline(job.id)

            assert worker.continue_flip(job.id) is True
            wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
            ended = worker.flip_deadline(job.id)
        finally:
            scanner.release_all()
            worker.stop()
            store.close()

        assert deadline is not None
        assert deadline == armed_at + wait
        assert before + wait <= deadline <= after + wait
        assert ended is None

    def test_flip_deadline_is_none_for_another_job(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
    ) -> None:
        """Only the job at the flip prompt has a deadline; no other id does."""
        scanner = _GatedScanner(frozenset())
        settings = _manual_duplex_settings(default_settings)
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, settings, store)
        try:
            assert worker.flip_deadline("no-such-job") is None
            worker.start()
            job = store.create_job("duplex", "Someone Else's")
            worker.submit(job)
            wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)

            own = worker.flip_deadline(job.id)
            other = worker.flip_deadline("other-id")

            assert worker.continue_flip(job.id) is True
            wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            scanner.release_all()
            worker.stop()
            store.close()

        assert own is not None
        assert other is None

    def test_flip_deadline_is_none_before_arming(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
    ) -> None:
        """While pass A runs the job has a coordinator but no wait, so no deadline."""
        scanner = _GatedScanner(frozenset({1}))
        settings = _manual_duplex_settings(default_settings)
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, settings, store)
        try:
            worker.start()
            job = store.create_job("duplex", "Still Scanning")
            worker.submit(job)

            assert scanner.entered[1].wait(_PASS_B_GATE_CEILING)
            has_coordinator = worker._flip_coordinator is not None
            during_pass_a = worker.flip_deadline(job.id)

            scanner.gates[1].set()
            wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
            assert worker.continue_flip(job.id) is True
            wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            scanner.release_all()
            worker.stop()
            store.close()

        assert has_coordinator is True
        assert during_pass_a is None


# The queue depth ScanWorker keeps (not configurable).  Restated here so a
# change to it has to be made on purpose in both places.
_QUEUE_DEPTH = 10


def _fill_the_queue(worker: ScanWorker, store: JobStore) -> list[SubmitResult]:
    """
    Submit enough jobs to fill the worker's queue, reporting each result.

    The caller must already hold a job inside ``scan_pages``, so the queue is
    empty when this starts and every one of these submits waits in it.

    Args:
        worker: A started worker whose current job is held.
        store: The worker's job store, which creates the rows.

    Returns:
        What ``submit`` reported for each of the ``_QUEUE_DEPTH`` jobs.

    """
    return [
        worker.submit(store.create_job("default", f"Queued {n}"))
        for n in range(_QUEUE_DEPTH)
    ]


class TestWorkerStopAndSubmit:
    """
    Stopping is a flag, bounded and reported; submitting never blocks.

    A full queue holds neither a request thread in ``submit`` nor shutdown
    in ``stop``: stopping sets a flag and shuts the queue down, the join is
    bounded at five seconds and reports whether the thread stopped, and
    ``submit`` reports ``QUEUE_FULL`` or ``DOWN`` instead of waiting.  Every
    wait below is on a state, a staged gate, or a bounded clock -- none
    relies on ``stop`` draining work.
    """

    def test_an_idle_worker_stops_at_once(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
    ) -> None:
        """
        An idle worker's stop() wakes the loop, not the idle tick.

        The idle tick is left at its default, far above the bound asserted
        here, so only the queue shutdown can explain a prompt return.
        """
        assert worker_module._IDLE_TICK_SECONDS >= 5.0
        store = JobStore()
        worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
        try:
            worker.start()
            started = time.monotonic()
            stopped = worker.stop()
            elapsed = time.monotonic() - started
        finally:
            worker.stop()
            store.close()

        assert stopped is True
        assert elapsed < 0.5
        assert not worker.is_alive

    def test_stop_is_safe_twice_and_before_start(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
    ) -> None:
        """stop() reports True for a stopped thread, however it got there."""
        store = JobStore()
        never_started = ScanWorker(
            mock_scanner, mock_paperless, default_settings, store
        )
        worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
        try:
            assert never_started.stop() is True
            worker.start()
            assert worker.stop() is True
            assert worker.stop() is True
        finally:
            worker.stop()
            store.close()

    def test_submit_reports_down_before_start_and_after_stop(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
    ) -> None:
        """A worker that cannot take work says so instead of queueing."""
        store = JobStore()
        worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
        try:
            before = worker.submit(store.create_job("default", "Too Early"))
            worker.start()
            worker.stop()
            after = worker.submit(store.create_job("default", "Too Late"))
        finally:
            worker.stop()
            store.close()

        assert before is SubmitResult.DOWN
        assert after is SubmitResult.DOWN

    def test_a_full_queue_is_reported_without_blocking(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
    ) -> None:
        """
        The submit that finds no room returns QUEUE_FULL at once.

        Job 1 is held inside ``scan_pages``, so the queue is empty when the
        next ten arrive; they fill it, and the eleventh has nowhere to go.
        """
        scanner = _GatedScanner(frozenset({1}))
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, default_settings, store)
        try:
            worker.start()
            first = worker.submit(store.create_job("default", "Held"))
            assert scanner.entered[1].wait(_PASS_B_GATE_CEILING)

            queued = _fill_the_queue(worker, store)
            started = time.monotonic()
            overflow = worker.submit(store.create_job("default", "No Room"))
            elapsed = time.monotonic() - started
        finally:
            scanner.release_all()
            worker.stop()
            store.close()

        assert first is SubmitResult.ACCEPTED
        assert queued == [SubmitResult.ACCEPTED] * _QUEUE_DEPTH
        assert overflow is SubmitResult.QUEUE_FULL
        assert elapsed < 2.0

    def test_stop_on_a_full_queue_is_bounded_and_reports_false(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A full queue cannot hold stop(), and a stuck join says so.

        Job 1 stays held past the (shortened) join, so the thread is still
        alive when stop() gives up: it must return False within its bound
        rather than block on the full queue.  Once the gate opens the thread
        finishes job 1 and exits without starting the queued ones.
        """
        monkeypatch.setattr("saneless.worker.STOP_JOIN_SECONDS", 0.2)
        scanner = _GatedScanner(frozenset({1}))
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, default_settings, store)
        try:
            worker.start()
            worker.submit(store.create_job("default", "Held"))
            assert scanner.entered[1].wait(_PASS_B_GATE_CEILING)
            _fill_the_queue(worker, store)

            started = time.monotonic()
            stopped = worker.stop()
            elapsed = time.monotonic() - started
        finally:
            scanner.release_all()
            # Joined here, not through stop(), whose join is shortened: the
            # store must not close under a thread still finishing job 1.
            worker._thread.join(_PASS_B_GATE_CEILING + _STATE_BUDGET)
            exited = not worker.is_alive
            store.close()

        assert stopped is False
        assert elapsed < 1.0
        assert exited
        assert scanner.scan_calls == 1

    def test_stop_at_the_flip_wait_keeps_the_fronts_and_records_the_restart(
        self,
        mock_paperless: MagicMock,
        isolated_duplex_settings: Settings,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A stop at the flip prompt is an interruption, not a cancel.

        The wait ends at once with ``INTERRUPTED``, pass A's fronts are kept
        as a ``(fronts)`` PDF in ``failed/``, and the row's error starts with
        the restart reason and names that file, with no category and no
        failure logged.  The owner sees the file relative to the data
        directory; anyone else sees only the fixed sentence saying something
        was kept.  Nobody chose to throw the scan away, so nothing is lost.
        """
        caplog.set_level(logging.INFO, logger="saneless.worker")
        scanner = _PassBGatedScanner()
        settings = isolated_duplex_settings
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, settings, store)
        try:
            worker.start()
            job = store.create_job("duplex", "Stopped At Prompt", owner_token=_OWNER)
            worker.submit(job)
            wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)

            started = time.monotonic()
            stopped = worker.stop()
            elapsed = time.monotonic() - started
            finished = _get(store, job.id)
        finally:
            scanner.release_pass_b.set()
            worker.stop()
            store.close()

        assert stopped is True
        assert elapsed < _STATE_BUDGET
        assert finished.state is JobState.ERROR
        assert finished.error is not None
        assert finished.error.startswith(f"{RESTART_REASON}. ")
        assert "preserved at " in finished.error
        assert finished.error_category is None
        assert scanner.scan_calls == 1
        mock_paperless.upload_document.assert_not_called()
        preserved = sorted(settings.output.failed_dir.glob("*.pdf"))
        assert len(preserved) == 1
        assert "fronts" in preserved[0].name
        assert preserved[0].name in finished.error
        # Ended by the server, not by a fault: one INFO line, no ERROR line.
        ended = f"Job {finished.id} ended by shutdown"
        assert len(_worker_records(caplog, logging.INFO, ended)) == 1
        assert _worker_records(caplog, logging.ERROR, finished.id) == []

        owner = build_job_view(finished, presented=_OWNER, settings=settings)
        assert owner.error is not None
        assert owner.error.startswith(RESTART_REASON)
        assert f"failed/{preserved[0].name}" in owner.error
        assert str(settings.output.data_dir) not in owner.error
        other = build_job_view(finished, presented="someone-else", settings=settings)
        assert other.error == HIDDEN_PRESERVED_ERROR

    def test_a_stop_during_pass_a_keeps_the_fronts_once_they_reach_the_flip(
        self,
        mock_paperless: MagicMock,
        isolated_duplex_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A stop claimed while pass A still scans is answered at the flip.

        stop() finds the job inside pass A's read, pre-answers the flip with
        ``INTERRUPTED`` and gives up its (shortened) join.  Once pass A
        finishes, the flip wait returns that answer at once, the fronts are
        kept and the row records the restart, exactly as for a stop at the
        prompt itself.
        """
        monkeypatch.setattr("saneless.worker.STOP_JOIN_SECONDS", 0.2)
        scanner = _GatedScanner(frozenset({1}))
        settings = isolated_duplex_settings
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, settings, store)
        try:
            worker.start()
            job = store.create_job("duplex", "Stopped In Pass A")
            worker.submit(job)
            assert scanner.entered[1].wait(_PASS_B_GATE_CEILING)
            stopped = worker.stop()
            coordinator = worker._flip_coordinator
            assert coordinator is not None
            assert coordinator.answer is FlipOutcome.INTERRUPTED
            scanner.gates[1].set()
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            scanner.release_all()
            # Joined here, not through stop(), whose join is shortened: the
            # store must not close under a thread still finishing the job.
            worker._thread.join(_PASS_B_GATE_CEILING + _STATE_BUDGET)
            exited = not worker.is_alive
            store.close()

        assert stopped is False
        assert exited
        assert finished.state is JobState.ERROR
        assert finished.error is not None
        assert finished.error.startswith(f"{RESTART_REASON}. ")
        assert "preserved at " in finished.error
        assert finished.error_category is None
        assert scanner.scan_calls == 1
        preserved = sorted(settings.output.failed_dir.glob("*.pdf"))
        assert len(preserved) == 1
        assert "fronts" in preserved[0].name

    def test_stop_waits_longer_while_a_stopped_scan_is_being_preserved(
        self,
        mock_paperless: MagicMock,
        isolated_duplex_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        The join is extended, within a bound, while the fronts are copied.

        Under Docker the preservation is a copy across filesystems, and it can
        outlast the ordinary join.  The race is staged rather than timed: the
        move into ``failed/`` is held on a gate until stop() starts a second
        join, so the ordinary (shortened) join always runs out with the copy
        still in flight.  stop() must see that a preservation is in flight,
        join again for ``PRESERVATION_JOIN_SECONDS``, and report the thread
        stopped with the fronts in place.
        """
        monkeypatch.setattr("saneless.worker.STOP_JOIN_SECONDS", 0.2)
        monkeypatch.setattr("saneless.worker.PRESERVATION_JOIN_SECONDS", 10.0)
        real_move = preservation_module.move_private
        copy_started = threading.Event()
        copy_released = threading.Event()

        def held_move(source: Path, destination: Path) -> None:
            copy_started.set()
            copy_released.wait(_PASS_B_GATE_CEILING)
            real_move(source, destination)

        monkeypatch.setattr("saneless.preservation.move_private", held_move)
        scanner = _PassBGatedScanner()
        settings = isolated_duplex_settings
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, settings, store)
        real_join = worker._thread.join
        joins: list[float | None] = []

        def recording_join(timeout: float | None = None) -> None:
            # The second join is the extension: the copy is released only
            # then, so the first one cannot have outlasted it.
            joins.append(timeout)
            if len(joins) >= 2:
                copy_released.set()
            real_join(timeout=timeout)

        monkeypatch.setattr(worker._thread, "join", recording_join)
        try:
            worker.start()
            job = store.create_job("duplex", "Slow To Preserve")
            worker.submit(job)
            wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
            stopped = worker.stop()
            finished = _get(store, job.id)
        finally:
            copy_released.set()
            scanner.release_pass_b.set()
            real_join(_PASS_B_GATE_CEILING + _STATE_BUDGET)
            store.close()

        assert stopped is True
        # The ordinary join, then the extension; the finally block's join
        # bypasses the recorder.
        assert joins == [0.2, 10.0]
        assert copy_started.is_set()
        assert finished.state is JobState.ERROR
        assert finished.error is not None
        assert finished.error.startswith(f"{RESTART_REASON}. ")
        preserved = sorted(settings.output.failed_dir.glob("*.pdf"))
        assert len(preserved) == 1
        assert "fronts" in preserved[0].name

    def test_stop_does_not_wait_longer_when_nothing_is_being_preserved(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        The stop extension applies only while pages are being preserved.

        A scan parked inside a SANE read is preserving nothing, so stop()
        gives up after the ordinary (shortened) join and reports the thread
        still running, however long the preservation bound is.
        """
        monkeypatch.setattr("saneless.worker.STOP_JOIN_SECONDS", 0.2)
        monkeypatch.setattr("saneless.worker.PRESERVATION_JOIN_SECONDS", 10.0)
        scanner = _GatedScanner(frozenset({1}))
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, default_settings, store)
        try:
            worker.start()
            worker.submit(store.create_job("default", "Parked In A Read"))
            assert scanner.entered[1].wait(_PASS_B_GATE_CEILING)

            started = time.monotonic()
            stopped = worker.stop()
            elapsed = time.monotonic() - started
        finally:
            scanner.release_all()
            worker._thread.join(_PASS_B_GATE_CEILING + _STATE_BUDGET)
            exited = not worker.is_alive
            store.close()

        assert stopped is False
        assert elapsed < _STATE_BUDGET
        assert exited

    def test_a_job_reaching_the_prompt_while_stopping_is_interrupted_at_once(
        self,
        mock_paperless: MagicMock,
        isolated_duplex_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Stopping during pass A still ends the flip wait.

        stop() can find no coordinator to answer because the job is still in
        pass A.  The stop flag is set while pass A is held, then pass A is
        released: the job must announce AWAITING_FLIP and be interrupted
        immediately, well inside the state budget and nowhere near the flip
        timeout.  Like any stop at the flip, it keeps the fronts, so the error
        starts with the restart reason and names the kept file.
        """
        scanner = _GatedScanner(frozenset({1}))
        settings = isolated_duplex_settings
        assert settings.output.operator_wait_timeout_seconds > _STATE_BUDGET * 10
        states_seen: list[JobState] = []
        store = JobStore()
        original_update = store.update_state

        def tracking_update(
            job_id: str,
            state: JobState,
            error: str | None = None,
            error_category: ErrorCategory | None = None,
        ) -> None:
            """Record every state the worker persists."""
            states_seen.append(state)
            original_update(job_id, state, error=error, error_category=error_category)

        monkeypatch.setattr(store, "update_state", tracking_update)
        worker = ScanWorker(scanner, mock_paperless, settings, store)
        try:
            worker.start()
            job = store.create_job("duplex", "Stopping In Pass A")
            worker.submit(job)
            assert scanner.entered[1].wait(_PASS_B_GATE_CEILING)

            worker._stopping.set()
            started = time.monotonic()
            scanner.gates[1].set()
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
            elapsed = time.monotonic() - started
        finally:
            scanner.release_all()
            worker.stop()
            store.close()

        assert JobState.AWAITING_FLIP in states_seen
        assert elapsed < _STATE_BUDGET
        assert finished.state is JobState.ERROR
        assert finished.error is not None
        assert finished.error.startswith(f"{RESTART_REASON}. ")
        assert "preserved at " in finished.error
        assert finished.error_category is None
        assert scanner.scan_calls == 1

    def test_a_pipeline_failure_while_stopping_keeps_its_own_cause(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Stopping alone does not make a failure a restart.

        The pipeline fails with a Paperless error just as shutdown begins.
        Shutdown did not cause it, so the row keeps the error's own text and
        category instead of "server restarted" with no category.
        """
        message = "Paperless refused the upload"
        holder: list[ScanWorker] = []

        def failing_during_shutdown(*_args: object, **_kwargs: object) -> ScanResult:
            holder[0]._stopping.set()
            raise PaperlessError(message)

        monkeypatch.setattr("saneless.worker.run_pipeline", failing_during_shutdown)
        store = JobStore()
        worker = worker_for(store)
        holder.append(worker)
        try:
            worker.start()
            job = _submit_jobs(worker, store, 1)[0]
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            worker.stop()
            store.close()

        assert finished.state is JobState.ERROR
        assert finished.error == message
        assert finished.error_category == classify_error(PaperlessError(message))

    def test_a_pass_a_failure_after_stop_claimed_the_flip_stays_a_failure(
        self,
        mock_paperless: MagicMock,
        isolated_duplex_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        Shutdown claiming the flip does not relabel a real failure.

        stop() pre-answers the flip wait with ``INTERRUPTED`` while pass A is
        still scanning, before the pipeline ever reaches the prompt.  Pass A
        then jams.  The job did not end at the flip, so the row keeps the
        scanner's text and category, and the failure is logged with its
        traceback rather than as a restart at INFO.

        The jam keeps pass A's fronts, so this test writes a real PDF into
        ``failed/``; the per-test settings keep it inside ``tmp_path``.
        """
        caplog.set_level(logging.INFO, logger="saneless.worker")
        monkeypatch.setattr("saneless.worker.STOP_JOIN_SECONDS", 0.2)
        scanner = _JammingGatedScanner(frozenset({1}))
        settings = isolated_duplex_settings
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, settings, store)
        try:
            worker.start()
            job = store.create_job("duplex", "Jammed In Pass A")
            worker.submit(job)
            assert scanner.entered[1].wait(_PASS_B_GATE_CEILING)
            worker.stop()
            coordinator = worker._flip_coordinator
            assert coordinator is not None
            assert coordinator.answer is FlipOutcome.INTERRUPTED
            scanner.gates[1].set()
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            scanner.release_all()
            worker._thread.join(_PASS_B_GATE_CEILING + _STATE_BUDGET)
            store.close()

        assert finished.state is JobState.ERROR
        assert finished.error is not None
        assert _JAM_MESSAGE in finished.error
        assert not finished.error.startswith(RESTART_REASON)
        assert finished.error_category == classify_error(ScanError(_JAM_MESSAGE))
        failures = [
            record.exc_info
            for record in caplog.records
            if record.name == "saneless.worker" and record.levelno >= logging.ERROR
        ]
        assert len(failures) == 1
        exc_info = failures[0]
        assert exc_info is not None
        assert isinstance(exc_info[1], ScanError)
        # The jam keeps pass A's fronts, and this pins where: inside
        # this test's own directory, not the suite's shared one.
        preserved = sorted(settings.output.failed_dir.glob("*.pdf"))
        assert len(preserved) == 1
        assert "fronts" in preserved[0].name

    def test_an_operator_abort_while_stopping_is_not_recorded_as_a_restart(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        An Abort the operator claimed keeps its own meaning.

        Shutdown has begun, but the operator's Abort claimed the flip answer
        first, so shutdown's own Abort is dropped and the row records the
        operator's cancel rather than a server restart.
        """
        caplog.set_level(logging.INFO, logger="saneless.worker")
        scanner = _PassBGatedScanner()
        settings = _manual_duplex_settings(default_settings)
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, settings, store)
        try:
            worker.start()
            job = store.create_job("duplex", "Operator Aborted")
            worker.submit(job)
            wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
            worker._stopping.set()
            operator_claimed = worker.abort_flip(job.id)
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            scanner.release_pass_b.set()
            worker.stop()
            store.close()

        assert operator_claimed is True
        assert finished.error != RESTART_REASON
        _assert_cancelled_at_the_flip_prompt(finished, caplog)


# The error text every simulated job store failure below carries.
_DISK_ERROR = "disk I/O error"

# The idle tick the guard tests run at, so housekeeping gets a turn many times
# inside a test instead of once every five seconds.
_FAST_TICK = 0.02

# An owed-write streak limit no test window can reach, for tests that
# deliberately watch the store fail before healing it.  TestOwedWriteStreak
# pins the real limit.
_UNREACHABLE_STREAK = 1_000_000


class _StoreFault:
    """
    A callable that raises the job store's ``sqlite3.OperationalError`` on cue.

    It wraps a job store method, or the worker's handling of a job (see
    :func:`_fail_the_job_path`).

    Every call is recorded.  A call whose 1-based number is in ``fail_calls``
    raises -- or every call does, when ``fail_calls`` is ``None`` -- until
    :meth:`heal` is called; the rest delegate to the real method, so the row
    the worker writes is the row the test reads back.

    Args:
        original: The bound store method being replaced.
        fail_calls: Which calls raise, or ``None`` for all of them.

    """

    def __init__(
        self,
        original: Callable[..., object],
        fail_calls: frozenset[int] | None = None,
    ) -> None:
        """Wrap ``original``, failing on ``fail_calls`` until healed."""
        self._original = original
        self._fail_calls = fail_calls
        self._healed = threading.Event()
        self._lock = threading.Lock()
        self._calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    @property
    def calls(self) -> list[tuple[tuple[object, ...], dict[str, object]]]:
        """A snapshot of every call so far, as ``(args, kwargs)``."""
        with self._lock:
            return list(self._calls)

    def heal(self) -> None:
        """Stop raising: every later call delegates to the real method."""
        self._healed.set()

    def __call__(self, *args: object, **kwargs: object) -> object:
        """Record the call, then raise or delegate."""
        with self._lock:
            self._calls.append((args, kwargs))
            number = len(self._calls)
        failing = self._fail_calls is None or number in self._fail_calls
        if failing and not self._healed.is_set():
            raise sqlite3.OperationalError(_DISK_ERROR)
        return self._original(*args, **kwargs)


def _fail_the_job_path(
    worker: ScanWorker,
    monkeypatch: pytest.MonkeyPatch,
    fail_calls: frozenset[int] | None = None,
) -> _StoreFault:
    """
    Make an unstarted worker's handling of chosen jobs raise the store's error.

    This stages a loop-level failure that is not the terminal write, so the
    guard has no outcome owed for the job and records the failure's own text.
    No store write is left ahead of the pipeline to stage it with: SCANNING
    is recorded from the pipeline's first event, under the scanner gate, as a
    progress write whose failure is only logged.  So the job's whole handling
    raises instead, as a store error escaping it would.

    Args:
        worker: The worker whose job handling to break.
        monkeypatch: Undoes the patch after the test.
        fail_calls: Which jobs, numbered from 1, raise; ``None`` for all.

    Returns:
        The fault, to count the jobs it saw or to heal it.

    """
    fault = _StoreFault(worker._scan_job, fail_calls)
    monkeypatch.setattr(worker, "_scan_job", fault)
    return fault


# Idle ticks a test that asserts nothing happened waits for before asserting.
_QUIET_TICKS = 10


def _count_idle_ticks(
    worker: ScanWorker, monkeypatch: pytest.MonkeyPatch
) -> Callable[[], int]:
    """
    Count the idle ticks an unstarted worker completes once it runs.

    A test that sleeps and then asserts nothing happened also passes when the
    thread never reached its first idle tick on a slow runner.
    Counting completed ticks proves the window was real.

    Returns:
        A callable returning how many idle ticks have completed so far.

    """
    original = worker._idle_housekeeping
    lock = threading.Lock()
    completed = [0]

    def counting_housekeeping() -> None:
        original()
        with lock:
            completed[0] += 1

    def count() -> int:
        with lock:
            return completed[0]

    monkeypatch.setattr(worker, "_idle_housekeeping", counting_housekeeping)
    return count


def _worker_records(
    caplog: pytest.LogCaptureFixture, level: int, text: str
) -> list[logging.LogRecord]:
    """Return the ``saneless.worker`` records at ``level`` whose message has ``text``."""
    return [
        record
        for record in caplog.records
        if record.name == "saneless.worker"
        and record.levelno == level
        and text in record.getMessage()
    ]


@pytest.fixture(name="worker_for")
def _worker_for_fixture(
    mock_scanner: MagicMock,
    mock_paperless: MagicMock,
    default_settings: Settings,
) -> Callable[[JobStore], ScanWorker]:
    """
    Build unstarted workers over the shared scanner, Paperless and settings mocks.

    Returns:
        A factory taking the job store the worker writes to.

    """

    def build(store: JobStore) -> ScanWorker:
        return ScanWorker(mock_scanner, mock_paperless, default_settings, store)

    return build


class TestWorkerGuard:
    """
    No exception from the pipeline, the job store or prune ends the worker.

    A pipeline failure is a job failure, a failure of the loop's own store
    writes is a loop failure, and prune runs on an hourly idle tick outside
    the job path.  None of them ends the thread, so no later scan sits
    PENDING until a restart.  Each test proves the worker survived by
    finishing a later job, not by reading ``is_alive`` alone.
    """

    def test_a_store_error_escaping_a_job_does_not_end_the_worker(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A store error escaping the job's handling is logged and survived."""
        caplog.set_level(logging.INFO, logger="saneless.worker")
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            lambda *_args, **_kwargs: _success_result(),
        )
        store = JobStore()
        worker = worker_for(store)
        _fail_the_job_path(worker, monkeypatch, frozenset({1}))
        try:
            worker.start()
            first = store.create_job("default", "Store Fails")
            worker.submit(first)
            failed = wait_for_state(store, first.id, TERMINAL_STATES, _STATE_BUDGET)
            second = store.create_job("default", "After The Failure")
            worker.submit(second)
            finished = wait_for_state(store, second.id, TERMINAL_STATES, _STATE_BUDGET)
            alive = worker.is_alive
        finally:
            worker.stop()
            store.close()

        assert failed.state is JobState.ERROR
        assert finished.state is JobState.DONE
        assert alive
        records = _worker_records(caplog, logging.ERROR, first.id)
        assert records
        assert records[0].exc_info is not None

    def test_the_failure_line_shows_controls_in_the_error_as_escapes(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A failure's text is logged quoted, with each control as its escape.

        The text can come from outside saneless -- a device, a proxy's error
        page -- so an ESC in it must not reach a terminal tailing the log live.
        """
        caplog.set_level(logging.INFO, logger="saneless.worker")

        def failing_pipeline(*_args: object, **_kwargs: object) -> ScanResult:
            msg = "upstream said \x1b]0;owned\x07no"
            raise PaperlessError(msg)

        monkeypatch.setattr("saneless.worker.run_pipeline", failing_pipeline)
        store = JobStore()
        worker = worker_for(store)
        try:
            worker.start()
            job = store.create_job("default", "Hostile Failure")
            worker.submit(job)
            failed = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            worker.stop()
            store.close()

        assert failed.state is JobState.ERROR
        (record,) = _worker_records(caplog, logging.ERROR, job.id)
        message = record.getMessage()
        assert not has_control_characters(message)
        assert "'upstream said \\x1b]0;owned\\x07no'" in message

    def test_a_failed_error_write_after_a_pipeline_failure_does_not_end_the_worker(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        The failure-path ``finish_job`` raising is a loop failure.

        The pipeline fails job 1; recording that failure raises once.  The guard
        logs it and its own best-effort write lands the ERROR, and job 2 runs.
        """
        caplog.set_level(logging.INFO, logger="saneless.worker")
        runs: list[str] = []

        def failing_once(
            _scanner: object,
            _paperless: object,
            _settings: object,
            request: PipelineRequest,
        ) -> ScanResult:
            runs.append(request.job_id)
            if len(runs) == 1:
                msg = "Scanner jammed"
                raise ScanError(msg)
            return _success_result()

        monkeypatch.setattr("saneless.worker.run_pipeline", failing_once)
        store = JobStore()
        fault = _StoreFault(store.finish_job, frozenset({1}))
        monkeypatch.setattr(store, "finish_job", fault)
        worker = worker_for(store)
        try:
            worker.start()
            first = store.create_job("default", "Unrecordable Failure")
            worker.submit(first)
            failed = wait_for_state(store, first.id, TERMINAL_STATES, _STATE_BUDGET)
            second = store.create_job("default", "After The Failure")
            worker.submit(second)
            finished = wait_for_state(store, second.id, TERMINAL_STATES, _STATE_BUDGET)
            alive = worker.is_alive
        finally:
            worker.stop()
            store.close()

        assert failed.state is JobState.ERROR
        assert failed.error is not None
        assert finished.state is JobState.DONE
        assert alive
        records = _worker_records(caplog, logging.ERROR, first.id)
        assert any(record.exc_info is not None for record in records)

    def test_a_prune_failure_never_fails_a_job(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A raising prune is a WARNING with its traceback; jobs finish."""
        caplog.set_level(logging.INFO, logger="saneless.worker")
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", _FAST_TICK)
        monkeypatch.setattr("saneless.worker._PRUNE_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            lambda *_args, **_kwargs: _success_result(),
        )
        store = JobStore()
        fault = _StoreFault(store.prune, frozenset({1}))
        monkeypatch.setattr(store, "prune", fault)
        worker = worker_for(store)
        try:
            worker.start()
            pruned = poll_until(lambda: len(fault.calls) >= 1, _STATE_BUDGET)
            jobs = [store.create_job("default", f"Around Prune {n}") for n in (1, 2)]
            for job in jobs:
                worker.submit(job)
            finished = [
                wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
                for job in jobs
            ]
            alive = worker.is_alive
        finally:
            worker.stop()
            store.close()

        assert pruned
        assert [job.state for job in finished] == [JobState.DONE, JobState.DONE]
        assert alive
        records = _worker_records(caplog, logging.WARNING, "Idle history prune failed")
        assert len(records) == 1
        assert records[0].exc_info is not None
        assert not _worker_records(caplog, logging.ERROR, "Idle history prune failed")

    def test_interleaved_prune_failures_do_not_degrade(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A successful prune ends the run of loop-level failures.

        Prunes 1, 3 and 5 fail and 2 and 4 succeed.  No two failures are
        consecutive, so the worker must never degrade.  A degraded worker
        heals on its next probe, so the check reads the degraded WARNING,
        which stays in the log, rather than trusting the health at the end.
        """
        caplog.set_level(logging.INFO, logger="saneless.worker")
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", _FAST_TICK)
        monkeypatch.setattr("saneless.worker._PRUNE_INTERVAL_SECONDS", 0.05)
        store = JobStore()
        fault = _StoreFault(store.prune, frozenset({1, 3, 5}))
        monkeypatch.setattr(store, "prune", fault)
        worker = worker_for(store)
        try:
            worker.start()
            pruned = poll_until(lambda: len(fault.calls) >= 5, _STATE_BUDGET)
            health = worker.health
        finally:
            worker.stop()
            store.close()

        assert pruned
        assert health is not WorkerHealth.DEGRADED
        assert worker._consecutive_loop_failures <= 1
        failures = _worker_records(caplog, logging.WARNING, "Idle history prune failed")
        assert len(failures) >= 3
        assert not _worker_records(caplog, logging.WARNING, "Scan worker degraded")

    def test_pipeline_failures_are_job_failures_not_loop_failures(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Three pipeline failures in a row are three ERROR jobs, and no more."""

        def failing_pipeline(*_args: object, **_kwargs: object) -> ScanResult:
            msg = "Scanner jammed"
            raise ScanError(msg)

        monkeypatch.setattr("saneless.worker.run_pipeline", failing_pipeline)
        store = JobStore()
        worker = worker_for(store)
        try:
            worker.start()
            jobs = [store.create_job("default", f"Jammed {n}") for n in (1, 2, 3)]
            for job in jobs:
                worker.submit(job)
            finished = [
                wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
                for job in jobs
            ]
            loop_failures = worker._consecutive_loop_failures
            health = worker.health
            alive = worker.is_alive
        finally:
            worker.stop()
            store.close()

        assert [job.state for job in finished] == [JobState.ERROR] * 3
        assert loop_failures == 0
        assert health is WorkerHealth.HEALTHY
        assert alive

    def test_idle_worker_prunes_history_on_its_interval(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An idle worker prunes with the configured limits once due."""
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", _FAST_TICK)
        monkeypatch.setattr("saneless.worker._PRUNE_INTERVAL_SECONDS", 0.05)
        store = JobStore()
        spy = _StoreFault(store.prune, frozenset())
        monkeypatch.setattr(store, "prune", spy)
        worker = worker_for(store)
        try:
            worker.start()
            pruned = poll_until(lambda: len(spy.calls) >= 1, 1.0)
        finally:
            worker.stop()
            store.close()

        assert pruned
        output = worker._settings.output
        assert spy.calls[0] == (
            (output.history_retention_days, output.history_max_rows),
            {},
        )

    def test_idle_worker_does_not_prune_before_the_interval(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The idle prune is hourly, not every tick."""
        assert worker_module._PRUNE_INTERVAL_SECONDS >= 3600.0
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", _FAST_TICK)
        store = JobStore()
        spy = _StoreFault(store.prune, frozenset())
        monkeypatch.setattr(store, "prune", spy)
        worker = worker_for(store)
        ticks = _count_idle_ticks(worker, monkeypatch)
        try:
            worker.start()
            # A window in which nothing may happen, measured in idle ticks the
            # worker actually took rather than in wall time.
            ticked = poll_until(lambda: ticks() >= _QUIET_TICKS, _STATE_BUDGET)
        finally:
            worker.stop()
            store.close()

        assert ticked
        assert spy.calls == []

    def test_the_guard_records_the_failure_it_could_not_hide(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A job whose handling raised a store error still ends.

        The guard makes one best-effort ``finish_job`` with the failure's own
        text and category, so the row does not sit active until restart.
        """
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            lambda *_args, **_kwargs: _success_result(),
        )
        store = JobStore()
        spy = _StoreFault(store.finish_job, frozenset())
        monkeypatch.setattr(store, "finish_job", spy)
        worker = worker_for(store)
        _fail_the_job_path(worker, monkeypatch)
        try:
            worker.start()
            job = store.create_job("default", "Never Scanning")
            worker.submit(job)
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            worker.stop()
            store.close()

        expected_category = classify_error(sqlite3.OperationalError(_DISK_ERROR))
        assert spy.calls == [
            (
                (job.id, JobState.ERROR),
                {
                    "result": None,
                    "error": _DISK_ERROR,
                    "error_category": expected_category,
                },
            )
        ]
        assert finished.state is JobState.ERROR
        assert finished.error == _DISK_ERROR

    @pytest.mark.parametrize("failed_writes", [1, 2])
    def test_a_failed_success_write_is_replayed_as_the_outcome_not_an_error(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
        failed_writes: int,
    ) -> None:
        """
        A document Paperless accepted is never recorded as an ERROR.

        The terminal DONE write fails after the upload.  With one failure the
        guard's own retry lands it; with two the guard's retry fails too and
        an idle tick lands it.  Either way the row is DONE with the result the
        pipeline returned, and no ERROR is ever written.
        """
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", _FAST_TICK)
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            lambda *_args, **_kwargs: _success_result(),
        )
        store = JobStore()
        finishes = _StoreFault(store.finish_job, frozenset(range(1, failed_writes + 1)))
        monkeypatch.setattr(store, "finish_job", finishes)
        worker = worker_for(store)
        try:
            worker.start()
            job = _submit_jobs(worker, store, 1)[0]
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
            drained = poll_until(lambda: not worker._unrecorded_failures, _STATE_BUDGET)
        finally:
            worker.stop()
            store.close()

        assert drained
        assert finished.state is JobState.DONE
        assert finished.outcome is ScanOutcome.SUCCESS
        assert finished.pages_uploaded == 1
        assert finished.error is None
        assert finished.error_category is None
        written_states = [args[1] for args, _kwargs in finishes.calls]
        assert written_states == [JobState.DONE] * (failed_writes + 1)

    def test_a_failed_error_write_is_replayed_with_the_pipeline_error(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        The owed ERROR keeps the pipeline's own text and category.

        The failure-path write raising must not replace the scanner's error
        with the job store's ``disk I/O error``.
        """

        def jammed(*_args: object, **_kwargs: object) -> ScanResult:
            msg = "Scanner jammed"
            raise ScanError(msg)

        monkeypatch.setattr("saneless.worker.run_pipeline", jammed)
        store = JobStore()
        finishes = _StoreFault(store.finish_job, frozenset({1}))
        monkeypatch.setattr(store, "finish_job", finishes)
        worker = worker_for(store)
        try:
            worker.start()
            job = _submit_jobs(worker, store, 1)[0]
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            worker.stop()
            store.close()

        assert finished.state is JobState.ERROR
        assert finished.error == "Scanner jammed"
        assert finished.error_category == classify_error(ScanError("Scanner jammed"))

    def test_a_failed_best_effort_write_is_retried_until_it_lands(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A row the guard could not end still ends on its own.

        The job's handling and the guard's ERROR write both fail once with a
        store error.  That is one loop-level failure, well below degraded, so
        no probe ever runs; the owed write must still land on a later idle
        tick, with no restart and no scan.  Only once that row is terminal is
        a second job submitted, so a newer row cannot hide a stranded one.
        """
        caplog.set_level(logging.INFO, logger="saneless.worker")
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", _FAST_TICK)
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            lambda *_args, **_kwargs: _success_result(),
        )
        store = JobStore()
        finishes = _StoreFault(store.finish_job, frozenset({1}))
        monkeypatch.setattr(store, "finish_job", finishes)
        worker = worker_for(store)
        _fail_the_job_path(worker, monkeypatch, frozenset({1}))
        try:
            worker.start()
            first = store.create_job("default", "Twice Unrecordable")
            worker.submit(first)
            failed = wait_for_state(store, first.id, TERMINAL_STATES, _STATE_BUDGET)
            second = store.create_job("default", "After Both Failures")
            worker.submit(second)
            finished = wait_for_state(store, second.id, TERMINAL_STATES, _STATE_BUDGET)
            alive = worker.is_alive
            health = worker.health
        finally:
            worker.stop()
            store.close()

        assert failed.state is JobState.ERROR
        assert failed.error == _DISK_ERROR
        assert failed.error_category == classify_error(
            sqlite3.OperationalError(_DISK_ERROR)
        )
        assert finished.state is JobState.DONE
        assert alive
        assert health is WorkerHealth.HEALTHY
        records = _worker_records(caplog, logging.WARNING, first.id)
        assert records
        assert records[0].exc_info is not None

    @pytest.mark.parametrize("stranded", [1, 2])
    def test_owed_failures_below_the_degraded_threshold_are_written_once_the_store_heals(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
        stranded: int,
    ) -> None:
        """
        Owed writes are retried on every idle tick, not probed.

        ``stranded`` loop-level failures stay below ``_DEGRADED_AFTER``, and the
        guard's ERROR write for each fails too.  Failed retries are not
        loop-level failures, and the streak limit is raised so the observation
        window before healing cannot reach it (``TestOwedWriteStreak`` pins
        the limit), so the worker stays HEALTHY and never probes;
        once the store accepts writes, every stranded row reaches ERROR with the
        guard's own text.
        """
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", _FAST_TICK)
        monkeypatch.setattr(
            "saneless.worker._OWED_RETRY_DEGRADED_AFTER", _UNREACHABLE_STREAK
        )
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            lambda *_args, **_kwargs: _success_result(),
        )
        store = JobStore()
        finishes = _StoreFault(store.finish_job, None)
        probes = _StoreFault(store.probe, frozenset())
        monkeypatch.setattr(store, "finish_job", finishes)
        monkeypatch.setattr(store, "probe", probes)
        worker = worker_for(store)
        _fail_the_job_path(worker, monkeypatch, frozenset(range(1, stranded + 1)))
        try:
            worker.start()
            jobs = _submit_jobs(worker, store, stranded)
            # The guard's write per job, then at least two idle-tick retries.
            retried = poll_until(
                lambda: len(finishes.calls) >= stranded + 2, _STATE_BUDGET
            )
            still_active = [_get(store, job.id).is_active for job in jobs]
            health_before = worker.health
            failures_before = worker._consecutive_loop_failures
            probes_before = probes.calls
            finishes.heal()
            failed = [
                wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
                for job in jobs
            ]
            latest = store.latest_run_job()
            # The flush drops an entry just after its write lands.
            assert poll_until(lambda: not worker._unrecorded_failures, _STATE_BUDGET)
            with worker._unrecorded_lock:
                owed_after = dict(worker._unrecorded_failures)
            health_after = worker.health
        finally:
            worker.stop()
            store.close()

        assert retried
        assert still_active == [True] * stranded
        assert health_before is WorkerHealth.HEALTHY
        assert failures_before == stranded
        assert probes_before == []
        expected_category = classify_error(sqlite3.OperationalError(_DISK_ERROR))
        for row in failed:
            assert row.state is JobState.ERROR
            assert row.error == _DISK_ERROR
            assert row.error_category == expected_category
        assert latest is not None
        assert not latest.is_active
        assert owed_after == {}
        assert probes.calls == []
        assert health_after is WorkerHealth.HEALTHY


# Owed rejections the many-thread test hands over, and the threads handing them.
_OWING_THREADS = 4
_OWED_PER_THREAD = 25
_MANY_OWED_BUDGET = 5.0


class TestOwedRejections:
    """
    A refused submit whose REJECTED write failed is written by the worker.

    The route creates the row before ``submit()``, so a refusal needs a
    second write, and when that write fails the row would stay PENDING with
    no REJECTED marker.  The route owes it to the worker instead, and the
    worker's idle flush -- shared with the guard's owed failures -- writes
    it on its next tick.  A failed retry is never a loop-level failure,
    while a streak of them degrades the worker.
    """

    def test_an_owed_rejection_is_written_on_the_next_idle_tick(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A healthy worker records an owed rejection without probing."""
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", _FAST_TICK)
        store = JobStore()
        probes = _StoreFault(store.probe, frozenset())
        monkeypatch.setattr(store, "probe", probes)
        worker = worker_for(store)
        try:
            worker.start()
            job = store.create_job("default", "Refused")
            worker.owe_rejection(job.id, "queue full")
            recorded = wait_for_state(store, job.id, JobState.ERROR, _STATE_BUDGET)
            latest = store.latest_run_job()
            health = worker.health
        finally:
            worker.stop()
            store.close()

        assert recorded.error == "queue full"
        assert recorded.error_category is ErrorCategory.REJECTED
        assert latest is None
        assert health is WorkerHealth.HEALTHY
        assert probes.calls == []

    def test_a_failed_owed_rejection_retry_is_not_a_loop_level_failure(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A request-side debt the store refuses is retried, never a loop failure.

        The streak limit is raised so the three-failure window cannot degrade
        the worker by that route either.
        """
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", _FAST_TICK)
        monkeypatch.setattr(
            "saneless.worker._OWED_RETRY_DEGRADED_AFTER", _UNREACHABLE_STREAK
        )
        store = JobStore()
        finishes = _StoreFault(store.finish_job, None)
        monkeypatch.setattr(store, "finish_job", finishes)
        worker = worker_for(store)
        try:
            worker.start()
            job = store.create_job("default", "Refused While Failing")
            worker.owe_rejection(job.id, "queue full")
            retried = poll_until(lambda: len(finishes.calls) >= 3, _STATE_BUDGET)
            state_before = _get(store, job.id).state
            failures_before = worker._consecutive_loop_failures
            health_before = worker.health
            finishes.heal()
            recorded = wait_for_state(store, job.id, JobState.ERROR, _STATE_BUDGET)
        finally:
            worker.stop()
            store.close()

        assert retried
        assert state_before is JobState.PENDING
        assert failures_before == 0
        assert health_before is WorkerHealth.HEALTHY
        assert recorded.error == "queue full"
        assert recorded.error_category is ErrorCategory.REJECTED

    def test_owed_rejections_from_many_threads_are_all_written(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Rejections owed while the worker flushes are none of them lost."""
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", _FAST_TICK)
        store = JobStore()
        worker = worker_for(store)
        barrier = threading.Barrier(_OWING_THREADS)
        raised: list[BaseException] = []
        raised_lock = threading.Lock()

        def owe(job_ids: list[str]) -> None:
            barrier.wait()
            try:
                for job_id in job_ids:
                    worker.owe_rejection(job_id, "queue full")
            except Exception as exc:
                with raised_lock:
                    raised.append(exc)

        try:
            worker.start()
            ids = [
                store.create_job("default", f"Refused {n}").id
                for n in range(_OWING_THREADS * _OWED_PER_THREAD)
            ]
            threads = [
                threading.Thread(
                    target=owe,
                    args=(ids[n * _OWED_PER_THREAD : (n + 1) * _OWED_PER_THREAD],),
                    daemon=True,
                )
                for n in range(_OWING_THREADS)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(_MANY_OWED_BUDGET)
            assert not any(thread.is_alive() for thread in threads)
            if raised:
                raise raised[0]

            def all_recorded() -> bool:
                # The flush drops an entry just after its write lands.  A row
                # that is gone was trimmed under the refused-row cap, which
                # deletes only rows already marked REJECTED, so its rejection
                # was written too; a lost one would leave its row PENDING.
                written = all(
                    (job := store.get_job(job_id)) is None
                    or job.state is JobState.ERROR
                    for job_id in ids
                )
                return written and not worker._unrecorded_failures

            recorded = poll_until(all_recorded, _MANY_OWED_BUDGET)
            rows = [job for job_id in ids if (job := store.get_job(job_id))]
            owed_after = dict(worker._unrecorded_failures)
        finally:
            worker.stop()
            store.close()

        assert recorded
        # Every one of the hundred was written, and the cap then kept the
        # newest REJECTED_HISTORY_ROWS of them.
        assert len(rows) == REJECTED_HISTORY_ROWS
        assert all(row.error_category is ErrorCategory.REJECTED for row in rows)
        assert all(row.error == "queue full" for row in rows)
        assert owed_after == {}

    def test_an_id_re_owed_while_its_write_is_in_flight_keeps_the_newer_write(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        The flush drops an owed entry only if it is unchanged.

        The first write for an id is held inside ``finish_job`` while the id is
        owed again with different text.  The flush must keep the newer entry
        rather than delete it unconditionally, and write it on a later tick.
        """
        first_error = "queue full"
        second_error = "service down"
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", _FAST_TICK)
        store = JobStore()
        original_finish = store.finish_job
        entered = threading.Event()
        release = threading.Event()
        written: list[str | None] = []
        written_lock = threading.Lock()

        def held_finish(
            job_id: str,
            state: JobState,
            result: JobResult | None = None,
            error: str | None = None,
            error_category: ErrorCategory | None = None,
        ) -> None:
            with written_lock:
                written.append(error)
                first_call = len(written) == 1
            if first_call:
                entered.set()
                release.wait(_STATE_BUDGET * 5)
            original_finish(
                job_id,
                state,
                result=result,
                error=error,
                error_category=error_category,
            )

        monkeypatch.setattr(store, "finish_job", held_finish)
        worker = worker_for(store)
        try:
            worker.start()
            job = store.create_job("default", "Owed Twice")
            worker.owe_rejection(job.id, first_error)
            assert entered.wait(_STATE_BUDGET)
            worker.owe_rejection(job.id, second_error)
            release.set()
            rewritten = poll_until(
                lambda: (
                    _get(store, job.id).error == second_error
                    and not worker.owed_rejection_ids()
                ),
                _STATE_BUDGET,
            )
            row = _get(store, job.id)
            with written_lock:
                written_before_stop = list(written)
        finally:
            release.set()
            worker.stop()
            store.close()

        assert rewritten
        assert written_before_stop[:2] == [first_error, second_error]
        assert row.error_category is ErrorCategory.REJECTED

    def test_owed_rejection_ids_lists_only_rejections_still_owed(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Only owed REJECTED writes are listed, and only until they are written.

        The status area skips these ids so a refused attempt is never shown
        as the live job.  A failure the loop guard could not write belongs
        to a job that ran, so it is left out and that job is still shown.
        The streak limit is raised so the failing window cannot degrade.
        """
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", _FAST_TICK)
        monkeypatch.setattr(
            "saneless.worker._OWED_RETRY_DEGRADED_AFTER", _UNREACHABLE_STREAK
        )
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            lambda *_args, **_kwargs: _success_result(),
        )
        store = JobStore()
        finishes = _StoreFault(store.finish_job, None)
        monkeypatch.setattr(store, "finish_job", finishes)
        worker = worker_for(store)
        _fail_the_job_path(worker, monkeypatch, frozenset({1}))
        try:
            worker.start()
            guard = _submit_jobs(worker, store, 1)[0]
            assert poll_until(
                lambda: guard.id in worker._unrecorded_failures, _STATE_BUDGET
            )
            refused = store.create_job("default", "Refused")
            worker.owe_rejection(refused.id, "queue full")
            owed = worker.owed_rejection_ids()
            finishes.heal()
            wait_for_state(store, refused.id, JobState.ERROR, _STATE_BUDGET)
            assert poll_until(lambda: not worker.owed_rejection_ids(), _STATE_BUDGET)
            after = worker.owed_rejection_ids()
        finally:
            worker.stop()
            store.close()

        assert owed == frozenset({refused.id})
        assert isinstance(owed, frozenset)
        assert after == frozenset()

    def test_an_owed_rejection_is_written_as_the_worker_stops(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A rejection still owed at shutdown is written before the thread exits.

        No idle tick can run in the test's window, so only the exit flush can
        write it.  Unwritten, the row would come back after a restart as a
        PENDING row recovered as "server restarted".
        """
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", 3600.0)
        store = JobStore()
        worker = worker_for(store)
        try:
            worker.start()
            job = store.create_job("default", "Refused Before Shutdown")
            worker.owe_rejection(job.id, "queue full")
            stopped = worker.stop()
            row = _get(store, job.id)
            owed_after = worker.owed_rejection_ids()
        finally:
            worker.stop()
            store.close()

        assert stopped is True
        assert row.state is JobState.ERROR
        assert row.error == "queue full"
        assert row.error_category is ErrorCategory.REJECTED
        assert owed_after == frozenset()

    def test_a_failed_exit_flush_is_logged_and_the_worker_still_stops(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A store that refuses the exit flush cannot hold the stop."""
        caplog.set_level(logging.INFO, logger="saneless.worker")
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", 3600.0)
        store = JobStore()
        finishes = _StoreFault(store.finish_job, None)
        monkeypatch.setattr(store, "finish_job", finishes)
        worker = worker_for(store)
        try:
            worker.start()
            job = store.create_job("default", "Refused While Broken")
            worker.owe_rejection(job.id, "queue full")
            stopped = worker.stop()
            row = _get(store, job.id)
        finally:
            worker.stop()
            store.close()

        assert stopped is True
        assert len(finishes.calls) == 1
        assert row.state is JobState.PENDING
        records = _worker_records(
            caplog, logging.WARNING, "Could not write owed job records"
        )
        assert len(records) == 1
        assert records[0].exc_info is not None


class TestOwedWriteStreak:
    """
    A streak of failed owed-write retries degrades the worker.

    A failed retry is not a loop-level failure, but
    ``_OWED_RETRY_DEGRADED_AFTER`` failed idle ticks in a row degrade the
    worker, and the probe-and-flush recovery path clears it again.  Without
    the streak, a job store that never heals would leave the stuck row live
    and ``/health`` at 200 until an hourly prune failed.
    """

    @pytest.mark.parametrize("debt", ["guard", "rejection"])
    def test_a_persistent_owed_write_fault_degrades_the_worker_in_bounded_idle_ticks(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        debt: str,
    ) -> None:
        """
        An owed write the store never accepts reaches DEGRADED.

        The debt is either the guard's (a store error escaping a job, whose
        ERROR write failed too, one loop-level failure) or a request-side rejection
        (no loop-level failure at all).  Either way ``finish_job`` never heals,
        so every idle retry fails, and the worker must degrade within
        ``_OWED_RETRY_DEGRADED_AFTER`` ticks rather than hours later.
        """
        production_tick = worker_module._IDLE_TICK_SECONDS
        caplog.set_level(logging.INFO, logger="saneless.worker")
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", _FAST_TICK)
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            lambda *_args, **_kwargs: _success_result(),
        )
        store = JobStore()
        finishes = _StoreFault(store.finish_job, None)
        probes = _StoreFault(store.probe, frozenset())
        monkeypatch.setattr(store, "finish_job", finishes)
        monkeypatch.setattr(store, "probe", probes)
        worker = worker_for(store)
        if debt == "guard":
            _fail_the_job_path(worker, monkeypatch, frozenset({1}))
        try:
            worker.start()
            if debt == "guard":
                job = _submit_jobs(worker, store, 1)[0]
            else:
                job = store.create_job("default", "Refused While Broken")
                worker.owe_rejection(job.id, "queue full")
            degraded = poll_until(
                lambda: worker.health is WorkerHealth.DEGRADED, _STATE_BUDGET
            )
            assert degraded
            probed = poll_until(lambda: len(probes.calls) >= 1, _STATE_BUDGET)
            streak = worker._failed_owed_retries
            loop_failures = worker._consecutive_loop_failures
            row = _get(store, job.id)
            alive = worker.is_alive
            refused = worker.submit(store.create_job("default", "While Degraded"))
        finally:
            worker.stop()
            store.close()

        assert probed
        assert streak == worker_module._OWED_RETRY_DEGRADED_AFTER
        assert worker_module._OWED_RETRY_DEGRADED_AFTER >= 2
        assert worker_module._OWED_RETRY_DEGRADED_AFTER * production_tick <= 30.0
        assert loop_failures == (1 if debt == "guard" else 0)
        assert row.is_active
        assert alive
        assert refused is SubmitResult.DEGRADED
        retry_warnings = _worker_records(
            caplog, logging.WARNING, "Owed job store writes failed"
        )
        assert len(retry_warnings) == 1
        assert retry_warnings[0].exc_info is not None
        assert _worker_records(caplog, logging.WARNING, "degraded")

    @pytest.mark.parametrize("debt", ["guard", "rejection"])
    def test_a_worker_degraded_by_a_failed_retry_streak_recovers_once_the_owed_write_lands(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        debt: str,
    ) -> None:
        """
        A streak-degraded worker heals through probe and flush.

        The debt is the guard's ERROR write or a request-side REJECTED write.
        Once the store accepts writes again, the probe succeeds and the owed
        write lands through the recovery path, so degraded clears, both failure
        counts are back to zero, the stuck row carries the owed text, no
        rejection is still listed as owed, and a new scan runs.
        """
        caplog.set_level(logging.INFO, logger="saneless.worker")
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", _FAST_TICK)
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            lambda *_args, **_kwargs: _success_result(),
        )
        store = JobStore()
        finishes = _StoreFault(store.finish_job, None)
        probes = _StoreFault(store.probe, frozenset())
        monkeypatch.setattr(store, "finish_job", finishes)
        monkeypatch.setattr(store, "probe", probes)
        worker = worker_for(store)
        if debt == "guard":
            _fail_the_job_path(worker, monkeypatch, frozenset({1}))
        try:
            worker.start()
            first = _owe_one_write(worker, store, debt)
            degraded = poll_until(
                lambda: worker.health is WorkerHealth.DEGRADED, _STATE_BUDGET
            )
            assert degraded
            owed_while_degraded = worker.owed_rejection_ids()
            finishes.heal()
            recovered = poll_until(
                lambda: worker.health is WorkerHealth.HEALTHY, _STATE_BUDGET
            )
            row = wait_for_state(store, first.id, JobState.ERROR, _STATE_BUDGET)
            # The flush drops an entry just after its write lands.
            drained = poll_until(lambda: not worker._unrecorded_failures, _STATE_BUDGET)
            with worker._unrecorded_lock:
                owed_after = dict(worker._unrecorded_failures)
            owed_rejections_after = worker.owed_rejection_ids()
            probed = len(probes.calls)
            streak_after = worker._failed_owed_retries
            loop_after = worker._consecutive_loop_failures
            second = _submit_jobs(worker, store, 1)[0]
            finished = wait_for_state(store, second.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            worker.stop()
            store.close()

        assert recovered
        assert drained
        # Degraded until the owed write landed, so only the probe path wrote it.
        assert probed >= 1
        expected = {
            "guard": (
                _DISK_ERROR,
                classify_error(sqlite3.OperationalError(_DISK_ERROR)),
                frozenset(),
            ),
            "rejection": ("queue full", ErrorCategory.REJECTED, frozenset({first.id})),
        }
        assert (row.error, row.error_category, owed_while_degraded) == expected[debt]
        assert owed_after == {}
        assert owed_rejections_after == frozenset()
        assert streak_after == 0
        assert loop_after == 0
        assert finished.state is JobState.DONE
        assert _worker_records(caplog, logging.INFO, "recovered")

    def test_a_job_whose_writes_all_land_ends_the_owed_write_streak(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A cleanly recorded job breaks the streak as it breaks the run.

        Idle ticks come only between jobs, so without this a streak could span
        a job that proved the store accepts writes, and one more failed tick
        would degrade a worker whose store is fine.  The streak is seeded one
        short of the limit and no idle tick can run in the window, so only the
        job itself can reset it.
        """
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", 3600.0)
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            lambda *_args, **_kwargs: _success_result(),
        )
        store = JobStore()
        worker = worker_for(store)
        worker._failed_owed_retries = worker_module._OWED_RETRY_DEGRADED_AFTER - 1
        try:
            worker.start()
            job = _submit_jobs(worker, store, 1)[0]
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
            reset = poll_until(lambda: worker._failed_owed_retries == 0, _STATE_BUDGET)
            health = worker.health
        finally:
            worker.stop()
            store.close()

        assert finished.state is JobState.DONE
        assert reset
        assert health is WorkerHealth.HEALTHY

    @staticmethod
    def _scripted_flush(
        worker: ScanWorker, monkeypatch: pytest.MonkeyPatch, outcomes: list[bool]
    ) -> list[bool]:
        """
        Make the worker's owed-write retries fail or land as scripted.

        Each retry takes the next outcome, True for a retry that lands; once
        the script runs out every retry lands.

        Args:
            worker: The worker whose retries are scripted.
            monkeypatch: pytest's patcher.
            outcomes: Whether each retry lands, in order.

        Returns:
            The outcomes of the retries made so far, appended as they run.

        """
        made: list[bool] = []

        def flush() -> int:
            """
            Land or fail as the script says.

            Returns:
                No rows written: a landed retry had nothing owed.

            Raises:
                sqlite3.OperationalError: For a retry scripted to fail.

            """
            lands = outcomes[len(made)] if len(made) < len(outcomes) else True
            made.append(lands)
            if not lands:
                raise sqlite3.OperationalError(_DISK_ERROR)
            return 0

        monkeypatch.setattr(worker, "_flush_unrecorded_failures", flush)
        return made

    def test_a_retry_failing_after_every_clean_job_warns_once(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        One failure episode is one WARNING, however many jobs it spans.

        A clean job ends the streak that degrades the worker, so on a busy
        queue every retry after a job is the first of a streak.  The WARNING
        with its traceback must still not repeat after every scan while the
        same write keeps failing: only a retry that lands ends the episode.
        """
        caplog.set_level(logging.DEBUG, logger="saneless.worker")
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", 3600.0)
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            lambda *_args, **_kwargs: _success_result(),
        )
        store = JobStore()
        worker = worker_for(store)
        made = self._scripted_flush(worker, monkeypatch, [False, False, False])
        try:
            worker.start()
            for _ in range(3):
                job = _submit_jobs(worker, store, 1)[0]
                wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
            retried = poll_until(lambda: len(made) >= 3, _STATE_BUDGET)
            health = worker.health
        finally:
            worker.stop()
            store.close()

        assert retried
        assert health is WorkerHealth.HEALTHY
        failures = "Owed job store writes failed"
        assert len(_worker_records(caplog, logging.WARNING, failures)) == 1
        assert len(_worker_records(caplog, logging.DEBUG, failures)) == 2

    def test_a_retry_that_lands_ends_the_episode_and_rearms_the_warning(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A failure after a landed retry is a new episode, and is warned about."""
        caplog.set_level(logging.DEBUG, logger="saneless.worker")
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", 3600.0)
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            lambda *_args, **_kwargs: _success_result(),
        )
        store = JobStore()
        worker = worker_for(store)
        made = self._scripted_flush(worker, monkeypatch, [False, True, False])
        try:
            worker.start()
            for _ in range(3):
                job = _submit_jobs(worker, store, 1)[0]
                wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
            retried = poll_until(lambda: len(made) >= 3, _STATE_BUDGET)
        finally:
            worker.stop()
            store.close()

        assert retried
        failures = "Owed job store writes failed"
        assert len(_worker_records(caplog, logging.WARNING, failures)) == 2
        assert _worker_records(caplog, logging.DEBUG, failures) == []

    def test_owed_write_retries_failing_fewer_ticks_than_the_streak_never_degrade(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A fault that heals inside the streak never degrades.

        For each of two jobs the guard's ERROR write fails, then one tick fewer
        than the streak limit of retries fail, then the next retry lands.  A
        retry that lands resets the streak, so the two short streaks never add
        up: no probe runs, the worker stays HEALTHY, and each streak logs its
        first failed retry at WARNING exactly once.
        """
        # Read from the production streak limit, never mirrored, so the streaks
        # below stay exactly one tick short of it if the limit changes.
        threshold = worker_module._OWED_RETRY_DEGRADED_AFTER
        caplog.set_level(logging.INFO, logger="saneless.worker")
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", _FAST_TICK)
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            lambda *_args, **_kwargs: _success_result(),
        )
        store = JobStore()
        # One finish call per tick while one entry is owed: the guard's write
        # and threshold - 1 retries fail, then the next retry lands -- per job.
        finish_job = _StoreFault(
            store.finish_job,
            frozenset(range(1, threshold + 1))
            | frozenset(range(threshold + 2, 2 * threshold + 2)),
        )
        probes = _StoreFault(store.probe, frozenset())
        monkeypatch.setattr(store, "finish_job", finish_job)
        monkeypatch.setattr(store, "probe", probes)
        worker = worker_for(store)
        _fail_the_job_path(worker, monkeypatch, frozenset({1, 2}))
        try:
            worker.start()
            first = _submit_jobs(worker, store, 1)[0]
            first_row = wait_for_state(store, first.id, JobState.ERROR, _STATE_BUDGET)
            second = _submit_jobs(worker, store, 1)[0]
            second_row = wait_for_state(store, second.id, JobState.ERROR, _STATE_BUDGET)
            drained = poll_until(lambda: not worker._unrecorded_failures, _STATE_BUDGET)
            health = worker.health
        finally:
            worker.stop()
            store.close()

        assert drained
        assert first_row.error == _DISK_ERROR
        assert second_row.error == _DISK_ERROR
        assert probes.calls == []
        assert health is WorkerHealth.HEALTHY
        retry_warnings = _worker_records(
            caplog, logging.WARNING, "Owed job store writes failed"
        )
        assert len(retry_warnings) == 2

    def test_a_non_empty_owed_flush_resets_the_loop_failure_count(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        An owed write that lands ends the run of loop-level failures.

        A store error escapes the job, one loop-level failure, and the
        guard's ERROR write fails too, so the ERROR is owed.  The next idle
        tick writes it: the store just accepted a write, so the run of
        failures before it is over.
        """
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", _FAST_TICK)
        store = JobStore()
        monkeypatch.setattr(
            store, "finish_job", _StoreFault(store.finish_job, frozenset({1}))
        )
        worker = worker_for(store)
        _fail_the_job_path(worker, monkeypatch, frozenset({1}))
        try:
            worker.start()
            job = _submit_jobs(worker, store, 1)[0]
            row = wait_for_state(store, job.id, JobState.ERROR, _STATE_BUDGET)
            # The flush drops the entry just after its write lands.
            drained = poll_until(lambda: not worker._unrecorded_failures, _STATE_BUDGET)
        finally:
            worker.stop()
            store.close()

        assert row.error == _DISK_ERROR
        assert drained
        assert worker._consecutive_loop_failures == 0

    def test_an_idle_tick_with_nothing_owed_keeps_the_loop_failure_count(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A flush that writes nothing proves nothing about the store.

        A store error escapes the job and the guard's ERROR write lands, so
        nothing is owed.  Idle ticks then flush an empty set, which touches no
        store, and the loop-level failure still counts.
        """
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", _FAST_TICK)
        store = JobStore()
        worker = worker_for(store)
        _fail_the_job_path(worker, monkeypatch, frozenset({1}))
        ticks = _count_idle_ticks(worker, monkeypatch)
        try:
            worker.start()
            job = _submit_jobs(worker, store, 1)[0]
            row = wait_for_state(store, job.id, JobState.ERROR, _STATE_BUDGET)
            ticked_from = ticks()
            ticked = poll_until(
                lambda: ticks() >= ticked_from + _QUIET_TICKS, _STATE_BUDGET
            )
            loop_failures = worker._consecutive_loop_failures
        finally:
            worker.stop()
            store.close()

        assert row.error == _DISK_ERROR
        assert ticked
        assert loop_failures == 1


class TestOwedWritesAfterAJob:
    """
    Owed writes are retried after every clean job, not only on an idle tick.

    A queue that never empties never gives the worker an idle tick, so an owed
    row retried only there would stay owed for as long as scans keep coming.
    Each test takes the idle tick out of the window and snapshots what is owed
    as every scan starts: the job before it has finished its loop pass by then,
    so the snapshot cannot race the flush.
    """

    def test_owed_writes_flush_after_a_job_when_the_queue_is_busy(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An owed rejection is written before the third job in a row scans."""
        owed_at_scan: list[frozenset[str]] = []
        store = JobStore()
        worker = worker_for(store)

        def snapshotting_pipeline(*_args: object, **_kwargs: object) -> ScanResult:
            owed_at_scan.append(worker.owed_rejection_ids())
            return _success_result()

        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", 3600.0)
        monkeypatch.setattr("saneless.worker.run_pipeline", snapshotting_pipeline)
        ticks = _count_idle_ticks(worker, monkeypatch)
        try:
            worker.start()
            refused = store.create_job("default", "Refused While Busy")
            worker.owe_rejection(refused.id, "queue full")
            jobs = _submit_jobs(worker, store, 3)
            finished = [
                wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
                for job in jobs
            ]
            row = _get(store, refused.id)
            idle_ticks = ticks()
        finally:
            worker.stop()
            store.close()

        assert [job.state for job in finished] == [JobState.DONE] * 3
        assert idle_ticks == 0
        assert owed_at_scan == [frozenset({refused.id}), frozenset(), frozenset()]
        assert row.state is JobState.ERROR
        assert row.error == "queue full"
        assert row.error_category is ErrorCategory.REJECTED

    def test_a_degraded_worker_leaves_the_owed_flush_to_recovery(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        mock_scanner: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A clean job on a degraded worker does not retry owed writes.

        Recovery owns the retry while degraded: it probes first, and clearing
        degraded is its call.  Startup generation is held so every job is
        queued before the worker takes one; a store error escapes each of the
        first jobs until the worker degrades, and the two queued after them
        then run cleanly on a degraded worker.
        """
        entered = threading.Event()
        released = threading.Event()
        owed_at_scan: list[frozenset[str]] = []
        store = JobStore()
        worker = worker_for(store)

        def held_devices(*_args: object, **_kwargs: object) -> list[DeviceInfo]:
            entered.set()
            released.wait(_STATE_BUDGET * 5)
            return []

        def snapshotting_pipeline(*_args: object, **_kwargs: object) -> ScanResult:
            owed_at_scan.append(worker.owed_rejection_ids())
            return _success_result()

        mock_scanner.get_devices.side_effect = held_devices
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", 3600.0)
        monkeypatch.setattr("saneless.worker.run_pipeline", snapshotting_pipeline)
        _fail_the_job_path(
            worker, monkeypatch, frozenset(range(1, _DEGRADING_JOBS + 1))
        )
        try:
            worker.start()
            assert entered.wait(_STATE_BUDGET)
            refused = store.create_job("default", "Refused Before Degraded")
            worker.owe_rejection(refused.id, "queue full")
            _submit_jobs(worker, store, _DEGRADING_JOBS)
            clean = _submit_jobs(worker, store, 2)
            released.set()
            finished = [
                wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
                for job in clean
            ]
            health = worker.health
        finally:
            released.set()
            worker.stop()
            store.close()

        assert [job.state for job in finished] == [JobState.DONE] * 2
        assert health is WorkerHealth.DEGRADED
        assert owed_at_scan == [frozenset({refused.id})] * 2


# Loop-level failures in a row that make a worker degraded.
_DEGRADING_JOBS = 3


def _submit_jobs(worker: ScanWorker, store: JobStore, count: int) -> list[Job]:
    """
    Create and submit ``count`` jobs, asserting each was accepted.

    Returns:
        The submitted jobs, in submission order.

    """
    jobs = [store.create_job("default", f"Job {n}") for n in range(1, count + 1)]
    for job in jobs:
        assert worker.submit(job) is SubmitResult.ACCEPTED
    return jobs


def _owe_one_write(worker: ScanWorker, store: JobStore, debt: str) -> Job:
    """
    Leave a started worker owing one job-row write of the given kind.

    ``"guard"`` submits a job the caller has made raise a store error, so
    the guard owes its ERROR write when that fails too; ``"rejection"``
    owes a refused submit's REJECTED write, as the scan route does.

    Returns:
        The job whose row is owed.

    """
    if debt == "guard":
        return _submit_jobs(worker, store, 1)[0]
    job = store.create_job("default", "Refused While Broken")
    worker.owe_rejection(job.id, "queue full")
    return job


def _degrade(worker: ScanWorker, store: JobStore) -> list[Job]:
    """
    Drive a started worker whose every job raises a store error into DEGRADED.

    ``_DEGRADING_JOBS`` jobs make that many loop-level failures in a row
    (D-10).  Degraded is set before the guard's write for the last job, so a
    caller that heals the store right away may see that one row written by
    the guard rather than by recovery -- with the same text either way.

    Returns:
        The jobs whose loop-level failures degraded the worker.

    """
    jobs = _submit_jobs(worker, store, _DEGRADING_JOBS)
    assert poll_until(lambda: worker.health is WorkerHealth.DEGRADED, _STATE_BUDGET)
    return jobs


class TestWorkerDegradedHealth:
    """
    The worker reports HEALTHY, DEGRADED or DOWN, and heals on its own.

    D-10: three consecutive loop-level failures -- the loop's own job store
    writes raising -- make the worker DEGRADED while its thread stays alive.
    D-11: a degraded worker rejects scans with ``SubmitResult.DEGRADED``, so
    nobody feeds paper into a job that cannot be recorded.  D-12: each idle
    tick while degraded probes the store; the first success clears DEGRADED
    with no scan needed.  Research Pitfall 6: that recovery also ends rows the
    guard could not end, using only the texts that already exist.
    """

    def test_health_is_down_unless_the_thread_runs_not_degraded(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
    ) -> None:
        """A worker not started, or stopped, is DOWN; a running one is HEALTHY."""
        store = JobStore()
        worker = worker_for(store)
        try:
            before = worker.health
            worker.start()
            running = worker.health
            worker.stop()
            after = worker.health
        finally:
            worker.stop()
            store.close()

        assert before is WorkerHealth.DOWN
        assert running is WorkerHealth.HEALTHY
        assert after is WorkerHealth.DOWN

    def test_three_loop_failures_in_a_row_make_the_worker_degraded(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """D-10, D-11: degraded after three store failures, alive, and rejecting."""
        caplog.set_level(logging.INFO, logger="saneless.worker")
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", _FAST_TICK)
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            lambda *_args, **_kwargs: _success_result(),
        )
        store = JobStore()
        monkeypatch.setattr(store, "probe", _StoreFault(store.probe))
        worker = worker_for(store)
        _fail_the_job_path(worker, monkeypatch)
        try:
            worker.start()
            _degrade(worker, store)
            health = worker.health
            alive = worker.is_alive
            fourth = worker.submit(store.create_job("default", "While Degraded"))
        finally:
            worker.stop()
            store.close()

        assert health is WorkerHealth.DEGRADED
        assert alive
        assert fourth is SubmitResult.DEGRADED
        assert _worker_records(caplog, logging.WARNING, "degraded")

    def test_degraded_needs_consecutive_failures_not_cumulative_ones(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """D-10: two failures, a success, two failures -- still HEALTHY."""
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", _FAST_TICK)
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            lambda *_args, **_kwargs: _success_result(),
        )
        store = JobStore()
        # A wrongly degraded worker must stay degraded for the assertion to see.
        monkeypatch.setattr(store, "probe", _StoreFault(store.probe))
        worker = worker_for(store)
        _fail_the_job_path(worker, monkeypatch, frozenset({1, 2, 4, 5}))
        try:
            worker.start()
            jobs = _submit_jobs(worker, store, 5)
            finished = [
                wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
                for job in jobs
            ]
            health = worker.health
        finally:
            worker.stop()
            store.close()

        assert [job.state for job in finished] == [
            JobState.ERROR,
            JobState.ERROR,
            JobState.DONE,
            JobState.ERROR,
            JobState.ERROR,
        ]
        assert health is WorkerHealth.HEALTHY

    def test_a_successful_probe_clears_degraded(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """D-12: a failing probe keeps DEGRADED; the first success heals it."""
        caplog.set_level(logging.INFO, logger="saneless.worker")
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", _FAST_TICK)
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            lambda *_args, **_kwargs: _success_result(),
        )
        store = JobStore()
        probes = _StoreFault(store.probe)
        monkeypatch.setattr(store, "probe", probes)
        worker = worker_for(store)
        jobs_fault = _fail_the_job_path(worker, monkeypatch)
        try:
            worker.start()
            _degrade(worker, store)
            probed = poll_until(lambda: len(probes.calls) >= 3, _STATE_BUDGET)
            still_degraded = worker.health
            jobs_fault.heal()
            probes.heal()
            recovered = poll_until(
                lambda: worker.health is WorkerHealth.HEALTHY, _STATE_BUDGET
            )
            job = store.create_job("default", "After Recovery")
            accepted = worker.submit(job)
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            worker.stop()
            store.close()

        assert probed
        assert still_degraded is WorkerHealth.DEGRADED
        assert recovered
        assert accepted is SubmitResult.ACCEPTED
        assert finished.state is JobState.DONE
        assert _worker_records(caplog, logging.INFO, "recovered")

    def test_a_degraded_worker_on_a_read_only_store_stays_degraded(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        tmp_path: Path,
    ) -> None:
        """
        A degraded worker whose store reads but cannot be written stays degraded.

        Reads still succeed on a read-only store, so only the probe's write
        tells it from a healthy one; a worker that healed on reads alone would
        accept scans it cannot record.
        """
        caplog.set_level(logging.INFO, logger="saneless.worker")
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", _FAST_TICK)
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            lambda *_args, **_kwargs: _success_result(),
        )
        store = JobStore(tmp_path / "jobs.db")
        probes = _StoreFault(store.probe)
        monkeypatch.setattr(store, "probe", probes)
        worker = worker_for(store)
        _fail_the_job_path(worker, monkeypatch)
        try:
            worker.start()
            jobs = _degrade(worker, store)
            for job in jobs:
                wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
            with store._lock:
                store._conn.execute("PRAGMA query_only = ON")
            failed_probes = len(probes.calls)
            probes.heal()
            probed = poll_until(
                lambda: len(probes.calls) >= failed_probes + 3, _STATE_BUDGET
            )
            health = worker.health
            recovered = _worker_records(caplog, logging.INFO, "recovered")
        finally:
            with store._lock:
                store._conn.execute("PRAGMA query_only = OFF")
            worker.stop()
            store.close()

        assert probed
        assert health is WorkerHealth.DEGRADED
        assert not recovered

    def test_degraded_recovery_ends_jobs_whose_failure_went_unrecorded(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Research Pitfall 6: a row the guard could not end is ended on recovery.

        Both the job's handling and the guard's ERROR write raise, so the rows
        stay PENDING through degraded.  The first successful probe writes the
        error the guard tried to write, and nothing is left active.
        """
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", _FAST_TICK)
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            lambda *_args, **_kwargs: _success_result(),
        )
        store = JobStore()
        faults = [_StoreFault(store.finish_job), _StoreFault(store.probe)]
        for name, fault in zip(("finish_job", "probe"), faults, strict=True):
            monkeypatch.setattr(store, name, fault)
        worker = worker_for(store)
        faults.append(_fail_the_job_path(worker, monkeypatch))
        try:
            worker.start()
            jobs = _degrade(worker, store)
            for fault in faults:
                fault.heal()
            recovered = poll_until(
                lambda: worker.health is WorkerHealth.HEALTHY, _STATE_BUDGET
            )
            rows = [_get(store, job.id) for job in jobs]
        finally:
            worker.stop()
            store.close()

        expected_category = classify_error(sqlite3.OperationalError(_DISK_ERROR))
        assert recovered
        assert [row.state for row in rows] == [JobState.ERROR] * _DEGRADING_JOBS
        assert all(row.error == _DISK_ERROR for row in rows)
        assert all(row.error_category is expected_category for row in rows)
        assert not any(row.is_active for row in rows)

    def test_mark_recovery_pending_starts_degraded_and_fails_orphans_on_recovery(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        D-13, research Open Question 1: a failed startup recovery starts degraded.

        The first successful probe fails the row the crashed process left
        SCANNING with ``RESTART_REASON``, then reports HEALTHY.
        """
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", _FAST_TICK)
        store = JobStore()
        orphan = store.create_job("default", "Left Scanning")
        store.update_state(orphan.id, JobState.SCANNING)
        probes = _StoreFault(store.probe)
        monkeypatch.setattr(store, "probe", probes)
        worker = worker_for(store)
        try:
            worker.mark_recovery_pending()
            worker.start()
            at_start = worker.health
            rejected = worker.submit(store.create_job("default", "Too Soon"))
            probes.heal()
            recovered = poll_until(
                lambda: worker.health is WorkerHealth.HEALTHY, _STATE_BUDGET
            )
            row = _get(store, orphan.id)
        finally:
            worker.stop()
            store.close()

        assert at_start is WorkerHealth.DEGRADED
        assert rejected is SubmitResult.DEGRADED
        assert recovered
        assert row.state is JobState.ERROR
        assert row.error == RESTART_REASON

    def test_recovered_workspace_texts_are_written_before_the_restart_reason(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A row whose pages startup recovered is told where they went.

        The kept sentences handed to ``mark_recovery_pending`` are written
        first, after the restart text, so the plain ``RESTART_REASON`` reaches
        only the rows with no sentence.
        """
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", _FAST_TICK)
        store = JobStore()
        recovered_job = store.create_job("default", "Pages Recovered")
        store.update_state(recovered_job.id, JobState.SCANNING)
        plain_job = store.create_job("default", "Nothing Recovered")
        store.update_state(plain_job.id, JobState.SCANNING)
        kept = "The scan was preserved at /data/failed/a.pdf"
        probes = _StoreFault(store.probe)
        monkeypatch.setattr(store, "probe", probes)
        worker = worker_for(store)
        try:
            worker.mark_recovery_pending({recovered_job.id: kept})
            worker.start()
            at_start = worker.health
            probes.heal()
            recovered = poll_until(
                lambda: worker.health is WorkerHealth.HEALTHY, _STATE_BUDGET
            )
            with_text = _get(store, recovered_job.id)
            without_text = _get(store, plain_job.id)
        finally:
            worker.stop()
            store.close()

        assert at_start is WorkerHealth.DEGRADED
        assert recovered
        assert (with_text.state, with_text.error) == (
            JobState.ERROR,
            f"{RESTART_REASON}. {kept}",
        )
        assert (without_text.state, without_text.error) == (
            JobState.ERROR,
            RESTART_REASON,
        )

    def test_recovery_replay_restart_words_an_uploading_row_as_maybe_delivered(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        The deferred recovery words each row by its state, as startup would.

        An uploading row, with or without kept pages, is the amber after-send
        category; a scanning row keeps the plain restart text.
        """
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", _FAST_TICK)
        store = JobStore()
        kept_upload = store.create_job("default", "Uploading, Pages Kept")
        store.update_state(kept_upload.id, JobState.UPLOADING)
        bare_upload = store.create_job("default", "Uploading, Nothing Kept")
        store.update_state(bare_upload.id, JobState.UPLOADING)
        scanning = store.create_job("default", "Scanning")
        store.update_state(scanning.id, JobState.SCANNING)
        kept = "The scan was preserved at /data/failed/c.pdf"
        probes = _StoreFault(store.probe)
        monkeypatch.setattr(store, "probe", probes)
        worker = worker_for(store)
        try:
            worker.mark_recovery_pending({kept_upload.id: kept})
            worker.start()
            probes.heal()
            recovered = poll_until(
                lambda: worker.health is WorkerHealth.HEALTHY, _STATE_BUDGET
            )
            rows = [_get(store, job.id) for job in (kept_upload, bare_upload, scanning)]
        finally:
            worker.stop()
            store.close()

        assert recovered
        assert [(row.state, row.error, row.error_category) for row in rows] == [
            (
                JobState.ERROR,
                f"{RESTART_UPLOADING_REASON}. {kept}",
                ErrorCategory.UNCONFIRMED_SEND,
            ),
            (JobState.ERROR, RESTART_UPLOADING_REASON, ErrorCategory.UNCONFIRMED_SEND),
            (JobState.ERROR, RESTART_REASON, None),
        ]

    def test_recovered_workspace_texts_survive_a_failed_write(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A first write of the texts that fails is retried, not dropped."""
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", _FAST_TICK)
        store = JobStore()
        job = store.create_job("default", "Pages Recovered")
        store.update_state(job.id, JobState.SCANNING)
        kept = "The scan was preserved at /data/failed/b.pdf"
        writes = _StoreFault(store.fail_recovered_jobs, frozenset({1}))
        monkeypatch.setattr(store, "fail_recovered_jobs", writes)
        worker = worker_for(store)
        try:
            worker.mark_recovery_pending({job.id: kept})
            worker.start()
            recovered = poll_until(
                lambda: worker.health is WorkerHealth.HEALTHY, _STATE_BUDGET
            )
            row = _get(store, job.id)
        finally:
            worker.stop()
            store.close()

        assert recovered
        assert len(writes.calls) == 2
        assert (row.state, row.error) == (JobState.ERROR, f"{RESTART_REASON}. {kept}")

    def test_probe_is_not_called_while_not_degraded(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """D-12: probing is the degraded worker's business only."""
        monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", _FAST_TICK)
        store = JobStore()
        probes = _StoreFault(store.probe, frozenset())
        monkeypatch.setattr(store, "probe", probes)
        worker = worker_for(store)
        ticks = _count_idle_ticks(worker, monkeypatch)
        try:
            worker.start()
            # A window in which nothing may happen, measured in idle ticks the
            # worker actually took rather than in wall time (IN-03).
            ticked = poll_until(lambda: ticks() >= _QUIET_TICKS, _STATE_BUDGET)
            health = worker.health
        finally:
            worker.stop()
            store.close()

        assert ticked
        assert health is WorkerHealth.HEALTHY
        assert probes.calls == []


# Rounds each thread runs in the profile lock stress test.
_PROFILE_LOCK_ROUNDS = 200


class TestWorkerProfileLock:
    """
    Every read and update of the worker's profiles goes through one lock (D-19).

    Request threads (the index dropdown, ROBU-08's unknown-profile check) and
    the worker's own lookup read the profiles while the worker may be replacing
    them.  Once routes run on the threadpool (ROBU-05) that is real
    concurrency, so reads are locked and updates rebind a new dict rather than
    mutating the one a reader may hold.
    """

    def test_profile_lock_names_are_a_copy_in_insertion_order(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
    ) -> None:
        """D-19: profile_names() is a snapshot the caller cannot use to mutate."""
        default_settings.profiles["flatbed"] = ProfileConfig(source="Flatbed")
        store = JobStore()
        try:
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            names = worker.profile_names()
            names.append("intruder")
            again = worker.profile_names()
        finally:
            store.close()

        assert again == ["default", "flatbed"]
        assert list(default_settings.profiles) == ["default", "flatbed"]

    def test_profile_lock_get_profile(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
    ) -> None:
        """D-19: get_profile returns the configured profile, or None."""
        store = JobStore()
        try:
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            found = worker.get_profile("default")
            missing = worker.get_profile("nope")
        finally:
            store.close()

        assert found is default_settings.profiles["default"]
        assert missing is None

    def test_profile_lock_set_profiles_rebinds_a_new_dict(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
    ) -> None:
        """
        D-19: an update replaces the mapping; the old dict is left untouched.

        A reader holding the old dict without the lock -- ``run_pipeline`` on
        the worker thread -- therefore keeps a consistent view.
        """
        old = default_settings.profiles
        old_snapshot = dict(old)
        replacement = {
            "default": ProfileConfig(),
            "flatbed": ProfileConfig(source="Flatbed"),
        }
        store = JobStore()
        try:
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            swapped = worker._set_profiles(replacement)
            names = worker.profile_names()
        finally:
            store.close()

        assert swapped is True
        assert names == ["default", "flatbed"]
        assert default_settings.profiles is not old
        assert default_settings.profiles is not replacement
        assert old == old_snapshot

    def test_profile_lock_set_profiles_honours_its_re_check(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
    ) -> None:
        """
        IN-01, D-19: ``only_if`` runs under the lock and can refuse the swap.

        Startup generation swaps through this helper with ``is_bare_default``,
        so the refusal below is the production re-check.
        """
        generated = {
            "default": ProfileConfig(source="ADF"),
            "adf": ProfileConfig(source="ADF"),
        }
        customised = {
            "default": ProfileConfig(),
            "photo": ProfileConfig(source="Flatbed", resolution=600),
        }
        store = JobStore()
        try:
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            swapped_bare = worker._set_profiles(generated, only_if=is_bare_default)
            names_after_bare = worker.profile_names()
            customised_accepted = worker._set_profiles(customised)
            swapped_customised = worker._set_profiles(
                generated, only_if=is_bare_default
            )
            names_after_customised = worker.profile_names()
        finally:
            store.close()

        assert swapped_bare is True
        assert names_after_bare == ["default", "adf"]
        assert customised_accepted is True
        assert swapped_customised is False
        assert names_after_customised == ["default", "photo"]

    def test_set_profiles_refuses_a_set_without_default(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A set without ``default`` is refused, logged, and the current set kept.

        Every scan that names no profile resolves ``default``, so a set without
        it would leave the appliance unable to scan.  The refusal is an answer
        and a WARNING, not an exception: the swap runs on the worker thread.
        """
        caplog.set_level(logging.WARNING, logger="saneless.worker")
        store = JobStore()
        try:
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            names_before = worker.profile_names()
            accepted = worker._set_profiles({"flatbed": ProfileConfig()})
            names_after = worker.profile_names()
        finally:
            store.close()

        assert accepted is False
        assert names_after == names_before
        assert "default" in names_after
        records = _worker_records(caplog, logging.WARNING, "'default'")
        assert len(records) == 1
        assert "Not replacing the scan profiles" in records[0].getMessage()

    def test_set_profiles_accepts_a_set_with_default(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A set holding ``default`` is swapped in, with no warning."""
        caplog.set_level(logging.WARNING, logger="saneless.worker")
        store = JobStore()
        try:
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            accepted = worker._set_profiles(
                {"default": ProfileConfig(), "x": ProfileConfig()}
            )
            names = worker.profile_names()
        finally:
            store.close()

        assert accepted is True
        assert names == ["default", "x"]
        assert _worker_records(caplog, logging.WARNING, "") == []

    def test_profile_lock_readers_never_see_a_dict_mid_update(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
    ) -> None:
        """
        D-19 / ROBU-05: concurrent readers and a writer never collide.

        Four readers iterate the names and look each one up while a writer
        swaps between two profile sets.  No thread may raise, and every name
        list a reader sees must be exactly one of the two sets.
        """
        first = {
            "default": ProfileConfig(),
            "flatbed": ProfileConfig(source="Flatbed"),
        }
        second = {
            "default": ProfileConfig(),
            "adf": ProfileConfig(source="ADF"),
            "duplex": ProfileConfig(source="ADF", duplex="manual"),
        }
        allowed = (list(first), list(second))
        errors: list[Exception] = []
        unexpected: list[list[str]] = []
        store = JobStore()
        worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
        barrier = threading.Barrier(5)

        def reader() -> None:
            """Read names and look each one up, many times."""
            try:
                barrier.wait(_STATE_BUDGET)
                for _ in range(_PROFILE_LOCK_ROUNDS):
                    names = worker.profile_names()
                    if names not in allowed:
                        unexpected.append(names)
                    for name in names:
                        worker.get_profile(name)
            except Exception as exc:  # recorded and asserted on below
                errors.append(exc)

        def writer() -> None:
            """Alternate the profile set between the two known shapes."""
            try:
                barrier.wait(_STATE_BUDGET)
                for round_number in range(_PROFILE_LOCK_ROUNDS):
                    assert worker._set_profiles(second if round_number % 2 else first)
            except Exception as exc:  # recorded and asserted on below
                errors.append(exc)

        threads = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
        threads.append(threading.Thread(target=writer, daemon=True))
        try:
            assert worker._set_profiles(first)
            for thread in threads:
                thread.start()
        finally:
            for thread in threads:
                if thread.ident is not None:
                    thread.join(_STATE_BUDGET * 5)
            store.close()

        assert all(not thread.is_alive() for thread in threads)
        assert errors == []
        assert unexpected == []


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

            wait_for_state(store, job1.id, TERMINAL_STATES)
            wait_for_state(store, job2.id, TERMINAL_STATES)
            worker.stop()

            assert _get(store, job1.id).state == JobState.DONE
            assert _get(store, job2.id).state == JobState.DONE
        finally:
            store.close()


class TestStartupProfileGeneration:
    """
    Auto-profile generation is the worker thread's first act (D-14, M-04).

    It used to run inside the first job, write to a path re-derived from the
    working directory rather than the file ``--config`` loaded, and mutate the
    profiles from the worker thread with no lock.  Now it runs once per start
    (D-15), persists only to ``settings.config_path`` (D-16), keeps the profiles
    in memory when there is no loaded file (D-17) or it cannot be written
    (D-18), and swaps them in under the profile lock (D-19).
    """

    @staticmethod
    def _mock_caps_scanner(mock_scanner: MagicMock) -> DeviceCapabilities:
        """
        Configure mock scanner with devices and capabilities.

        Returns:
            The capabilities the mock scanner reports.

        """
        mock_scanner.get_devices.return_value = [
            DeviceInfo(
                name="test:device:001",
                vendor="Test",
                model="Scanner",
                device_type="scanner",
            ),
        ]
        caps = DeviceCapabilities(
            sources=["Flatbed", "ADF"],
            resolutions=[150, 300, 600],
            modes=["Color", "Gray"],
        )
        mock_scanner.get_capabilities.return_value = caps
        return caps

    def test_startup_generation_writes_the_loaded_config_file(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """D-14 / D-16: generated profiles land in memory and in the loaded file."""
        caps = self._mock_caps_scanner(mock_scanner)
        expected = generate_profiles(caps)
        config_file = tmp_path / "saneless.toml"
        config_file.write_text("# loaded by --config\n")
        default_settings._config_path = config_file

        store = JobStore()
        worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
        try:
            worker.start()
            generated = poll_until(
                lambda: "flatbed" in worker.profile_names(), _STATE_BUDGET
            )
        finally:
            worker.stop()
            store.close()

        assert generated
        assert set(worker.profile_names()) == set(expected)
        assert "default" in worker.profile_names()
        on_disk = tomllib.loads(config_file.read_text())
        assert set(on_disk["profiles"]) == set(expected)

    def test_startup_generation_keeps_a_default_the_file_already_defines(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        WR-03: memory keeps the ``default`` a restart will load from the file.

        The file spells out a bare ``[profiles.default]``, so the write skips
        ``default``.  Memory must keep that loaded ``default`` rather than the
        generated one, or ``default`` would change on the next restart.
        """
        caps = self._mock_caps_scanner(mock_scanner)
        expected = generate_profiles(caps)
        assert expected["default"] != ProfileConfig()
        config_file = tmp_path / "saneless.toml"
        spelled_out = (
            '[profiles.default]\nsource = "Flatbed"\nresolution = 300\nmode = "color"\n'
        )
        config_file.write_text(spelled_out)
        default_settings._config_path = config_file

        store = JobStore()
        worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
        try:
            worker.start()
            generated = poll_until(
                lambda: "flatbed" in worker.profile_names(), _STATE_BUDGET
            )
            in_memory_default = worker.get_profile("default")
        finally:
            worker.stop()
            store.close()

        assert generated
        assert set(worker.profile_names()) == set(expected)
        assert in_memory_default == ProfileConfig()
        on_disk = tomllib.loads(config_file.read_text())
        assert set(on_disk["profiles"]) == set(expected)
        assert (
            on_disk["profiles"]["default"]
            == tomllib.loads(spelled_out)["profiles"]["default"]
        )
        for name in set(expected) - {"default"}:
            assert worker.get_profile(name) == expected[name]

    def test_startup_generation_on_a_no_source_scanner_keeps_default(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        A scanner with no source option still leaves a working ``default``.

        The generated set for such a device is ``default`` alone.  Start-up
        swaps it in and writes it, so ``default`` is in memory and in the file
        a restart loads -- the appliance can still scan with no profile named.
        """
        mock_scanner.get_devices.return_value = [
            DeviceInfo(
                name="test:device:001",
                vendor="Test",
                model="Scanner",
                device_type="scanner",
            ),
        ]
        mock_scanner.get_capabilities.return_value = DeviceCapabilities(
            sources=[], resolutions=[300], modes=["Color"]
        )
        config_file = tmp_path / "saneless.toml"
        config_file.write_text("# loaded by --config\n")
        default_settings._config_path = config_file

        def swapped_in() -> bool:
            """Whether the generated ``default`` has replaced the bare one."""
            profile = worker.get_profile("default")
            return profile is not None and profile.auto_generated

        store = JobStore()
        worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
        try:
            worker.start()
            generated = poll_until(swapped_in, _STATE_BUDGET)
        finally:
            worker.stop()
            store.close()

        assert generated
        assert "default" in worker.profile_names()
        on_disk = tomllib.loads(config_file.read_text())
        assert "default" in on_disk["profiles"]
        assert "source" not in on_disk["profiles"]["default"]

    def test_startup_generation_memory_keeps_every_unpersisted_name(
        self, tmp_path: Path
    ) -> None:
        """
        WR-03 / D-01: a name the write did not persist keeps its loaded profile.

        The worker never forces, so a flagged file profile is skipped as
        existing and an unflagged one as not generated; in both cases memory
        must match what a restart loads, not the generated profile.
        """
        loaded = {"default": ProfileConfig(), "flatbed": ProfileConfig(resolution=150)}
        generated = {
            "default": ProfileConfig(source="ADF", auto_generated=True),
            "flatbed": ProfileConfig(source="Flatbed", auto_generated=True),
            "adf": ProfileConfig(source="ADF", auto_generated=True),
        }
        result = ProfileWriteResult(
            path=tmp_path / "saneless.toml",
            added=("adf",),
            skipped_not_generated=("default",),
            skipped_existing=("flatbed",),
        )

        profiles = worker_module._profiles_after_persist(loaded, generated, result)

        assert profiles["default"] == loaded["default"]
        assert profiles["flatbed"] == loaded["flatbed"]
        assert profiles["adf"] == generated["adf"]

    def test_startup_generation_logs_the_grouped_result(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """D-04: the startup INFO line uses the CLI's group vocabulary."""
        caplog.set_level(logging.INFO, logger="saneless.worker")
        self._mock_caps_scanner(mock_scanner)
        config_file = tmp_path / "saneless.toml"
        config_file.write_text('[profiles.default]\nsource = "Flatbed"\n')
        default_settings._config_path = config_file

        store = JobStore()
        worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
        try:
            worker.start()
            generated = poll_until(
                lambda: bool(_worker_records(caplog, logging.INFO, "Added: ")),
                _STATE_BUDGET,
            )
        finally:
            worker.stop()
            store.close()

        assert generated
        records = _worker_records(caplog, logging.INFO, "Added: ")
        assert len(records) == 1
        message = records[0].getMessage()
        assert str(config_file.resolve()) in message
        assert "'flatbed'" in message
        assert "Skipped (not auto-generated): 'default'" in message

    def test_startup_generation_without_a_loaded_file_writes_nothing(
        self,
        mock_scanner: MagicMock,
        worker_for: Callable[[JobStore], ScanWorker],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        D-17 / T-26-32: no loaded file means memory only, and no file anywhere.

        The working directory and HOME both point into ``tmp_path``, so a write
        to any re-derived location -- ``./saneless.toml`` or the XDG path --
        would show up as a new file there.
        """
        caplog.set_level(logging.INFO, logger="saneless.worker")
        self._mock_caps_scanner(mock_scanner)
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(home))
        before = sorted(tmp_path.rglob("*"))

        store = JobStore()
        worker = worker_for(store)
        assert worker._settings.config_path is None
        try:
            worker.start()
            generated = poll_until(
                lambda: "flatbed" in worker.profile_names(), _STATE_BUDGET
            )
        finally:
            worker.stop()
            store.close()

        assert generated
        assert sorted(tmp_path.rglob("*")) == before
        records = _worker_records(caplog, logging.INFO, "no config file was loaded")
        assert len(records) == 1
        message = records[0].getMessage()
        assert "--config" in message
        for path in config_search_paths():
            assert str(path) in message

    def test_startup_generation_keeps_profiles_when_the_file_is_unwritable(
        self,
        mock_scanner: MagicMock,
        worker_for: Callable[[JobStore], ScanWorker],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """D-18: an OSError writing the loaded file keeps the profiles in memory."""
        self._mock_caps_scanner(mock_scanner)
        config_file = tmp_path / "saneless.toml"
        config_file.write_text("# read-only\n")
        real_replace = auto_profiles_module.replace_file_atomically

        def refusing(path: Path, text: str) -> Path:
            """Refuse the loaded file the way the kernel would; write any other."""
            if path == config_file:
                raise PermissionError(
                    errno.EACCES, os.strerror(errno.EACCES), str(path)
                )
            return real_replace(path, text)

        monkeypatch.setattr(auto_profiles_module, "replace_file_atomically", refusing)

        store = JobStore()
        worker = worker_for(store)
        worker._settings._config_path = config_file
        try:
            worker.start()
            generated = poll_until(
                lambda: "flatbed" in worker.profile_names(), _STATE_BUDGET
            )
        finally:
            worker.stop()
            store.close()

        assert generated
        assert config_file.read_text() == "# read-only\n"
        records = _worker_records(caplog, logging.WARNING, "will not survive a restart")
        assert len(records) == 1
        message = records[0].getMessage()
        assert str(config_file) in message
        assert "PermissionError" in message

    def test_startup_generation_keeps_profiles_when_profiles_is_not_a_table(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """D-18: a ConfigError from the write is handled like an OSError."""
        self._mock_caps_scanner(mock_scanner)
        config_file = tmp_path / "saneless.toml"
        config_file.write_text("profiles = 1\n")
        default_settings._config_path = config_file

        store = JobStore()
        worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
        try:
            worker.start()
            generated = poll_until(
                lambda: "flatbed" in worker.profile_names(), _STATE_BUDGET
            )
        finally:
            worker.stop()
            store.close()

        assert generated
        assert config_file.read_text() == "profiles = 1\n"
        records = _worker_records(caplog, logging.WARNING, "will not survive a restart")
        assert len(records) == 1
        message = records[0].getMessage()
        assert str(config_file) in message
        assert "ConfigError" in message

    def test_startup_generation_ebusy_keeps_profiles_in_memory(
        self,
        mock_scanner: MagicMock,
        worker_for: Callable[[JobStore], ScanWorker],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        D-08: a single-file bind mount is a logged ConfigError, not a lost set.

        The kernel refuses to rename over a bind-mount point with EBUSY; the
        atomic write reports it as a ConfigError, and the worker's existing
        branch keeps the generated profiles for this run (Phase 26 D-18).
        """
        caps = self._mock_caps_scanner(mock_scanner)
        expected = generate_profiles(caps)
        config_file = tmp_path / "saneless.toml"
        config_file.write_text("# mounted as a single file\n")

        def busy(_self: Path, _target: object) -> Path:
            raise OSError(errno.EBUSY, os.strerror(errno.EBUSY))

        monkeypatch.setattr(Path, "replace", busy)

        store = JobStore()
        worker = worker_for(store)
        worker._settings._config_path = config_file
        try:
            worker.start()
            generated = poll_until(
                lambda: "flatbed" in worker.profile_names(), _STATE_BUDGET
            )
            in_memory = worker.get_profile("flatbed")
        finally:
            worker.stop()
            store.close()

        assert generated
        assert in_memory == expected["flatbed"]
        assert config_file.read_text() == "# mounted as a single file\n"
        records = _worker_records(caplog, logging.WARNING, "will not survive a restart")
        assert len(records) == 1
        assert "ConfigError" in records[0].getMessage()
        assert [path.name for path in tmp_path.iterdir()] == ["saneless.toml"]

    def test_startup_generation_non_utf8_config_is_a_config_error_utf8(
        self,
        mock_scanner: MagicMock,
        worker_for: Callable[[JobStore], ScanWorker],
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """D-05: a non-UTF-8 file is refused as a ConfigError and left alone."""
        self._mock_caps_scanner(mock_scanner)
        config_file = tmp_path / "saneless.toml"
        contents = b"\xff\xfe not utf-8\n"
        config_file.write_bytes(contents)

        store = JobStore()
        worker = worker_for(store)
        worker._settings._config_path = config_file
        try:
            worker.start()
            generated = poll_until(
                lambda: "flatbed" in worker.profile_names(), _STATE_BUDGET
            )
        finally:
            worker.stop()
            store.close()

        assert generated
        assert config_file.read_bytes() == contents
        records = _worker_records(caplog, logging.WARNING, "will not survive a restart")
        assert len(records) == 1
        message = records[0].getMessage()
        assert "ConfigError" in message
        assert "UTF-8" in message

    def test_startup_generation_invalid_toml_config_is_a_config_error(
        self,
        mock_scanner: MagicMock,
        worker_for: Callable[[JobStore], ScanWorker],
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """D-12: an unparseable file is refused as a ConfigError and left alone."""
        self._mock_caps_scanner(mock_scanner)
        config_file = tmp_path / "saneless.toml"
        contents = b"[profiles\n"
        config_file.write_bytes(contents)

        store = JobStore()
        worker = worker_for(store)
        worker._settings._config_path = config_file
        try:
            worker.start()
            generated = poll_until(
                lambda: "flatbed" in worker.profile_names(), _STATE_BUDGET
            )
        finally:
            worker.stop()
            store.close()

        assert generated
        assert config_file.read_bytes() == contents
        records = _worker_records(caplog, logging.WARNING, "will not survive a restart")
        assert len(records) == 1
        message = records[0].getMessage()
        assert "ConfigError" in message
        assert "line 1, column" in message

    def test_startup_generation_keeps_profiles_when_the_write_raises_anything_else(
        self,
        mock_scanner: MagicMock,
        worker_for: Callable[[JobStore], ScanWorker],
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        WR-04, D-18: an exception outside OSError and ConfigError keeps them too.

        Neither a non-UTF-8 file (D-05) nor an unparseable one (D-12) reaches
        this handler any more, so the write is patched to raise an unexpected
        class. The generated profiles must still be used in memory, with a
        WARNING that names the exception class.
        """
        exception_name = "TypeError"

        def _raise_unexpected(*_args: object, **_kwargs: object) -> None:
            msg = "container error"
            raise TypeError(msg)

        monkeypatch.setattr(
            "saneless.worker.write_profiles_to_config", _raise_unexpected
        )
        self._mock_caps_scanner(mock_scanner)
        config_file = tmp_path / "saneless.toml"
        contents = b"# mounted config\n"
        config_file.write_bytes(contents)

        store = JobStore()
        worker = worker_for(store)
        worker._settings._config_path = config_file
        try:
            worker.start()
            generated = poll_until(
                lambda: "flatbed" in worker.profile_names(), _STATE_BUDGET
            )
            alive = worker.is_alive
        finally:
            worker.stop()
            store.close()

        assert generated
        assert alive
        assert config_file.read_bytes() == contents
        records = _worker_records(caplog, logging.WARNING, "will not survive a restart")
        assert len(records) == 1
        assert exception_name in records[0].getMessage()
        assert not _worker_records(
            caplog, logging.ERROR, "Auto-profiles: startup generation failed"
        )

    def test_startup_generation_scanner_failure_keeps_the_bare_default(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        D-15 / T-26-34: the WARNING names the real exception, not a guessed cause.

        The old message called every failure an unreachable scanner, which sent
        operators hunting network faults for what was often a parsing error.
        """
        mock_scanner.get_devices.side_effect = ScanError("boom")

        store = JobStore()
        worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
        try:
            worker.start()
            warned = poll_until(
                lambda: bool(_worker_records(caplog, logging.WARNING, "ScanError")),
                _STATE_BUDGET,
            )
            names = worker.profile_names()
            alive = worker.is_alive
        finally:
            worker.stop()
            store.close()

        assert warned
        assert alive
        assert names == ["default"]
        # Broader than the retired wording: no guess about reachability at all.
        assert "unreachable" not in caplog.text

    def test_startup_generation_no_scanners_keeps_the_bare_default(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """D-15: no devices found keeps the bare default with a WARNING."""
        mock_scanner.get_devices.return_value = []

        store = JobStore()
        worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
        try:
            worker.start()
            warned = poll_until(
                lambda: bool(
                    _worker_records(caplog, logging.WARNING, "no scanners found")
                ),
                _STATE_BUDGET,
            )
            names = worker.profile_names()
        finally:
            worker.stop()
            store.close()

        assert warned
        assert names == ["default"]
        mock_scanner.get_capabilities.assert_not_called()

    def test_startup_generation_skips_customized_profiles(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A profile set that is not the bare default never asks the scanner."""
        self._mock_caps_scanner(mock_scanner)
        default_settings.profiles["photo"] = ProfileConfig(
            source="Flatbed", resolution=600, mode="Color"
        )
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            lambda *_args, **_kwargs: _success_result(),
        )

        store = JobStore()
        worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
        try:
            worker.start()
            job = store.create_job("default", "Custom Test")
            worker.submit(job)
            wait_for_state(store, job.id, TERMINAL_STATES)
        finally:
            worker.stop()
            store.close()

        mock_scanner.get_devices.assert_not_called()
        mock_scanner.get_capabilities.assert_not_called()
        assert list(default_settings.profiles) == ["default", "photo"]

    def test_startup_generation_is_tried_once_per_start(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """D-15: three jobs later the scanner has still been asked only once."""
        self._mock_caps_scanner(mock_scanner)
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            lambda *_args, **_kwargs: _success_result(),
        )

        store = JobStore()
        worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
        try:
            worker.start()
            jobs = [store.create_job("default", f"Job {n}") for n in range(3)]
            for job in jobs:
                worker.submit(job)
            for job in jobs:
                wait_for_state(store, job.id, TERMINAL_STATES)
        finally:
            worker.stop()
            store.close()

        assert mock_scanner.get_devices.call_count == 1
        assert mock_scanner.get_capabilities.call_count == 1

    def test_startup_generation_precedes_the_first_job(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        D-14: a job submitted during generation waits, then sees the new profiles.

        ``get_capabilities`` is held on an event.  While it is held the job
        stays PENDING and the pipeline has not run; once released, the job's
        profile resolves against the generated set.
        """
        caps = self._mock_caps_scanner(mock_scanner)
        expected = generate_profiles(caps)
        entered = threading.Event()
        release = threading.Event()

        def held_capabilities(_device_id: str) -> DeviceCapabilities:
            entered.set()
            release.wait(_STATE_BUDGET * 5)
            return caps

        mock_scanner.get_capabilities.side_effect = held_capabilities
        store = JobStore()
        worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
        seen: list[tuple[list[str], ProfileConfig | None]] = []

        def recording_pipeline(
            _scanner: object,
            _paperless: object,
            settings: Settings,
            request: PipelineRequest,
        ) -> ScanResult:
            seen.append(
                (list(settings.profiles), worker.get_profile(request.profile_name))
            )
            return _success_result()

        monkeypatch.setattr("saneless.worker.run_pipeline", recording_pipeline)
        try:
            worker.start()
            assert entered.wait(_STATE_BUDGET)
            job = store.create_job("default", "First Job")
            submitted = worker.submit(job)
            # A fixed window, not a poll: this is a negative observation.  The
            # worker is blocked on ``release``, so nothing should happen; the
            # window only gives a worker that skipped ahead to the queue time to
            # start the job and be caught.
            quiet_window(0.2)
            held_state = _get(store, job.id).state
            runs_while_held = len(seen)
            release.set()
            finished = wait_for_state(store, job.id, TERMINAL_STATES)
        finally:
            release.set()
            worker.stop()
            store.close()

        assert submitted is SubmitResult.ACCEPTED
        assert held_state is JobState.PENDING
        assert runs_while_held == 0
        assert finished.state is JobState.DONE
        assert len(seen) == 1
        names, profile = seen[0]
        assert set(names) == set(expected)
        assert profile == expected["default"]


class TestNoConfigFileMessageAgreesWithTheRestOfTheProduct:
    """
    The worker's "no config file was loaded" line is a fifth description of one fact.

    The startup log, the Configuration row, ``doctor``'s table and the
    one-shot warning all read ``config_file_state``.  This message was written
    before any of them and says "create one of <three paths>" whatever the
    search found -- so on the machine that started this phase it would have
    told an operator to create a ``saneless.toml`` while a ``config.toml``
    sat in the very directory it was naming, and never mentioned it.

    It is exercised through ``_persist_generated_profiles`` directly rather
    than through a started worker, because the branch is reached before
    anything is written and starting a worker would only add timing.
    """

    @staticmethod
    def _persist(
        settings: Settings, scanner: MagicMock, paperless: MagicMock
    ) -> ProfileWriteResult | None:
        """
        Run the persist step over settings that loaded no file.

        Args:
            settings: The settings the worker runs on.
            scanner: The scanner the worker is built over.
            paperless: The Paperless client the worker is built over.

        Returns:
            Whatever the persist step reported, which should be None.

        """
        store = JobStore()
        worker = ScanWorker(scanner, paperless, settings, store)
        try:
            return worker._persist_generated_profiles({"default": ProfileConfig()})
        finally:
            store.close()

    def test_a_stale_only_search_names_the_ignored_file_and_the_new_name(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The reason nothing loaded is the file the message used to leave out."""
        caplog.set_level(logging.INFO, logger="saneless.worker")
        directory = tmp_path / "etc"
        directory.mkdir()
        stale = directory / LEGACY_CONFIG_FILENAME
        stale.write_text("# left behind\n")
        default_settings._config_discovery = discover_config(
            (directory / CONFIG_FILENAME,)
        )

        assert self._persist(default_settings, mock_scanner, mock_paperless) is None

        records = _worker_records(caplog, logging.INFO, "no config file was loaded")
        assert len(records) == 1
        message = records[0].getMessage()
        assert str(stale.absolute()) in message
        assert CONFIG_FILENAME in message

    def test_a_search_that_found_nothing_lists_what_it_looked_at(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """With nothing stale there is nothing to rename, so the advice is unchanged."""
        caplog.set_level(logging.INFO, logger="saneless.worker")
        candidates = tuple(
            tmp_path / name / CONFIG_FILENAME for name in ("cwd", "xdg", "etc")
        )
        for candidate in candidates:
            candidate.parent.mkdir()
        default_settings._config_discovery = discover_config(candidates)

        assert self._persist(default_settings, mock_scanner, mock_paperless) is None

        records = _worker_records(caplog, logging.INFO, "no config file was loaded")
        assert len(records) == 1
        message = records[0].getMessage()
        for candidate in candidates:
            assert str(candidate.absolute()) in message
        assert LEGACY_CONFIG_FILENAME not in message

    def test_a_search_from_a_deleted_working_directory_lists_it_as_spelled(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A candidate with no directory to anchor it is named, not made absolute."""
        caplog.set_level(logging.INFO, logger="saneless.worker")
        later = tmp_path / "etc" / CONFIG_FILENAME
        later.parent.mkdir()
        gone = tmp_path / "gone"
        gone.mkdir()
        with pytest.MonkeyPatch.context() as patch:
            patch.chdir(gone)
            gone.rmdir()
            default_settings._config_discovery = discover_config(
                (Path(CONFIG_FILENAME), later)
            )

            persisted = self._persist(default_settings, mock_scanner, mock_paperless)

        assert persisted is None

        records = _worker_records(caplog, logging.INFO, "no config file was loaded")
        assert len(records) == 1
        assert f"create one of {CONFIG_FILENAME}, {later}" in records[0].getMessage()

    def test_neither_message_writes_a_file(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Naming the stale file is reporting, not repairing.

        The working directory is ``tmp_path``, so a write to any re-derived
        location would show up here -- and the stale file's own bytes are
        compared, because a helpful rename is exactly the repair this message
        must not attempt.
        """
        monkeypatch.chdir(tmp_path)
        directory = tmp_path / "etc"
        directory.mkdir()
        stale = directory / LEGACY_CONFIG_FILENAME
        stale.write_text("# left behind\n")
        default_settings._config_discovery = discover_config(
            (directory / CONFIG_FILENAME,)
        )

        assert self._persist(default_settings, mock_scanner, mock_paperless) is None

        assert stale.read_text() == "# left behind\n"
        assert not (directory / CONFIG_FILENAME).exists()
        assert not (tmp_path / CONFIG_FILENAME).exists()


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
                # worker's own post-pipeline DONE write cannot be mistaken
                # for callback output.
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
            wait_for_state(store, job.id, TERMINAL_STATES)
            worker.stop()

            assert JobState.ASSEMBLING in states_seen

            # A job must never be marked DONE from inside run_pipeline: that
            # happens before the pipeline's temporary directory is cleaned up
            # and is visible to the one-second web poll.
            assert JobState.DONE not in from_callback

            # The states applied are exactly the in-flight ones, in the order
            # the events were emitted.  SCANNING_REVERSE is persisted as its own
            # busy state, so pass B takes the job out of AWAITING_FLIP (DPLX-06).
            # DONE (terminal) contributes nothing.  SCANNING comes first, and
            # from here: the worker writes nothing before the pipeline, whose
            # first event is announced only once the scanner gate is held.
            # The three multi-page waits are active states like the flip
            # wait, so each is persisted as well.
            assert from_callback == [
                JobState.SCANNING,
                JobState.AWAITING_FLIP,
                JobState.SCANNING_REVERSE,
                JobState.AWAITING_NEXT_PASS,
                JobState.AWAITING_BLANK_DECISION,
                JobState.AWAITING_RETRY,
                JobState.ASSEMBLING,
                JobState.UPLOADING,
            ]
            assert states_seen.count(JobState.SCANNING) == 1

            # DONE is still written by the worker, after run_pipeline returned
            # -- but through finish_job now, not update_state.  update_state's
            # SQL is an unconditional SET that never names the six result
            # columns (D-04), so the terminal write cannot go through it
            # without blanking the outcome and page counts it just recorded.
            assert JobState.DONE not in states_seen
            assert finished_states == [JobState.DONE]
        finally:
            store.close()

    def test_worker_makes_no_prune_call_in_the_job_path(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        D-13: a completed job is not followed by a prune.

        Prune runs on the idle tick instead, so a prune failure can never fail
        a job (ROBU-01).  The prune interval stays at its hourly default, and
        stop() joins the thread, so the job's ``finally`` has run before the
        spy is read.
        """
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
            finished = wait_for_state(store, job.id, TERMINAL_STATES)
            worker.stop()

            assert finished.state is JobState.DONE
            assert prune_calls == []
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

    def test_finish_records_the_removed_positions_on_a_plain_done(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Pages 2 and 4 removed as blank: named on the job, and the job stays DONE.

        The positions are information beside the counts, never a warning, so a
        run that dropped blank backs does not become a warned success.
        """
        store = JobStore()
        try:
            monkeypatch.setattr(
                "saneless.worker.run_pipeline",
                lambda *_args, **_kwargs: ScanResult(
                    outcome=ScanOutcome.SUCCESS,
                    pages_scanned=4,
                    pages_removed=2,
                    pages_uploaded=2,
                    removed_positions=(2, 4),
                ),
            )
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()

            job = store.create_job("default", "Blank Backs")
            worker.submit(job)
            finished = wait_for_state(store, job.id, TERMINAL_STATES)
            worker.stop()

            assert finished.state is JobState.DONE
            assert finished.removed_positions == (2, 4)
            assert finished.pages_removed == 2
            assert finished.warning is None
        finally:
            store.close()

    def test_finish_records_that_nothing_was_removed(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A run that removed nothing records an empty list, not "never recorded"."""
        store = JobStore()
        try:
            monkeypatch.setattr(
                "saneless.worker.run_pipeline",
                lambda *_args, **_kwargs: _success_result(),
            )
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()

            job = store.create_job("default", "Nothing Removed")
            worker.submit(job)
            finished = wait_for_state(store, job.id, TERMINAL_STATES)
            worker.stop()

            assert finished.removed_positions == ()
        finally:
            store.close()

    def test_finish_persists_a_duplex_mismatch_warning(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
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

    def test_finish_clears_the_job_without_a_prune_after_a_fallback_outcome(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        The ``finally`` still clears the current job on FALLBACK, and prunes nothing.

        STOR-05's history bound is kept by the idle-tick prune now (D-13), so
        the terminal path of any outcome makes no prune call.
        """
        store = JobStore()
        prune_spy = _StoreFault(store.prune, frozenset())
        try:
            monkeypatch.setattr(store, "prune", prune_spy)
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
            assert prune_spy.calls == []
        finally:
            store.close()


def _raising_pipeline(error: BaseException) -> Callable[..., ScanResult]:
    """
    Build a ``run_pipeline`` stand-in that raises ``error`` itself.

    The very instance is raised, so a test can check a log record's
    ``exc_info`` carries it rather than an equal copy.

    Returns:
        A callable taking ``run_pipeline``'s arguments and raising ``error``.

    """

    def failing(*_args: object, **_kwargs: object) -> ScanResult:
        raise error

    return failing


def _interrupted_after(
    events: tuple[PipelineEvent, ...], interruption: ScanInterrupted
) -> Callable[..., ScanResult]:
    """
    Build a ``run_pipeline`` stand-in that announces ``events``, then is stopped.

    Returns:
        A callable taking ``run_pipeline``'s arguments, calling the request's
        status callback with each event in turn and then raising
        ``interruption``.

    """

    def interrupted(
        _scanner: object,
        _paperless: object,
        _settings: object,
        request: PipelineRequest,
    ) -> ScanResult:
        if request.status_callback:
            for event in events:
                request.status_callback(event)
        raise interruption

    return interrupted


def _finish_one_job(
    worker: ScanWorker,
    store: JobStore,
    profile: str = "default",
) -> Job:
    """
    Run one job on an unstarted worker to its end, then stop and close.

    ``stop()`` joins the thread, so every record the job's ending logs has been
    emitted by the time this returns.

    Returns:
        The job row as it ended.

    """
    try:
        worker.start()
        job = store.create_job(profile, "Job Ending")
        assert worker.submit(job) is SubmitResult.ACCEPTED
        finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
    finally:
        worker.stop()
        store.close()
    return finished


class TestWorkerJobEndings:
    """
    A job ends by shutdown, by cancel, or by failure: three explicit branches.

    D-01: a ``ScanCancelledError`` is recorded ``CANCELLED`` with its message
    and no category.  N-08: a cancel is not a failure, so it is logged once at
    INFO with no traceback.  EXC-05: every other failure is recorded ERROR with
    its category and logged at ERROR with ``exc_info``, so the operator can
    find the cause.  A server stop reaches the worker as
    ``ScanInterrupted``, its own ending, and is recorded as a restart followed
    by whatever the pipeline kept.  Phase 26 D-10: job endings never count
    toward degraded health.
    """

    def test_a_cancel_is_recorded_cancelled_without_a_category(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """D-01: a ScanCancelledError ends the job CANCELLED, not ERROR."""
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            _raising_pipeline(ScanCancelledError(_CANCEL_MESSAGE)),
        )
        store = JobStore()
        finished = _finish_one_job(worker_for(store), store)

        assert finished.state is JobState.CANCELLED
        assert finished.error == _CANCEL_MESSAGE
        assert finished.error_category is None

    def test_a_cancel_is_logged_once_at_info_without_exc_info(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """N-08: a cancel logs one INFO line, no traceback and no ERROR."""
        caplog.set_level(logging.INFO, logger="saneless.worker")
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            _raising_pipeline(ScanCancelledError(_CANCEL_MESSAGE)),
        )
        store = JobStore()
        finished = _finish_one_job(worker_for(store), store)

        cancelled = _worker_records(
            caplog, logging.INFO, f"Job {finished.id} cancelled"
        )
        assert len(cancelled) == 1
        assert cancelled[0].exc_info is None
        assert _worker_records(caplog, logging.ERROR, finished.id) == []

    def test_a_scan_error_is_logged_with_its_exc_info(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """EXC-05: a scanner failure is ERROR, SCANNER, logged with its traceback."""
        caplog.set_level(logging.INFO, logger="saneless.worker")
        message = "Paper jam"
        error = ScanError(message)
        monkeypatch.setattr("saneless.worker.run_pipeline", _raising_pipeline(error))
        store = JobStore()
        finished = _finish_one_job(worker_for(store), store)

        assert finished.state is JobState.ERROR
        assert finished.error == message
        assert finished.error_category is ErrorCategory.SCANNER
        records = _worker_records(caplog, logging.ERROR, finished.id)
        assert len(records) == 1
        assert records[0].exc_info is not None
        assert records[0].exc_info[1] is error

    @pytest.mark.parametrize(
        "failure",
        [
            (PaperlessError, ErrorCategory.UPLOAD),
            (ConfigError, ErrorCategory.CONFIG),
            (PdfError, ErrorCategory.ASSEMBLY),
            (RuntimeError, ErrorCategory.UNKNOWN),
        ],
        ids=lambda failure: failure[0].__name__,
    )
    def test_every_failure_is_logged_with_its_exc_info(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        failure: tuple[type[Exception], ErrorCategory],
    ) -> None:
        """EXC-05: classified or unknown, a failure carries its traceback."""
        caplog.set_level(logging.INFO, logger="saneless.worker")
        error_type, category = failure
        message = f"{error_type.__name__} ended the job"
        error = error_type(message)
        monkeypatch.setattr("saneless.worker.run_pipeline", _raising_pipeline(error))
        store = JobStore()
        finished = _finish_one_job(worker_for(store), store)

        assert finished.state is JobState.ERROR
        assert finished.error == message
        assert finished.error_category is category
        records = _worker_records(caplog, logging.ERROR, finished.id)
        assert len(records) == 1
        assert records[0].exc_info is not None
        assert records[0].exc_info[1] is error

    def test_an_interruption_is_recorded_as_a_restart_naming_what_was_kept(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A server stop ends the job as a restart, followed by the kept file.

        The pipeline raises ``ScanInterrupted`` for a stop, carrying the
        preservation's sentence as a note.  It is not an ``Exception``, so the
        worker catches it by name: ERROR with the restart reason and the note,
        no category, one INFO line and no traceback, because nothing failed.

        This replaces a test that pinned the old path, where the stop answered
        the flip with Abort and the worker had to tell that Abort from the
        operator's by a marker on the coordinator.
        """
        caplog.set_level(logging.INFO, logger="saneless.worker")
        note = "The 1 page(s) scanned before the error were preserved at /x.pdf"
        interruption = ScanInterrupted("The server is stopping")
        interruption.add_note(note)
        monkeypatch.setattr(
            "saneless.worker.run_pipeline", _raising_pipeline(interruption)
        )
        store = JobStore()
        finished = _finish_one_job(worker_for(store), store)

        assert finished.state is JobState.ERROR
        assert finished.error == f"{RESTART_REASON}. {note}"
        assert finished.error_category is None
        shutdown = f"Job {finished.id} ended by shutdown"
        assert len(_worker_records(caplog, logging.INFO, shutdown)) == 1
        assert _worker_records(caplog, logging.ERROR, finished.id) == []

    def test_an_interruption_that_kept_nothing_records_the_bare_restart(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With no page to keep there is no note, and the row says only why."""
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            _raising_pipeline(ScanInterrupted("The server is stopping")),
        )
        store = JobStore()
        finished = _finish_one_job(worker_for(store), store)

        assert finished.state is JobState.ERROR
        assert finished.error == RESTART_REASON
        assert finished.error_category is None

    def test_a_restart_while_uploading_is_recorded_as_maybe_delivered(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A stop during the upload may have left the document in paperless-ngx.

        The row is the amber after-send category, and its text says the
        upload may have arrived, followed by the kept file, rather than
        "before this scan finished".
        """
        note = "The 2 page(s) scanned were preserved at /x.pdf"
        interruption = ScanInterrupted("The server is stopping")
        interruption.add_note(note)
        events = (PipelineEvent.ASSEMBLING, PipelineEvent.UPLOADING)
        monkeypatch.setattr(
            "saneless.worker.run_pipeline", _interrupted_after(events, interruption)
        )
        store = JobStore()
        finished = _finish_one_job(worker_for(store), store)

        assert finished.state is JobState.ERROR
        assert finished.error == f"{RESTART_UPLOADING_REASON}. {note}"
        assert finished.error_category is ErrorCategory.UNCONFIRMED_SEND

    def test_a_restart_while_uploading_that_kept_nothing_is_still_amber(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With no note, the row carries the uploading reason alone."""
        interruption = ScanInterrupted("The server is stopping")
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            _interrupted_after((PipelineEvent.UPLOADING,), interruption),
        )
        store = JobStore()
        finished = _finish_one_job(worker_for(store), store)

        assert finished.state is JobState.ERROR
        assert finished.error == RESTART_UPLOADING_REASON
        assert finished.error_category is ErrorCategory.UNCONFIRMED_SEND

    def test_a_restart_while_uploading_is_amber_when_the_state_write_failed(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        What the run announced decides the wording, not what reached the row.

        The UPLOADING progress write fails and is logged, so the row still
        reads ASSEMBLING when the stop arrives; the upload had begun all the
        same, so the ending is still the amber one.
        """
        store = JobStore()
        original_update = store.update_state

        def refusing_uploading(
            job_id: str,
            state: JobState,
            error: str | None = None,
            error_category: ErrorCategory | None = None,
        ) -> None:
            if state is JobState.UPLOADING:
                msg = "database is locked"
                raise sqlite3.OperationalError(msg)
            original_update(job_id, state, error=error, error_category=error_category)

        monkeypatch.setattr(store, "update_state", refusing_uploading)
        events = (PipelineEvent.ASSEMBLING, PipelineEvent.UPLOADING)
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            _interrupted_after(events, ScanInterrupted("The server is stopping")),
        )
        finished = _finish_one_job(worker_for(store), store)

        assert finished.state is JobState.ERROR
        assert finished.error == RESTART_UPLOADING_REASON
        assert finished.error_category is ErrorCategory.UNCONFIRMED_SEND

    def test_a_restart_before_the_upload_keeps_the_restart_reason(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A stop while assembling never reached paperless-ngx, so no category."""
        note = "The 2 page(s) scanned were preserved at /x.pdf"
        interruption = ScanInterrupted("The server is stopping")
        interruption.add_note(note)
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            _interrupted_after((PipelineEvent.ASSEMBLING,), interruption),
        )
        store = JobStore()
        finished = _finish_one_job(worker_for(store), store)

        assert finished.state is JobState.ERROR
        assert finished.error == f"{RESTART_REASON}. {note}"
        assert finished.error_category is None

    def test_the_worker_thread_survives_an_interruption(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        ``ScanInterrupted`` is a ``BaseException``, and it must not kill the thread.

        Escaping ``_scan_job`` would end the worker thread with the row left
        active.  The first job is interrupted, the second succeeds on the same
        thread, and the worker stays healthy: an interruption is a job
        ending, not a loop failure.
        """
        calls: list[str] = []
        interruption = ScanInterrupted("The server is stopping")

        def interrupted_once(*_args: object, **_kwargs: object) -> ScanResult:
            calls.append("run")
            if len(calls) == 1:
                raise interruption
            return _success_result()

        monkeypatch.setattr("saneless.worker.run_pipeline", interrupted_once)
        store = JobStore()
        worker = worker_for(store)
        try:
            worker.start()
            first, second = _submit_jobs(worker, store, 2)
            first_row = wait_for_state(store, first.id, TERMINAL_STATES, _STATE_BUDGET)
            second_row = wait_for_state(
                store, second.id, TERMINAL_STATES, _STATE_BUDGET
            )
            alive = worker.is_alive
            health = worker.health
        finally:
            worker.stop()
            store.close()

        assert first_row.state is JobState.ERROR
        assert first_row.error == RESTART_REASON
        assert second_row.state is JobState.DONE
        assert alive
        assert health is WorkerHealth.HEALTHY

    def test_a_cancel_after_shutdown_claimed_the_flip_is_still_a_cancel(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        The outcome carries the meaning; nothing on the coordinator relabels it.

        A ``ScanCancelledError`` is the operator's cancel however the flip was
        answered, so the row is CANCELLED and never a restart.  A stop reaches
        the worker as ``ScanInterrupted`` instead.
        """
        caplog.set_level(logging.INFO, logger="saneless.worker")

        def shutdown_then_cancel(
            _scanner: object,
            _paperless: object,
            _settings: object,
            request: PipelineRequest,
        ) -> ScanResult:
            coordinator = request.flip_coordinator
            assert isinstance(coordinator, WorkerFlipCoordinator)
            assert coordinator.interrupt_for_shutdown()
            raise ScanCancelledError(_CANCEL_MESSAGE)

        monkeypatch.setattr("saneless.worker.run_pipeline", shutdown_then_cancel)
        # worker_for builds over this same fixture instance, so the manual
        # duplex profile it adds is the one the worker gets.
        _manual_duplex_settings(default_settings)
        store = JobStore()
        worker = worker_for(store)
        finished = _finish_one_job(worker, store, profile="duplex")

        assert finished.state is JobState.CANCELLED
        assert finished.error == _CANCEL_MESSAGE
        assert finished.error_category is None
        shutdown = f"Job {finished.id} ended by shutdown"
        assert _worker_records(caplog, logging.INFO, shutdown) == []

    def test_cancelled_jobs_never_degrade_the_worker(
        self,
        worker_for: Callable[[JobStore], ScanWorker],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Phase 26 D-10: cancels in a row are job endings, not loop failures."""
        monkeypatch.setattr(
            "saneless.worker.run_pipeline",
            _raising_pipeline(ScanCancelledError(_CANCEL_MESSAGE)),
        )
        store = JobStore()
        worker = worker_for(store)
        try:
            worker.start()
            jobs = _submit_jobs(worker, store, worker_module._DEGRADED_AFTER)
            finished = [
                wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
                for job in jobs
            ]
            health = worker.health
            next_submit = worker.submit(store.create_job("default", "After Cancels"))
        finally:
            worker.stop()
            store.close()

        assert [job.state for job in finished] == [JobState.CANCELLED] * len(jobs)
        assert health is WorkerHealth.HEALTHY
        assert next_submit is SubmitResult.ACCEPTED


# What a jam raised from pass B says on the row.
_PASS_B_JAM = "Scanner error on test:device:001: Document feeder jammed in pass B"


class _CountedPassScanner(StubScannerBackend):
    """
    A manual-duplex scanner with a chosen page count per pass and a held pass B.

    ``_PassBGatedScanner`` already holds pass B open, but it spools exactly one
    page per pass, so "the front count" and "the back count" are the same
    number and a test cannot tell which one it is reading.  This one takes both
    counts, which is what makes ``fronts != backs`` -- and so the back count
    failing to overwrite the front count -- observable.

    Concrete rather than a ``MagicMock``, like every other fake here, and
    inheriting ``get_devices() -> []`` from ``StubScannerBackend`` so startup
    profile generation (D-14) leaves these tests' settings alone.

    Attributes:
        release_pass_b: Set by the test to let pass B return.
        scan_calls: How many times ``scan_pages`` has been entered.

    """

    def __init__(self, fronts: int = 3, backs: int = 3) -> None:
        """
        Hold pass B, and answer each pass with its own page count.

        Args:
            fronts: Pages pass A spools.
            backs: Pages pass B spools.

        """
        self.release_pass_b = threading.Event()
        self.scan_calls = 0
        self._per_pass = (fronts, backs)

    def get_capabilities(self, device_id: str) -> DeviceCapabilities:
        """
        Report a feeder-only device.

        Args:
            device_id: Ignored.

        Returns:
            Capabilities naming a single feeder source.

        """
        return DeviceCapabilities(sources=["ADF"], resolutions=[300], modes=["color"])

    def scan_pages(
        self, device_id: str, settings: ScanSettings, sink: PageSink
    ) -> ScanBatch:
        """
        Spool this pass's pages, holding the second and later calls on the gate.

        Args:
            device_id: Ignored.
            settings: Only ``resolution`` is used, as the pages' dpi and the batch's.
            sink: The pipeline's own sink, which receives the pages.

        Returns:
            A batch of the records the sink returned.

        """
        self.scan_calls += 1
        if self.scan_calls >= 2:
            self.release_pass_b.wait(_PASS_B_GATE_CEILING)
        wanted = self._per_pass[min(self.scan_calls, 2) - 1]
        records = [
            sink.add(_inked_page(), dpi=settings.resolution) for _ in range(wanted)
        ]
        return scan_batch(records, resolution=settings.resolution)


class _JammingPassBScanner(_CountedPassScanner):
    """A ``_CountedPassScanner`` whose pass B jams once the gate releases it."""

    def scan_pages(
        self, device_id: str, settings: ScanSettings, sink: PageSink
    ) -> ScanBatch:
        """
        Spool the pass as usual, then fail pass B as a paper jam.

        Args:
            device_id: Passed through.
            settings: Passed through.
            sink: Passed through.

        Returns:
            Pass A's batch.

        Raises:
            ScanError: On pass B, after the gate is released.

        """
        batch = super().scan_pages(device_id, settings, sink)
        if self.scan_calls >= 2:
            raise ScanError(_PASS_B_JAM)
        return batch


def _run_duplex_job(worker: ScanWorker, store: JobStore, title: str) -> Job:
    """
    Submit one manual-duplex job to an already-started worker.

    Args:
        worker: An already-started worker.
        store: The store the job is created in.
        title: The job title.

    Returns:
        The created job.

    """
    job = store.create_job("duplex", title)
    worker.submit(job)
    return job


class TestFrontPages:
    """
    ``ScanWorker.front_pages`` carries pass A's count out of a running job (D-33).

    The status area renders from the job row, and the row has no column for
    this: CONTEXT forbids a schema migration, and the number is wanted only
    while one specific job is in ``SCANNING_REVERSE``.  So it lives on the
    worker beside ``current_job_id``, is written by the pipeline's pass-count
    callback, and is cleared however the job ends.
    """

    def test_a_fresh_worker_reports_front_pages_as_none(
        self,
        mock_paperless: MagicMock,
        isolated_duplex_settings: Settings,
    ) -> None:
        """Before any job there is no pass A, so there is no count."""
        store = JobStore()
        worker = ScanWorker(
            _CountedPassScanner(), mock_paperless, isolated_duplex_settings, store
        )
        try:
            assert worker.front_pages is None
        finally:
            store.close()

    def test_front_pages_holds_pass_a_count_at_the_flip_prompt(
        self,
        mock_paperless: MagicMock,
        isolated_duplex_settings: Settings,
    ) -> None:
        """
        The count is readable from another thread while the job is in flight.

        The worker thread is blocked in the flip wait, so this read happens on
        the test thread against a live job -- which is exactly how a request
        thread rendering the status area will read it.
        """
        scanner = _CountedPassScanner(fronts=3, backs=3)
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, isolated_duplex_settings, store)
        try:
            worker.start()
            job = _run_duplex_job(worker, store, "Front Count At Prompt")
            wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
            at_prompt = worker.front_pages
            worker.continue_flip(job.id)
        finally:
            scanner.release_pass_b.set()
            worker.stop()
            store.close()

        assert at_prompt == 3

    def test_front_pages_is_readable_while_pass_b_is_in_flight(
        self,
        mock_paperless: MagicMock,
        isolated_duplex_settings: Settings,
    ) -> None:
        """D-33: SCANNING_REVERSE is exactly when the strip wants the number."""
        scanner = _CountedPassScanner(fronts=4, backs=4)
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, isolated_duplex_settings, store)
        try:
            worker.start()
            job = _run_duplex_job(worker, store, "Front Count During Pass B")
            wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
            worker.continue_flip(job.id)
            wait_for_state(store, job.id, JobState.SCANNING_REVERSE, _STATE_BUDGET)
            during = worker.front_pages
            scanner.release_pass_b.set()
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            scanner.release_pass_b.set()
            worker.stop()
            store.close()

        assert during == 4
        assert finished.state is JobState.DONE

    def test_front_pages_is_not_overwritten_by_the_back_count(
        self,
        mock_paperless: MagicMock,
        isolated_duplex_settings: Settings,
    ) -> None:
        """
        The back count arrives too, and is ignored.

        Pass A feeds three sheets and pass B two, so the two counts are
        distinguishable; the upload is held open so the read happens after pass
        B has finished and announced its own number.
        """
        scanner = _CountedPassScanner(fronts=3, backs=2)
        uploading = threading.Event()
        release_upload = threading.Event()

        def holding_upload(*_args: object, **_kwargs: object) -> UploadResult:
            """
            Announce the upload, then wait for the test to release it.

            Returns:
                A successful upload result.

            """
            uploading.set()
            release_upload.wait(_PASS_B_GATE_CEILING)
            return ApiDelivery(task_id="held-task")

        mock_paperless.upload_document.side_effect = holding_upload
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, isolated_duplex_settings, store)
        try:
            worker.start()
            job = _run_duplex_job(worker, store, "Back Count Ignored")
            wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
            worker.continue_flip(job.id)
            scanner.release_pass_b.set()
            assert uploading.wait(_PASS_B_GATE_CEILING)
            after_pass_b = worker.front_pages
        finally:
            release_upload.set()
            scanner.release_pass_b.set()
            worker.stop()
            store.close()

        assert after_pass_b == 3

    def test_front_pages_is_cleared_after_a_successful_job(
        self,
        mock_paperless: MagicMock,
        isolated_duplex_settings: Settings,
    ) -> None:
        """A finished job leaves no count behind for the next one to read."""
        scanner = _CountedPassScanner(fronts=2, backs=2)
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, isolated_duplex_settings, store)
        try:
            worker.start()
            job = _run_duplex_job(worker, store, "Cleared After Success")
            wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
            worker.continue_flip(job.id)
            scanner.release_pass_b.set()
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
            cleared = poll_until(lambda: worker.front_pages is None, _STATE_BUDGET)
        finally:
            scanner.release_pass_b.set()
            worker.stop()
            store.close()

        assert finished.state is JobState.DONE
        assert cleared

    def test_front_pages_is_cleared_after_a_failed_job(
        self,
        mock_paperless: MagicMock,
        isolated_duplex_settings: Settings,
    ) -> None:
        """A pass-B jam is an ERROR, and it clears the count too."""
        scanner = _JammingPassBScanner(fronts=2, backs=2)
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, isolated_duplex_settings, store)
        try:
            worker.start()
            job = _run_duplex_job(worker, store, "Cleared After Error")
            wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
            at_prompt = worker.front_pages
            worker.continue_flip(job.id)
            scanner.release_pass_b.set()
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
            cleared = poll_until(lambda: worker.front_pages is None, _STATE_BUDGET)
        finally:
            scanner.release_pass_b.set()
            worker.stop()
            store.close()

        assert at_prompt == 2
        assert finished.state is JobState.ERROR
        assert cleared

    def test_front_pages_is_cleared_after_a_cancelled_job(
        self,
        mock_paperless: MagicMock,
        isolated_duplex_settings: Settings,
    ) -> None:
        """An Abort at the prompt ends the job before pass B, and clears it."""
        scanner = _CountedPassScanner(fronts=5, backs=5)
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, isolated_duplex_settings, store)
        try:
            worker.start()
            job = _run_duplex_job(worker, store, "Cleared After Cancel")
            wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
            at_prompt = worker.front_pages
            worker.abort_flip(job.id)
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
            cleared = poll_until(lambda: worker.front_pages is None, _STATE_BUDGET)
        finally:
            scanner.release_pass_b.set()
            worker.stop()
            store.close()

        assert at_prompt == 5
        assert finished.state is JobState.CANCELLED
        assert cleared
        assert scanner.scan_calls == 1


def _gate_is_free(worker: ScanWorker) -> bool:
    """
    Report whether the scanner gate can be taken right now, leaving it as found.

    This is the refresher's own move -- ``acquire(blocking=False)``, never a
    blocking wait -- with the release the refresher would also make once its
    probe is done.

    Args:
        worker: The worker whose gate to test.

    Returns:
        Whether the gate was free.

    """
    if not worker.scanner_gate.acquire(blocking=False):
        return False
    worker.scanner_gate.release()
    return True


class _GatedProfileScanner(StubScannerBackend):
    """
    A scanner whose startup ``get_devices`` waits for the test to release it.

    Startup profile generation is the worker thread's first act (D-14) and it
    enters SANE twice, so it is the second place the gate has to be held.
    Holding enumeration open turns that into a state the test can observe.

    It answers ``[]`` once released, so generation logs "no scanners found",
    keeps the bare default and writes nothing.

    Attributes:
        entered: Set by the scanner when enumeration begins.
        release_devices: Set by the test to let enumeration return.

    """

    def __init__(self) -> None:
        """Start with enumeration held."""
        self.entered = threading.Event()
        self.release_devices = threading.Event()

    def get_devices(self) -> list[DeviceInfo]:
        """
        Announce arrival, wait for the test, then report no devices.

        Returns:
            An empty list.

        """
        self.entered.set()
        self.release_devices.wait(_PASS_B_GATE_CEILING)
        return []


class TestScannerGate:
    """
    ``ScanWorker.scanner_gate`` is real mutual exclusion on SANE (D-08).

    Nothing in ``scanner/sane_backend.py`` excludes two concurrent SANE calls:
    ``_refuse_if_wedged`` fires on a *stuck* read rather than a running one,
    and ``_INIT_LOCK`` guards ``sane_init``/``sane_exit`` only.  The advisory
    ``current_job_id is None`` test has a genuine race -- read ``None``, enter
    ``get_devices()``, and the worker starts a job a microsecond later -- and
    on the ``net`` backend losing that race is a second RPC on the control
    wire a scan is using, not merely a slow probe (Pitfall 2).
    """

    def test_scanner_gate_is_free_on_an_idle_worker(
        self,
        mock_paperless: MagicMock,
        isolated_duplex_settings: Settings,
    ) -> None:
        """With no job running the refresher's non-blocking acquire succeeds."""
        store = JobStore()
        worker = ScanWorker(
            _CountedPassScanner(), mock_paperless, isolated_duplex_settings, store
        )
        try:
            assert isinstance(worker.scanner_gate, threading.Lock)
            assert _gate_is_free(worker)
        finally:
            store.close()

    def test_scanner_gate_is_held_while_a_job_is_in_the_pipeline(
        self,
        mock_paperless: MagicMock,
        isolated_duplex_settings: Settings,
    ) -> None:
        """A probe arriving mid-scan finds the gate taken and must skip."""
        scanner = _CountedPassScanner(fronts=2, backs=2)
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, isolated_duplex_settings, store)
        try:
            worker.start()
            job = _run_duplex_job(worker, store, "Gate Held")
            wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
            during = _gate_is_free(worker)
            worker.continue_flip(job.id)
        finally:
            scanner.release_pass_b.set()
            worker.stop()
            store.close()

        assert not during

    def test_scanner_gate_is_released_after_a_successful_job(
        self,
        mock_paperless: MagicMock,
        isolated_duplex_settings: Settings,
    ) -> None:
        """A finished scan hands the scanner back."""
        scanner = _CountedPassScanner(fronts=2, backs=2)
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, isolated_duplex_settings, store)
        try:
            worker.start()
            job = _run_duplex_job(worker, store, "Gate Released On Success")
            wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
            worker.continue_flip(job.id)
            scanner.release_pass_b.set()
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
            freed = poll_until(lambda: _gate_is_free(worker), _STATE_BUDGET)
        finally:
            scanner.release_pass_b.set()
            worker.stop()
            store.close()

        assert finished.state is JobState.DONE
        assert freed

    def test_scanner_gate_is_released_after_a_failed_job(
        self,
        mock_paperless: MagicMock,
        isolated_duplex_settings: Settings,
    ) -> None:
        """A jam must not leave the scanner locked out for the rest of the run."""
        scanner = _JammingPassBScanner(fronts=2, backs=2)
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, isolated_duplex_settings, store)
        try:
            worker.start()
            job = _run_duplex_job(worker, store, "Gate Released On Error")
            wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
            worker.continue_flip(job.id)
            scanner.release_pass_b.set()
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
            freed = poll_until(lambda: _gate_is_free(worker), _STATE_BUDGET)
        finally:
            scanner.release_pass_b.set()
            worker.stop()
            store.close()

        assert finished.state is JobState.ERROR
        assert freed

    def test_scanner_gate_is_released_after_a_cancelled_job(
        self,
        mock_paperless: MagicMock,
        isolated_duplex_settings: Settings,
    ) -> None:
        """Nor may an Abort at the prompt strand it."""
        scanner = _CountedPassScanner(fronts=2, backs=2)
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, isolated_duplex_settings, store)
        try:
            worker.start()
            job = _run_duplex_job(worker, store, "Gate Released On Cancel")
            wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
            worker.abort_flip(job.id)
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
            freed = poll_until(lambda: _gate_is_free(worker), _STATE_BUDGET)
        finally:
            scanner.release_pass_b.set()
            worker.stop()
            store.close()

        assert finished.state is JobState.CANCELLED
        assert freed

    def test_scanner_gate_is_held_while_startup_generation_reads_the_scanner(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
    ) -> None:
        """
        D-14's first act enters SANE twice, so it is gated too.

        ``_read_generated_profiles`` calls ``get_devices`` and then
        ``get_capabilities``; on the ``net`` backend the first is an RPC and
        the second opens the device.  A refresher probe landing in that window
        would be exactly the concurrency Pitfall 2 describes.
        """
        scanner = _GatedProfileScanner()
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, default_settings, store)
        try:
            worker.start()
            assert scanner.entered.wait(_PASS_B_GATE_CEILING)
            during = _gate_is_free(worker)
            scanner.release_devices.set()
            freed = poll_until(lambda: _gate_is_free(worker), _STATE_BUDGET)
        finally:
            scanner.release_devices.set()
            worker.stop()
            store.close()

        assert not during
        assert freed


# The refusal a wedged read makes a restart of SANE raise, in the backend's
# own words: a job refused this way fails exactly as a refused scan does.
_WEDGED_REINIT_REFUSAL = (
    "Could not start a scan on the scanner: a read on test:0 (page 1) has not "
    "returned, and SANE allows no other operation on a device while one is "
    "outstanding. The scan will be possible again as soon as the scanner "
    "releases it. Restart saneless if it does not."
)


class _ReinitRecordingScanner(StubScannerBackend):
    """
    A scanner that logs, in order, every SANE entry the worker makes.

    ``reinitialise``, ``get_devices``, ``get_capabilities`` and ``scan_pages``
    all append their name to one list, so a test can read the order of a
    job's calls.  ``reinitialise`` also records whether the scanner gate
    was free at that moment, as a probe would see it, and can be told to
    refuse the way a wedged read makes the real backend refuse.

    It reports one flatbed, so startup generation builds a ``default``
    profile and a job with no device configured picks this one.

    Attributes:
        calls: The name of each call made, in order.
        gate_free_at_reinit: For each restart, whether the gate could be taken.
        worker: The worker whose gate to test; set once the worker exists.

    """

    def __init__(self, *, refusal: str | None = None) -> None:
        """
        Start with no calls recorded.

        Args:
            refusal: When set, every restart raises a ``ScanError`` with this text.

        """
        self.calls: list[str] = []
        self.gate_free_at_reinit: list[bool] = []
        self.worker: ScanWorker | None = None
        self._refusal = refusal

    def reinitialise(self) -> None:
        """
        Record the restart and the gate's state, then refuse if told to.

        Raises:
            ScanError: When the scanner was built with a refusal.

        """
        self.calls.append("reinitialise")
        if self.worker is not None:
            self.gate_free_at_reinit.append(_gate_is_free(self.worker))
        if self._refusal is not None:
            raise ScanError(self._refusal)

    def get_devices(self) -> list[DeviceInfo]:
        """
        Record the listing and report one flatbed.

        Returns:
            A single device.

        """
        self.calls.append("get_devices")
        return [
            DeviceInfo(
                name="test:0",
                vendor="Test",
                model="Flatbed",
                device_type="flatbed scanner",
            )
        ]

    def get_capabilities(self, device_id: str) -> DeviceCapabilities:
        """
        Record the open and report the stub's plain flatbed.

        Args:
            device_id: Passed through to the stub.

        Returns:
            The stub's capabilities.

        """
        self.calls.append("get_capabilities")
        return super().get_capabilities(device_id)

    def scan_pages(
        self, device_id: str, settings: ScanSettings, sink: PageSink
    ) -> ScanBatch:
        """
        Record the scan and spool the stub's one page.

        Args:
            device_id: Passed through to the stub.
            settings: Passed through to the stub.
            sink: Passed through to the stub.

        Returns:
            The stub's one-page batch.

        """
        self.calls.append("scan_pages")
        return super().scan_pages(device_id, settings, sink)


def _reinit_call_sites() -> list[tuple[str, str]]:
    """
    Find every call to a method named ``reinitialise`` in the saneless package.

    The walk is over the syntax tree rather than the text, so a docstring or
    comment that names the method is not mistaken for a call.

    Returns:
        One ``(file name, enclosing function name)`` pair per call, with
        ``""`` for a call outside any function.

    """
    package = Path(worker_module.__file__).parent
    sites: list[tuple[str, str]] = []
    for path in sorted(package.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owner: dict[int, str] = {}
        # ast.walk visits an outer function before the ones nested in it, so
        # the innermost enclosing function is the last one written here.
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                for inner in ast.walk(node):
                    owner[id(inner)] = node.name
        sites.extend(
            (path.relative_to(package).as_posix(), owner.get(id(node), ""))
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "reinitialise"
        )
    return sites


class TestPerJobReinitialise:
    """
    Every scan job restarts SANE first, under the scanner gate.

    After a saned restart, the net backend keeps using a control connection
    the server has already dropped, so every later open in this process fails
    until SANE is restarted.  Restarting at the top of each job, before its
    first listing or open, means every scan starts on a fresh connection.
    The gate is already held there and no handle of this process is open,
    which is the one state in which a restart is known to be safe.
    """

    def test_a_job_reinitialises_once_under_the_gate_before_listing(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
    ) -> None:
        """The restart is the job's first SANE call, made with the gate held."""
        default_settings.scanner.device = ""
        scanner = _ReinitRecordingScanner()
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, default_settings, store)
        scanner.worker = worker
        try:
            worker.start()
            job = store.create_job("default", "Reinit Once")
            worker.submit(job)
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            worker.stop()
            store.close()

        assert "reinitialise" in scanner.calls, "the job never restarted SANE"
        job_calls = scanner.calls[scanner.calls.index("reinitialise") :]
        assert job_calls[:2] == ["reinitialise", "get_devices"]
        assert "scan_pages" in job_calls
        assert scanner.calls.count("reinitialise") == 1
        assert scanner.gate_free_at_reinit == [False]
        assert finished.state is JobState.DONE

    def test_each_of_two_jobs_reinitialises_once_at_its_start(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
    ) -> None:
        """A saned restart between two jobs is met by the second job's restart."""
        scanner = _ReinitRecordingScanner()
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, default_settings, store)
        scanner.worker = worker
        try:
            worker.start()
            first = store.create_job("default", "Reinit First")
            worker.submit(first)
            wait_for_state(store, first.id, TERMINAL_STATES, _STATE_BUDGET)
            mark = len(scanner.calls)
            second = store.create_job("default", "Reinit Second")
            worker.submit(second)
            wait_for_state(store, second.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            worker.stop()
            store.close()

        assert scanner.calls[:mark].count("reinitialise") == 1
        assert scanner.calls[mark:].count("reinitialise") == 1
        assert scanner.calls[mark : mark + 1] == ["reinitialise"]
        assert scanner.gate_free_at_reinit == [False, False]

    def test_startup_profile_read_does_not_reinitialise(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
    ) -> None:
        """
        The startup read uses the SANE the backend just started; jobs restart it.

        It runs once, before any job, so there is nothing stale to clear.
        """
        scanner = _ReinitRecordingScanner()
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, default_settings, store)
        scanner.worker = worker
        try:
            worker.start()
            generated = poll_until(
                lambda: "flatbed" in worker.profile_names(), _STATE_BUDGET
            )
            before_any_job = list(scanner.calls)
            job = store.create_job("default", "Reinit After Startup")
            worker.submit(job)
            wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            worker.stop()
            store.close()

        assert generated
        assert before_any_job == ["get_devices", "get_capabilities"]
        assert scanner.calls.count("reinitialise") == 1

    def test_a_refused_reinitialise_fails_the_job_as_a_scanner_error(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
    ) -> None:
        """
        A restart refused over a wedged read fails the job and frees the gate.

        Nothing else reaches SANE for that job: no listing and no scan.
        """
        default_settings.scanner.device = ""
        scanner = _ReinitRecordingScanner(refusal=_WEDGED_REINIT_REFUSAL)
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, default_settings, store)
        scanner.worker = worker
        try:
            worker.start()
            job = store.create_job("default", "Reinit Refused")
            worker.submit(job)
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
            freed = poll_until(lambda: _gate_is_free(worker), _STATE_BUDGET)
        finally:
            worker.stop()
            store.close()

        assert "reinitialise" in scanner.calls, "the job never restarted SANE"
        job_calls = scanner.calls[scanner.calls.index("reinitialise") :]
        assert job_calls == ["reinitialise"]
        assert finished.state is JobState.ERROR
        assert finished.error_category is ErrorCategory.SCANNER
        assert finished.error is not None
        assert "Restart saneless" in finished.error
        assert freed

    @pytest.mark.source_structure
    def test_reinitialise_is_called_only_from_the_scan_job(self) -> None:
        """
        SANE is restarted in the job's gated block and nowhere outside the job.

        The job's own restart is in ``_scan_job``; the only other one is a
        multi-page scan's before each later pass, which runs inside that same
        job, under the gate it holds, after the previous pass closed its
        device.  A restart anywhere else could run while a handle is open or a
        listing child holds the gate, which is exactly the state it must never
        see.
        """
        assert _reinit_call_sites() == [
            ("pipeline.py", "_scan_pass"),
            ("worker.py", "_scan_job"),
        ]


# How long a test leaves a job waiting on a held gate before it looks at the
# row.  A SCANNING written ahead of the gate lands within microseconds of the
# job being taken, so this is generous for the failure it is there to catch.
_GATE_WAIT_WINDOW = 0.3


class _GateStateRecorder:
    """
    ``JobStore.update_state`` that notes whether the test had let the gate go.

    Every call records the state it was asked to write beside whether
    ``released`` was set at that moment, then delegates, so the row the worker
    writes is the row the test reads back.

    Args:
        original: The bound ``update_state`` being replaced.
        released: Set by the test just before it releases the scanner gate.

    """

    def __init__(
        self, original: Callable[..., None], released: threading.Event
    ) -> None:
        """Wrap ``original``, watching ``released``."""
        self._original = original
        self._released = released
        self._lock = threading.Lock()
        self._writes: list[tuple[JobState, bool]] = []

    @property
    def writes(self) -> list[tuple[JobState, bool]]:
        """Each state written so far, with whether the gate had been let go."""
        with self._lock:
            return list(self._writes)

    def __call__(
        self,
        job_id: str,
        state: JobState,
        error: str | None = None,
        error_category: ErrorCategory | None = None,
    ) -> None:
        """Record the state and the gate's release, then delegate."""
        with self._lock:
            self._writes.append((state, self._released.is_set()))
        self._original(job_id, state, error=error, error_category=error_category)


def _a_scanner_free_of_startup_generation(
    settings: Settings,
) -> _ReinitRecordingScanner:
    """
    Give a worker test a recording scanner the worker reaches only for a job.

    A second profile makes the set something other than the bare default, so
    the worker thread skips startup generation and never takes the gate for
    it: the first time the worker wants the gate is the job.

    Args:
        settings: The test's settings, given the second profile in place.

    Returns:
        A scanner recording every SANE call the worker makes.

    """
    settings.scanner.device = ""
    settings.profiles["duplex"] = ProfileConfig(source="ADF Duplex")
    return _ReinitRecordingScanner()


class TestScanningWaitsForTheGate:
    """
    SCANNING is recorded only once the worker holds the scanner gate.

    A health check can hold the gate for up to its listing deadline.  A job
    submitted meanwhile has not started scanning, so its row stays PENDING --
    the status area keeps "Starting scan..." -- until the worker has the
    gate and the pipeline reports that the scan has begun.
    """

    def test_a_job_waiting_for_the_gate_stays_pending(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
    ) -> None:
        """With the gate held elsewhere, the taken job's row still reads PENDING."""
        scanner = _a_scanner_free_of_startup_generation(default_settings)
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, default_settings, store)
        gate = worker.scanner_gate
        try:
            worker.start()
            assert gate.acquire(timeout=_PASS_B_GATE_CEILING)
            try:
                job = store.create_job("default", "Waiting For The Gate")
                worker.submit(job)
                taken = poll_until(
                    lambda: worker.current_job_id == job.id, _STATE_BUDGET
                )
                quiet_window(_GATE_WAIT_WINDOW)
                during = store.get_job(job.id)
                calls_during = list(scanner.calls)
            finally:
                gate.release()
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            worker.stop()
            store.close()

        assert taken, "the worker never took the job"
        assert during is not None
        assert during.state is JobState.PENDING
        assert calls_during == []
        assert finished.state is JobState.DONE

    def test_scanning_is_recorded_once_the_gate_is_held(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """SCANNING is written once, after PENDING, and only after the gate is free."""
        scanner = _a_scanner_free_of_startup_generation(default_settings)
        store = JobStore()
        released = threading.Event()
        recorder = _GateStateRecorder(store.update_state, released)
        monkeypatch.setattr(store, "update_state", recorder)
        worker = ScanWorker(scanner, mock_paperless, default_settings, store)
        gate = worker.scanner_gate
        try:
            worker.start()
            assert gate.acquire(timeout=_PASS_B_GATE_CEILING)
            try:
                job = store.create_job("default", "Scanning Under The Gate")
                created = store.get_job(job.id)
                worker.submit(job)
                taken = poll_until(
                    lambda: worker.current_job_id == job.id, _STATE_BUDGET
                )
                quiet_window(_GATE_WAIT_WINDOW)
            finally:
                released.set()
                gate.release()
            scanning = poll_until(
                lambda: any(s is JobState.SCANNING for s, _ in recorder.writes),
                _STATE_BUDGET,
            )
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            worker.stop()
            store.close()

        assert taken, "the worker never took the job"
        assert scanning, "SCANNING was never recorded"
        assert created is not None
        sequence = [created.state, *(state for state, _ in recorder.writes)]
        assert sequence.count(JobState.SCANNING) == 1
        assert sequence.index(JobState.PENDING) < sequence.index(JobState.SCANNING)
        assert [
            after for state, after in recorder.writes if state is JobState.SCANNING
        ] == [True]
        assert finished.state is JobState.DONE

    def test_a_stop_while_waiting_for_the_gate_ends_the_job_untouched(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A job that gets the gate only once the server is stopping never scans.

        A stop can end the check that held the gate, so the waiting job is
        handed the gate in the middle of shutdown.  Starting the scan then
        would feed paper the stop's bounded join walks away from; the job
        ends instead, with the restart text for a job that never started.
        """
        scanner = _a_scanner_free_of_startup_generation(default_settings)
        pipelines: list[str] = []

        def spy_pipeline(
            _scanner: object,
            _paperless: object,
            _settings: object,
            request: PipelineRequest,
        ) -> ScanResult:
            pipelines.append(request.job_id)
            return _success_result()

        monkeypatch.setattr("saneless.worker.run_pipeline", spy_pipeline)
        store = JobStore()
        worker = ScanWorker(scanner, mock_paperless, default_settings, store)
        gate = worker.scanner_gate
        stopped: list[bool] = []

        def stop_the_worker() -> None:
            stopped.append(worker.stop())

        stopper = threading.Thread(
            target=stop_the_worker, name="test-stopper", daemon=True
        )
        try:
            worker.start()
            assert gate.acquire(timeout=_PASS_B_GATE_CEILING)
            try:
                job = store.create_job("default", "Stopped At The Gate")
                worker.submit(job)
                taken = poll_until(
                    lambda: worker.current_job_id == job.id, _STATE_BUDGET
                )
                stopper.start()
                stopping = poll_until(worker._stopping.is_set, _STATE_BUDGET)
            finally:
                gate.release()
            stopper.join(worker_module.STOP_JOIN_SECONDS + _STATE_BUDGET)
            assert not stopper.is_alive(), "the stop never returned"
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            worker.stop()
            store.close()

        assert taken, "the worker never took the job"
        assert stopping, "the stop never began"
        assert stopped == [True]
        assert finished.state is JobState.ERROR
        assert finished.error == restart_error(JobState.PENDING, None)
        assert finished.error_category is restart_category(JobState.PENDING)
        assert scanner.calls == []
        assert pipelines == []


class TestProfileStorage:
    """
    ``ScanWorker.profile_storage`` records what the startup persist did (A-2).

    ``_persist_generated_profiles`` returns ``None`` for two genuinely
    different situations -- no config file was loaded at all, and one was
    loaded and could not be written -- and used to keep no record of which.
    D-22's Profiles row has to tell a household member which happened, and a
    fresh ``os.access()`` probe at check time cannot: Phase 27 D-09's
    motivating failure is EBUSY on a single-file bind mount, where the
    directory is writable and only the rename fails.
    """

    @staticmethod
    def _caps_scanner(mock_scanner: MagicMock) -> None:
        """
        Give the shared mock scanner one device and readable capabilities.

        Args:
            mock_scanner: The shared scanner mock, configured in place.

        """
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

    def test_profile_storage_before_any_attempt_is_the_no_config_file_value(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
    ) -> None:
        """
        An unstarted worker has not written anything, and says so.

        The default is the in-memory answer rather than ``PERSISTED``, because
        claiming the profiles are on disk before any write has been attempted
        is the one answer that could mislead an operator.
        """
        store = JobStore()
        worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
        try:
            assert worker.profile_storage is ProfileStorage.IN_MEMORY_NO_CONFIG_FILE
        finally:
            store.close()

    def test_profile_storage_is_persisted_after_a_successful_write(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """A write that landed means the profiles survive a restart."""
        self._caps_scanner(mock_scanner)
        config_file = tmp_path / "saneless.toml"
        config_file.write_text("# loaded by --config\n")
        default_settings._config_path = config_file

        store = JobStore()
        worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
        try:
            worker.start()
            generated = poll_until(
                lambda: "flatbed" in worker.profile_names(), _STATE_BUDGET
            )
            storage = worker.profile_storage
        finally:
            worker.stop()
            store.close()

        assert generated
        assert storage is ProfileStorage.PERSISTED

    def test_profile_storage_is_no_config_file_when_none_was_loaded(
        self,
        mock_scanner: MagicMock,
        worker_for: Callable[[JobStore], ScanWorker],
    ) -> None:
        """D-17: nothing to write to is not the same as cannot write."""
        self._caps_scanner(mock_scanner)
        store = JobStore()
        worker = worker_for(store)
        assert worker._settings.config_path is None
        try:
            worker.start()
            generated = poll_until(
                lambda: "flatbed" in worker.profile_names(), _STATE_BUDGET
            )
            storage = worker.profile_storage
        finally:
            worker.stop()
            store.close()

        assert generated
        assert storage is ProfileStorage.IN_MEMORY_NO_CONFIG_FILE

    def test_profile_storage_is_unwritable_when_the_write_raises_oserror(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """D-18: a loaded file that will not take the write is the amber case."""
        self._caps_scanner(mock_scanner)
        config_file = tmp_path / "saneless.toml"
        config_file.write_text("# read-only\n")
        default_settings._config_path = config_file

        def refusing(_path: Path, _profiles: object) -> ProfileWriteResult:
            """
            Fail the write the way a read-only mount does.

            Raises:
                OSError: Always.

            """
            raise OSError(errno.EACCES, os.strerror(errno.EACCES))

        monkeypatch.setattr(worker_module, "write_profiles_to_config", refusing)

        store = JobStore()
        worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
        try:
            worker.start()
            generated = poll_until(
                lambda: "flatbed" in worker.profile_names(), _STATE_BUDGET
            )
            storage = worker.profile_storage
        finally:
            worker.stop()
            store.close()

        assert generated
        assert storage is ProfileStorage.IN_MEMORY_UNWRITABLE

    def test_profile_storage_is_unwritable_when_the_write_raises_unexpectedly(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        WR-04's catch-all branch records the same outcome as the OSError one.

        Two branches, one truth: whatever went wrong, a file was loaded and the
        profiles did not reach it.
        """
        self._caps_scanner(mock_scanner)
        config_file = tmp_path / "saneless.toml"
        config_file.write_text("# loaded\n")
        default_settings._config_path = config_file

        def exploding(_path: Path, _profiles: object) -> ProfileWriteResult:
            """
            Fail the write the way a tomlkit container error would.

            Raises:
                RuntimeError: Always.

            """
            msg = "tomlkit refused the container"
            raise RuntimeError(msg)

        monkeypatch.setattr(worker_module, "write_profiles_to_config", exploding)

        store = JobStore()
        worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
        try:
            worker.start()
            generated = poll_until(
                lambda: "flatbed" in worker.profile_names(), _STATE_BUDGET
            )
            storage = worker.profile_storage
        finally:
            worker.stop()
            store.close()

        assert generated
        assert storage is ProfileStorage.IN_MEMORY_UNWRITABLE

    @staticmethod
    def _second_profile(settings: Settings) -> None:
        """
        Take the settings out of the bare default, the way a config file does.

        A second profile is what every real deployment has once
        ``saneless auto-profiles`` has run once and written the file, and it is
        the shape ``docker-compose.yml``'s mounted ``./config`` produces. It
        makes ``is_bare_default`` False, so startup generation is skipped.

        Args:
            settings: The settings to customise in place.

        """
        settings.profiles = {
            "default": ProfileConfig(),
            "adf": ProfileConfig(source="ADF"),
        }

    def test_profile_storage_is_persisted_when_generation_was_skipped(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        CR-01: the production shape reported a permanent falsehood.

        An operator with a real config file holding profiles that are not the
        bare default never reaches a branch that records the storage outcome --
        ``_generate_startup_profiles`` returns at ``if not bare``. The seed said
        no config file was in use, for the life of the process, on the one
        deployment shape the compose file ships.
        """
        self._caps_scanner(mock_scanner)
        self._second_profile(default_settings)
        config_file = tmp_path / "saneless.toml"
        config_file.write_text("# loaded by --config\n")
        default_settings._config_path = config_file

        store = JobStore()
        worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
        try:
            assert not is_bare_default(default_settings)
            worker.start()
            stopped = worker.stop()
            storage = worker.profile_storage
        finally:
            worker.stop()
            store.close()

        assert stopped
        assert storage is ProfileStorage.PERSISTED

    def test_profile_storage_is_no_config_file_when_generation_was_skipped(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
    ) -> None:
        """Customised profiles from the environment alone still have no file."""
        self._caps_scanner(mock_scanner)
        self._second_profile(default_settings)

        store = JobStore()
        worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
        try:
            assert default_settings.config_path is None
            worker.start()
            stopped = worker.stop()
            storage = worker.profile_storage
        finally:
            worker.stop()
            store.close()

        assert stopped
        assert storage is ProfileStorage.IN_MEMORY_NO_CONFIG_FILE

    def test_profile_storage_is_persisted_when_no_scanner_was_found(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        D-15: a SANE failure leaves the loaded profiles exactly where they were.

        The autouse fixture answers ``get_devices`` with ``[]``, so
        ``_read_generated_profiles`` returns ``None`` and generation gives up.
        Nothing was written, but nothing moved either: the bare default came out
        of the config file and is still in it.
        """
        config_file = tmp_path / "saneless.toml"
        config_file.write_text("# loaded by --config\n")
        default_settings._config_path = config_file

        store = JobStore()
        worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
        try:
            worker.start()
            stopped = worker.stop()
            storage = worker.profile_storage
        finally:
            worker.stop()
            store.close()

        assert stopped
        assert mock_scanner.get_devices.called
        assert storage is ProfileStorage.PERSISTED

    def test_profile_storage_is_no_config_file_when_no_scanner_was_found(
        self,
        mock_scanner: MagicMock,
        worker_for: Callable[[JobStore], ScanWorker],
    ) -> None:
        """A failed generation with no file loaded is still the no-file answer."""
        store = JobStore()
        worker = worker_for(store)
        try:
            assert worker._settings.config_path is None
            worker.start()
            stopped = worker.stop()
            storage = worker.profile_storage
        finally:
            worker.stop()
            store.close()

        assert stopped
        assert mock_scanner.get_devices.called
        assert storage is ProfileStorage.IN_MEMORY_NO_CONFIG_FILE

    def test_profile_storage_renders_a_green_profiles_row_with_a_config_file(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        D-02 is about the words, so assert the rendered row, not just the enum.

        The amber row CR-01 produced told a household member to create a
        configuration file they already had. Asserting the enum alone would not
        have caught that the sentence was false.
        """
        self._caps_scanner(mock_scanner)
        self._second_profile(default_settings)
        config_file = tmp_path / "saneless.toml"
        config_file.write_text("# loaded by --config\n")
        default_settings._config_path = config_file

        store = JobStore()
        worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
        try:
            worker.start()
            stopped = worker.stop()
            storage = worker.profile_storage
        finally:
            worker.stop()
            store.close()

        results = run_checks(
            CheckContext(
                settings=default_settings,
                scanner=None,
                paperless=None,
                profile_storage=storage,
            )
        )
        row = next(result for result in results if result.key is CheckKey.PROFILES)

        assert stopped
        assert row.state is CheckState.OK
        assert "in memory" not in row.message.lower()
        assert not row.next_step


# What the simulated locked job database says.  The progress-write tests use
# the text SQLite really gives a writer that lost the lock.
_LOCKED = "database is locked"

# A flip timeout short enough for a test to sit through, and still a whole
# second longer than the minimum the setting allows.
_SHORT_FLIP_TIMEOUT = 2


class _FailingStateWrite:
    """
    ``JobStore.update_state`` that raises on its first calls for one state.

    Every call records the state it was asked to write.  The first
    ``failures`` calls for ``state`` raise ``sqlite3.OperationalError`` as a
    locked database would; every other call delegates to the real method, so
    the row the worker writes is the row the test reads back.

    Args:
        original: The bound ``update_state`` being replaced.
        state: The state whose writes fail.
        failures: How many of that state's writes fail before one lands.

    """

    def __init__(
        self,
        original: Callable[..., None],
        state: JobState,
        failures: int,
    ) -> None:
        """Wrap ``original``, failing ``failures`` writes of ``state``."""
        self._original = original
        self._state = state
        self._failures = failures
        self._lock = threading.Lock()
        self._seen: list[JobState] = []
        self._failed = 0

    @property
    def seen(self) -> list[JobState]:
        """Every state the worker asked to write so far, in order."""
        with self._lock:
            return list(self._seen)

    @property
    def failed(self) -> int:
        """How many writes have raised so far."""
        with self._lock:
            return self._failed

    def __call__(
        self,
        job_id: str,
        state: JobState,
        error: str | None = None,
        error_category: ErrorCategory | None = None,
    ) -> None:
        """Record the state, then raise or delegate."""
        with self._lock:
            self._seen.append(state)
            failing = state is self._state and self._failed < self._failures
            if failing:
                self._failed += 1
        if failing:
            raise sqlite3.OperationalError(_LOCKED)
        self._original(job_id, state, error=error, error_category=error_category)


def _loop_failures_at_finish(
    worker: ScanWorker, store: JobStore, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[int, bool]]:
    """
    Record the loop-failure count and degraded flag at each terminal write.

    The loop resets the count after every job whose writes all land, so reading
    it once the job is over proves nothing.  Reading it as the terminal write
    starts does: anything the job's progress writes counted is still there.

    Args:
        worker: The unstarted worker under test.
        store: The store it writes to.
        monkeypatch: Replaces ``store.finish_job``.

    Returns:
        A list that gains ``(count, degraded)`` at every ``finish_job`` call.

    """
    snapshots: list[tuple[int, bool]] = []
    original = store.finish_job

    def watching_finish(
        job_id: str,
        state: JobState,
        result: JobResult | None = None,
        error: str | None = None,
        error_category: ErrorCategory | None = None,
    ) -> None:
        snapshots.append((worker._consecutive_loop_failures, worker._degraded.is_set()))
        original(
            job_id, state, result=result, error=error, error_category=error_category
        )

    monkeypatch.setattr(store, "finish_job", watching_finish)
    return snapshots


def _progress_write_warnings(
    caplog: pytest.LogCaptureFixture, job_id: str
) -> list[logging.LogRecord]:
    """
    Return the worker's WARNINGs about ``job_id`` that carry a store error.

    Args:
        caplog: The test's log capture.
        job_id: The job the warning must name.

    Returns:
        The matching records, in order.

    """
    return [
        record
        for record in caplog.records
        if record.name == "saneless.worker"
        and record.levelno == logging.WARNING
        and job_id in record.getMessage()
        and record.exc_info is not None
        and isinstance(record.exc_info[1], sqlite3.OperationalError)
    ]


class TestProgressWriteFailures:
    """
    A failed progress write to the job store never changes how a scan ends.

    The thumbnail and the active-state writes only tell observers how far a
    scan has got.  A locked or failing job database during one of them used to
    abort the run, file the store's error as a scanner fault and, outside the
    pipeline's guard windows, delete the spooled pages.  Each test here runs
    the real pipeline over a manual-duplex job, so the spool, the flip wait and
    the assembly are all real; only the scanner and Paperless are fakes.
    """

    @pytest.mark.parametrize(
        "state",
        [
            JobState.SCANNING,
            JobState.ASSEMBLING,
            JobState.UPLOADING,
            JobState.SCANNING_REVERSE,
        ],
        ids=lambda state: state.value.lower(),
    )
    def test_a_progress_write_failing_once_leaves_the_scan_to_finish(
        self,
        state: JobState,
        mock_paperless: MagicMock,
        isolated_duplex_settings: Settings,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The document is uploaded, the job is DONE and the failure is a WARNING."""
        caplog.set_level(logging.WARNING, logger="saneless.worker")
        scanner = _CountedPassScanner(fronts=1, backs=1)
        scanner.release_pass_b.set()
        store = JobStore()
        updates = _FailingStateWrite(store.update_state, state, failures=1)
        worker = ScanWorker(scanner, mock_paperless, isolated_duplex_settings, store)
        # A MonkeyPatch context rather than the fixture keeps this parametrised
        # test inside the lint's argument limit.
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(store, "update_state", updates)
            at_finish = _loop_failures_at_finish(worker, store, patch)
            try:
                worker.start()
                job = _run_duplex_job(worker, store, f"Locked At {state.value}")
                wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
                claimed = worker.continue_flip(job.id)
                finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
            finally:
                worker.stop()
                store.close()

        assert claimed
        assert updates.failed == 1
        assert finished.state is JobState.DONE
        assert finished.error is None
        assert finished.error_category is None
        assert mock_paperless.upload_document.call_count == 1
        warnings = _progress_write_warnings(caplog, job.id)
        assert len(warnings) == 1
        assert state.value in warnings[0].getMessage()
        assert at_finish == [(0, False)]
        # Nothing was filed as a failure: the pages went to Paperless.
        assert not list(isolated_duplex_settings.output.failed_dir.glob("*"))

    def test_a_progress_write_failing_at_the_thumbnail_leaves_the_scan_to_finish(
        self,
        mock_paperless: MagicMock,
        isolated_duplex_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The job is DONE without a thumbnail, and the failure is a WARNING."""
        caplog.set_level(logging.WARNING, logger="saneless.worker")
        scanner = _CountedPassScanner(fronts=1, backs=1)
        scanner.release_pass_b.set()
        store = JobStore()
        thumbnails = _StoreFault(store.update_thumbnail, frozenset({1}))
        monkeypatch.setattr(store, "update_thumbnail", thumbnails)
        worker = ScanWorker(scanner, mock_paperless, isolated_duplex_settings, store)
        at_finish = _loop_failures_at_finish(worker, store, monkeypatch)
        try:
            worker.start()
            job = _run_duplex_job(worker, store, "Locked At The Thumbnail")
            wait_for_state(store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET)
            claimed = worker.continue_flip(job.id)
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            worker.stop()
            store.close()

        assert claimed
        assert len(thumbnails.calls) == 1
        assert finished.state is JobState.DONE
        assert finished.error is None
        assert finished.error_category is None
        assert finished.thumbnail is None
        assert mock_paperless.upload_document.call_count == 1
        warnings = _progress_write_warnings(caplog, job.id)
        assert len(warnings) == 1
        assert "thumbnail" in warnings[0].getMessage()
        assert at_finish == [(0, False)]
        assert not list(isolated_duplex_settings.output.failed_dir.glob("*"))

    def test_a_progress_write_failing_once_at_the_flip_prompt_is_retried(
        self,
        mock_paperless: MagicMock,
        isolated_duplex_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        The prompt renders from the row, so its write is tried a second time.

        Reaching AWAITING_FLIP at all is the proof the retry landed: the first
        write of it raised.  The operator's Continue is then accepted and the
        scan finishes as if nothing had happened.
        """
        caplog.set_level(logging.WARNING, logger="saneless.worker")
        scanner = _CountedPassScanner(fronts=1, backs=1)
        scanner.release_pass_b.set()
        store = JobStore()
        updates = _FailingStateWrite(
            store.update_state, JobState.AWAITING_FLIP, failures=1
        )
        monkeypatch.setattr(store, "update_state", updates)
        worker = ScanWorker(scanner, mock_paperless, isolated_duplex_settings, store)
        at_finish = _loop_failures_at_finish(worker, store, monkeypatch)
        try:
            worker.start()
            job = _run_duplex_job(worker, store, "Locked At The Prompt Once")
            waiting = wait_for_state(
                store, job.id, JobState.AWAITING_FLIP, _STATE_BUDGET
            )
            claimed = worker.continue_flip(job.id)
            finished = wait_for_state(store, job.id, TERMINAL_STATES, _STATE_BUDGET)
        finally:
            worker.stop()
            store.close()

        assert waiting.state is JobState.AWAITING_FLIP
        assert claimed
        assert updates.seen.count(JobState.AWAITING_FLIP) == 2
        assert finished.state is JobState.DONE
        assert finished.error is None
        assert mock_paperless.upload_document.call_count == 1
        assert scanner.scan_calls == 2
        warnings = _progress_write_warnings(caplog, job.id)
        assert len(warnings) == 1
        assert JobState.AWAITING_FLIP.value in warnings[0].getMessage()
        assert at_finish == [(0, False)]

    def test_a_progress_write_failing_twice_at_the_flip_prompt_waits_for_the_timeout(
        self,
        mock_paperless: MagicMock,
        isolated_duplex_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        With the prompt's row unwritable, the flip timeout still keeps the fronts.

        One retry, then the worker logs and keeps waiting rather than ending
        the scan itself.  Nobody can see a prompt to answer, so the bounded
        flip wait runs out and ends the job the way any unanswered flip ends:
        ERROR with the timeout's own text, pass A's fronts filed in
        ``failed/``, and the store's error never recorded as a scanner fault.
        """
        caplog.set_level(logging.WARNING, logger="saneless.worker")
        settings = isolated_duplex_settings
        settings.output.operator_wait_timeout_seconds = _SHORT_FLIP_TIMEOUT
        scanner = _CountedPassScanner(fronts=2, backs=2)
        store = JobStore()
        updates = _FailingStateWrite(
            store.update_state, JobState.AWAITING_FLIP, failures=2
        )
        monkeypatch.setattr(store, "update_state", updates)
        worker = ScanWorker(scanner, mock_paperless, settings, store)
        at_finish = _loop_failures_at_finish(worker, store, monkeypatch)
        try:
            worker.start()
            submitted = time.monotonic()
            job = _run_duplex_job(worker, store, "Locked At The Prompt Twice")
            finished = wait_for_state(
                store, job.id, TERMINAL_STATES, _SHORT_FLIP_TIMEOUT + _STATE_BUDGET
            )
            elapsed = time.monotonic() - submitted
        finally:
            scanner.release_pass_b.set()
            worker.stop()
            store.close()

        # Exactly one retry: two attempts, and no third.
        assert updates.seen.count(JobState.AWAITING_FLIP) == 2
        assert len(_progress_write_warnings(caplog, job.id)) == 2
        # It waited out the flip timeout rather than ending at the failed write.
        assert elapsed >= _SHORT_FLIP_TIMEOUT
        assert finished.state is JobState.ERROR
        assert finished.error is not None
        assert "flip wait timed out" in finished.error
        assert _LOCKED not in finished.error
        assert scanner.scan_calls == 1
        assert mock_paperless.upload_document.call_count == 0
        preserved = sorted(settings.output.failed_dir.glob("*.pdf"))
        assert len(preserved) == 1
        assert "fronts" in preserved[0].name
        assert at_finish == [(0, False)]
