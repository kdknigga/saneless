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

from .auto_profiles import (
    generate_profiles,
    is_bare_default,
    resolve_config_path,
    write_profiles_to_config,
)
from .exceptions import ConfigError, FeederEmptyError, PaperlessError, ScanError
from .job import ErrorCategory, JobState
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
        self._transition_event = threading.Event()
        self._current_job_id: str | None = None
        self._auto_generated = False

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

    def wait_transition(self, timeout: float = 2.0) -> bool:
        """
        Wait for worker to transition state after flip continue.

        Args:
            timeout: Maximum seconds to wait.

        Returns:
            True if transition occurred, False on timeout.

        """
        result = self._transition_event.wait(timeout=timeout)
        self._transition_event.clear()
        return result

    @property
    def is_alive(self) -> bool:
        """Whether the worker thread is currently running."""
        return self._thread.is_alive()

    @property
    def current_job_id(self) -> str | None:
        """ID of the currently processing job, or None."""
        return self._current_job_id

    def _categorize_error(self, exc: Exception) -> ErrorCategory:
        """
        Map an exception to its error category.

        Args:
            exc: The caught exception.

        Returns:
            The appropriate ErrorCategory value.

        """
        if isinstance(exc, FeederEmptyError):
            return ErrorCategory.FEEDER
        if isinstance(exc, ConfigError):
            return ErrorCategory.CONFIG
        if isinstance(exc, ScanError):
            return ErrorCategory.SCANNER
        if isinstance(exc, PaperlessError):
            return ErrorCategory.UPLOAD
        return ErrorCategory.UNKNOWN

    def _run(self) -> None:
        """Worker loop: process jobs until sentinel None received."""
        while True:
            item = self._queue.get()
            if item is None:
                break
            self._process_job(item)

    def _maybe_auto_generate(self) -> None:
        """Auto-generate profiles from scanner if only bare default exists."""
        if self._auto_generated:
            return
        self._auto_generated = True  # Only try once regardless of outcome

        if not is_bare_default(self._settings):
            return

        try:
            devices = self._scanner.get_devices()
            if not devices:
                logger.warning("Auto-profiles: no scanners found, using bare default")
                return
            device_id = self._settings.scanner.device or devices[0].name
            caps = self._scanner.get_capabilities(device_id)
            profiles = generate_profiles(caps)
            config_path = resolve_config_path()
            written = write_profiles_to_config(config_path, profiles)
            # Update in-memory settings
            for name, profile in profiles.items():
                self._settings.profiles[name] = profile
            if written:
                logger.info(
                    "Auto-generated %d profile(s): %s",
                    len(written),
                    ", ".join(written),
                )
        except Exception:
            logger.warning(
                "Auto-profiles: scanner unreachable, using bare default",
                exc_info=True,
            )

    def _process_job(self, job: Job) -> None:
        """
        Execute a single scan job through the pipeline.

        Args:
            job: The Job to process.

        """
        self._current_job_id = job.id
        self._maybe_auto_generate()
        self._job_store.update_state(job.id, JobState.SCANNING)
        self._transition_event.set()

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
                self._transition_event.clear()
                self._job_store.update_state(_jid, JobState.AWAITING_FLIP)
            elif msg == "Assembling PDF...":
                self._job_store.update_state(_jid, JobState.ASSEMBLING)
                self._transition_event.set()
            elif msg == "Uploading to paperless-ngx...":
                self._job_store.update_state(_jid, JobState.UPLOADING)
                self._transition_event.set()

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
            self._transition_event.set()
        except Exception as exc:
            category = self._categorize_error(exc)
            self._job_store.update_state(
                job.id,
                JobState.ERROR,
                error=str(exc),
                error_category=category,
            )
            logger.error("Job %s failed (%s): %s", job.id, category.value.lower(), exc)
        finally:
            self._flip_event = None
            self._abort_event = None
            self._current_job_id = None
