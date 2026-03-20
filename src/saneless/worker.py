"""
Background worker thread consuming scan jobs from a queue.

The ScanWorker runs a daemon thread that processes Job objects
submitted via a queue.Queue, updating job state through the
JobStore as the pipeline progresses.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import TYPE_CHECKING

from .job import JobState
from .pipeline import PipelineRequest, run_pipeline

if TYPE_CHECKING:
    from .config import Settings
    from .job import Job, JobStore
    from .paperless import PaperlessClient
    from .scanner.base import ScannerBackend

__all__ = ["ScanWorker"]

logger = logging.getLogger(__name__)


class ScanWorker:
    """
    Background worker that processes scan jobs from a queue.

    Args:
        scanner: Scanner backend instance.
        paperless: Paperless-ngx API client.
        settings: Application settings.
        job_store: Job persistence store.

    """

    def __init__(
        self,
        scanner: ScannerBackend,
        paperless: PaperlessClient,
        settings: Settings,
        job_store: JobStore,
    ) -> None:
        """Initialize the worker with its dependencies and start queue."""
        self._scanner = scanner
        self._paperless = paperless
        self._settings = settings
        self._job_store: JobStore = job_store
        self._queue: queue.Queue[Job | None] = queue.Queue(maxsize=10)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._flip_event: threading.Event | None = None
        self._abort_event: threading.Event | None = None
        self._current_job_id: str | None = None

    def start(self) -> None:
        """Start the worker thread."""
        self._thread.start()
        logger.info("ScanWorker started")

    def stop(self) -> None:
        """Signal the worker to stop and wait for it to finish."""
        self._queue.put(None)
        self._thread.join(timeout=5)
        logger.info("ScanWorker stopped")

    def submit(self, job: Job) -> None:
        """
        Submit a job for processing.

        Args:
            job: The Job to process.

        """
        self._queue.put(job)
        logger.info("Job %s submitted to worker queue", job.id)

    def continue_flip(self) -> None:
        """Signal the worker to continue with pass B of manual duplex."""
        if self._flip_event is not None:
            self._flip_event.set()
            logger.info("Manual duplex: continue signal sent")

    def abort_flip(self) -> None:
        """Signal the worker to abort manual duplex scan."""
        if self._abort_event is not None:
            self._abort_event.set()
        if self._flip_event is not None:
            self._flip_event.set()  # Unblock the wait
            logger.info("Manual duplex: abort signal sent")

    @property
    def current_job_id(self) -> str | None:
        """ID of the currently processing job, or None."""
        return self._current_job_id

    def _run(self) -> None:
        """Worker loop: process jobs until sentinel None received."""
        while True:
            item = self._queue.get()
            if item is None:
                break

            job = item
            self._current_job_id = job.id
            self._job_store.update_state(job.id, JobState.SCANNING)

            # Detect manual duplex from profile source
            profile = self._settings.profiles.get(job.profile)
            source = profile.source.lower() if profile else ""
            is_manual_duplex = "manual" in source and "duplex" in source

            if is_manual_duplex:
                self._flip_event = threading.Event()
                self._abort_event = threading.Event()
            else:
                self._flip_event = None
                self._abort_event = None

            def _thumbnail_cb(thumb: str, _jid: str = job.id) -> None:
                self._job_store.update_thumbnail(_jid, thumb)

            def _status_cb(msg: str, _jid: str = job.id) -> None:
                logger.info(msg)
                if msg == "Awaiting flip...":
                    self._job_store.update_state(_jid, JobState.AWAITING_FLIP)

            try:
                request = PipelineRequest(
                    profile_name=job.profile,
                    title=job.title,
                    tags=job.tags or None,
                    correspondent=job.correspondent,
                    status_callback=_status_cb,
                    thumbnail_callback=_thumbnail_cb,
                    flip_event=self._flip_event,
                    abort_event=self._abort_event,
                )
                run_pipeline(
                    self._scanner,
                    self._paperless,
                    self._settings,
                    request,
                )
                self._job_store.update_state(job.id, JobState.DONE)
            except Exception as exc:
                self._job_store.update_state(
                    job.id,
                    JobState.ERROR,
                    error=str(exc),
                )
                logger.error("Job %s failed: %s", job.id, exc)
            finally:
                self._flip_event = None
                self._abort_event = None
                self._current_job_id = None
