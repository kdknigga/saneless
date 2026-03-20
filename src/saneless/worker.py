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
        self._job_store = job_store
        self._queue: queue.Queue[Job | None] = queue.Queue(maxsize=10)
        self._thread = threading.Thread(target=self._run, daemon=True)

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

    def _run(self) -> None:
        """Worker loop: process jobs until sentinel None received."""
        while True:
            item = self._queue.get()
            if item is None:
                break

            job = item
            self._job_store.update_state(job.id, JobState.SCANNING)

            try:
                request = PipelineRequest(
                    profile_name=job.profile,
                    title=job.title,
                    tags=job.tags or None,
                    correspondent=job.correspondent,
                    status_callback=logger.info,
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
