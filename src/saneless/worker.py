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
from .job import JobResult
from .pipeline import FlipCoordinator, PipelineEvent, PipelineRequest, run_pipeline
from .vocabulary import (
    ACTIVE_STATES,
    FlipOutcome,
    JobState,
    classify_error,
    job_state_for,
)

if TYPE_CHECKING:
    from .config import Settings
    from .job import Job, JobStore
    from .paperless import PaperlessClient
    from .scanner.base import ScannerBackend

__all__ = ["ScanWorker", "WorkerFlipCoordinator"]

logger = logging.getLogger(__name__)


class WorkerFlipCoordinator(FlipCoordinator):
    """
    The web flip coordinator: one answer, for one job, claimed once, and final.

    The Continue and Abort routes signal it from request threads while the
    worker thread waits on it.  Whichever of Continue, Abort or the timeout
    claims the answer first is the answer; anything arriving later is dropped
    (D-16).

    It is also bound to one job and accepts a signal only once armed.  The
    worker arms it when the job announces ``AWAITING_FLIP``, which is after
    pass A has finished.  Until then a signal is dropped, not queued: a stale
    Abort double-clicked at the previous job's prompt, or a Continue sent
    during pass A, would otherwise pre-answer a prompt nobody has seen yet and
    abort the wrong job or start pass B on an unflipped stack (CR-01).
    ``FlipCoordinator`` itself is unchanged (D-09): arming is this class's
    detail, not part of the contract the pipeline waits on.

    The answer is written under a lock *before* the event is set, so a waiter
    that wakes always finds an answer to read -- there is no window in which
    the event says "resolved" and the slot still says nothing.  That ordering
    is what makes it race-free by construction rather than by timing.

    Args:
        job_id: The id of the job whose flip this coordinator answers.

    """

    def __init__(self, job_id: str) -> None:
        """Start unarmed and unanswered, bound to ``job_id``."""
        self._job_id = job_id
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._armed = False
        self._outcome: FlipOutcome | None = None

    @property
    def job_id(self) -> str:
        """The id of the job whose flip this coordinator answers."""
        return self._job_id

    @property
    def armed(self) -> bool:
        """Whether the flip prompt exists, so a signal can claim the answer."""
        with self._lock:
            return self._armed

    @property
    def answer(self) -> FlipOutcome | None:
        """The claimed answer, or ``None`` while the wait is unanswered."""
        with self._lock:
            return self._outcome

    def arm(self) -> None:
        """Open the flip prompt to signals.  Idempotent."""
        with self._lock:
            self._armed = True

    def signal_continue(self) -> bool:
        """
        Answer the wait: the operator flipped the stack.

        Returns:
            Whether this signal claimed the answer.  ``False`` means it was
            dropped: sent before the prompt was armed, or after an answer.

        """
        return self._signal(FlipOutcome.CONTINUED)

    def signal_abort(self) -> bool:
        """
        Answer the wait: the operator gave up at the flip prompt.

        Returns:
            Whether this signal claimed the answer.  ``False`` means it was
            dropped: sent before the prompt was armed, or after an answer.

        """
        return self._signal(FlipOutcome.ABORTED)

    def wait_for_flip(self, timeout: float) -> FlipOutcome:
        """
        Block until answered, or claim ``TIMED_OUT`` when ``timeout`` elapses.

        The wait arms the coordinator first, as a backstop: a caller that never
        announces ``AWAITING_FLIP`` still gets a prompt its operator can answer.

        On expiry the timeout is claimed through the single-answer slot, and
        what comes back is whatever answer is actually in effect -- so a
        Continue that landed between the wait expiring and this claim is
        honoured rather than overwritten.

        Args:
            timeout: The longest to wait, in seconds.

        Returns:
            The one answer this coordinator resolved to.

        """
        self.arm()
        # One path for both endings.  If a signal set the event, _resolve finds
        # that answer already claimed and hands it back; if the wait expired,
        # TIMED_OUT is offered and wins unless a signal claimed first after
        # all.  Either way the result is the claimed answer, never a guess.
        self._event.wait(timeout)
        return self._resolve(FlipOutcome.TIMED_OUT)

    def _signal(self, outcome: FlipOutcome) -> bool:
        """
        Claim the answer for an operator signal, if the prompt is armed and open.

        Unlike ``_resolve``, this honours ``armed``: a signal before the prompt
        exists, or after an answer, leaves the slot and the event untouched.

        Args:
            outcome: The answer the operator is offering.

        Returns:
            Whether ``outcome`` became the answer.

        """
        with self._lock:
            if not self._armed or self._outcome is not None:
                return False
            self._outcome = outcome
        self._event.set()
        return True

    def _resolve(self, outcome: FlipOutcome) -> FlipOutcome:
        """
        Claim the single answer unconditionally, unless one is already claimed.

        Only the timeout uses this path; it ignores ``armed``, which is why the
        operator signals go through ``_signal`` instead.

        Returning the answer in effect, rather than asserting one exists, is
        what lets ``wait_for_flip`` narrow ``FlipOutcome | None`` to
        ``FlipOutcome`` without an ``assert`` -- which ``S101`` bans in
        ``src/``.

        Args:
            outcome: The answer this caller is offering.

        Returns:
            The claimed answer: ``outcome`` if it was first, otherwise the
            answer that beat it.

        """
        with self._lock:
            if self._outcome is None:
                self._outcome = outcome
            claimed = self._outcome
        self._event.set()
        return claimed


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
        self._flip_coordinator: WorkerFlipCoordinator | None = None
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
        coordinator = self._flip_coordinator
        if coordinator is not None:
            coordinator.signal_continue()
            logger.info("Manual duplex: continue signal sent")

    def abort_flip(self) -> None:
        """Signal the worker to abort manual duplex scan."""
        coordinator = self._flip_coordinator
        if coordinator is not None:
            coordinator.signal_abort()
            logger.info("Manual duplex: abort signal sent")

    @property
    def is_alive(self) -> bool:
        """Whether the worker thread is currently running."""
        return self._thread.is_alive()

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

        # Flip machinery follows profile.duplex alone, the same field
        # run_pipeline reads to choose the strategy.  source is a pure SANE
        # value and is never consulted: a second copy of the detection rule
        # here could drift from the pipeline's and skip the flip wait (C-02).
        profile = self._settings.profiles.get(job.profile)
        is_manual_duplex = profile is not None and profile.duplex == "manual"

        coordinator = WorkerFlipCoordinator(job.id) if is_manual_duplex else None
        self._flip_coordinator = coordinator

        def _thumbnail_cb(thumb: str, _jid: str = job.id) -> None:
            self._job_store.update_thumbnail(_jid, thumb)

        # The worker persisted SCANNING just above, before starting the
        # pipeline.  run_pipeline re-announces it as its first event; rewriting
        # the state we just wrote would blank error/error_category a second
        # time for no change of state.
        persisted_state = JobState.SCANNING

        def _status_cb(event: PipelineEvent, _jid: str = job.id) -> None:
            nonlocal persisted_state
            logger.info("Pipeline event: %s", event.value)
            state = event.job_state
            if state not in ACTIVE_STATES:
                # DONE is terminal.  The worker writes it only once
                # run_pipeline has returned and its temporary directory is
                # gone -- never from in here.
                return
            if state is persisted_state:
                # Already persisted; not a transition.
                return
            # Every active state, AWAITING_FLIP and SCANNING_REVERSE included,
            # is simply persisted.  The row is the only thing observers read:
            # the web UI re-reads it each poll, so nothing here needs signalling.
            if state is JobState.AWAITING_FLIP and coordinator is not None:
                # Arm BEFORE persisting.  Any observer that reads AWAITING_FLIP
                # from the store -- the status poll, which renders Continue and
                # Abort -- must find the coordinator already armed; arming
                # after the write would open a window in which a click on a
                # freshly rendered Continue is dropped.  Pass A has already
                # finished when AWAITING_FLIP is announced, so arming here
                # cannot accept a click sent during pass A (CR-01).
                coordinator.arm()
            self._job_store.update_state(_jid, state)
            persisted_state = state

        try:
            request = PipelineRequest(
                profile_name=job.profile,
                title=job.title,
                # The assembled PDF is named from this id, which is what makes
                # two same-second scans of the same title two files rather than
                # one overwriting the other.
                job_id=job.id,
                tags=job.tags or None,
                correspondent=job.correspondent,
                status_callback=_status_cb,
                thumbnail_callback=_thumbnail_cb,
                flip_coordinator=coordinator,
            )
            result = run_pipeline(
                self._scanner,
                self._paperless,
                self._settings,
                request,
            )
            # The terminal state is derived from the outcome the pipeline
            # returned, never assumed.  The mapping below is a match with
            # assert_never, so a future third ScanOutcome member fails the type
            # gate at edit time rather than falling silently into an else.
            self._job_store.finish_job(
                job.id,
                job_state_for(result.outcome),
                result=JobResult(
                    outcome=result.outcome,
                    warning=result.warning,
                    pages_scanned=result.pages_scanned,
                    pages_removed=result.pages_removed,
                    pages_uploaded=result.pages_uploaded,
                ),
            )
        except Exception as exc:
            category = classify_error(exc)
            # No result argument: outcome, warning and all three page counts
            # stay NULL.  NULL means "never recorded"; 0 would claim a
            # measurement a job that never reached the scanner did not make.
            self._job_store.finish_job(
                job.id,
                JobState.ERROR,
                error=str(exc),
                error_category=category,
            )
            logger.error("Job %s failed (%s): %s", job.id, category.value.lower(), exc)
        finally:
            self._flip_coordinator = None
            self._current_job_id = None
            self._job_store.prune(
                self._settings.output.history_retention_days,
                self._settings.output.history_max_rows,
            )
