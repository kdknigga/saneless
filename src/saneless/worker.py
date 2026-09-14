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
from typing import TYPE_CHECKING, Final, Literal

from .auto_profiles import (
    generate_profiles,
    is_bare_default,
    resolve_config_path,
    write_profiles_to_config,
)
from .job import JobResult
from .pipeline import (
    FlipAnswerSlot,
    FlipCoordinator,
    PipelineEvent,
    PipelineRequest,
    run_pipeline,
)
from .vocabulary import (
    ACTIVE_STATES,
    RESTART_REASON,
    FlipOutcome,
    JobState,
    SubmitResult,
    classify_error,
    job_state_for,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .config import ProfileConfig, Settings
    from .job import Job, JobStore
    from .paperless import PaperlessClient
    from .scanner.base import ScannerBackend

__all__ = ["STOP_JOIN_SECONDS", "ScanWorker", "WorkerFlipCoordinator"]

logger = logging.getLogger(__name__)

# How long stop() waits for the worker thread before reporting it still alive
# (D-08).  Five seconds leaves room for uvicorn inside Docker's 10 s SIGKILL
# grace.  Deliberately not configurable.  Read at call time, so tests can
# shorten it.
STOP_JOIN_SECONDS: Final = 5.0

# The idle loop's queue.get() timeout.  stop() does not rely on it -- the queue
# shutdown wakes a blocked get() at once -- so it only sets how often an idle
# worker gets a turn for housekeeping.  Read at call time.
_IDLE_TICK_SECONDS: Final = 5.0

# How many unstarted jobs may wait behind the running one.  Not configurable:
# a submit beyond it is reported as QUEUE_FULL rather than queued (C-09).
_QUEUE_DEPTH: Final = 10


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
    detail, not part of the contract the pipeline waits on, and not part of
    the shared ``FlipAnswerSlot`` either.

    The claim itself -- first answer wins, and a waiter that wakes always finds
    an answer -- is ``FlipAnswerSlot``'s, shared with the CLI coordinator so a
    fix to it reaches both (IN-02).  Arming is a one-way latch checked before
    an offer: once set it is never cleared, so a signal that sees it set may
    offer, and one that sees it unset is dropped, never deferred.

    Args:
        job_id: The id of the job whose flip this coordinator answers.

    """

    def __init__(self, job_id: str) -> None:
        """Start unarmed and unanswered, bound to ``job_id``."""
        self._job_id = job_id
        self._slot = FlipAnswerSlot()
        self._armed = threading.Event()

    @property
    def job_id(self) -> str:
        """The id of the job whose flip this coordinator answers."""
        return self._job_id

    @property
    def armed(self) -> bool:
        """Whether the flip prompt exists, so a signal can claim the answer."""
        return self._armed.is_set()

    @property
    def answer(self) -> FlipOutcome | None:
        """The claimed answer, or ``None`` while the wait is unanswered."""
        return self._slot.answer

    def arm(self) -> None:
        """Open the flip prompt to signals.  Idempotent."""
        self._armed.set()

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
        # One path for both endings.  If a signal answered, settle finds that
        # answer already claimed and hands it back; if the wait expired,
        # TIMED_OUT is offered and wins unless a signal claimed first after
        # all.  Either way the result is the claimed answer, never a guess.
        self._slot.wait(timeout)
        return self._slot.settle(FlipOutcome.TIMED_OUT)

    def _signal(self, outcome: FlipOutcome) -> bool:
        """
        Offer an operator signal to the answer slot, if the prompt is armed.

        Unlike the timeout's ``settle``, this honours ``armed``: a signal before
        the prompt exists is dropped here, and one after an answer is dropped by
        the slot, leaving the answer untouched either way (CR-01, D-16).

        Args:
            outcome: The answer the operator is offering.

        Returns:
            Whether ``outcome`` became the answer.

        """
        if not self._armed.is_set():
            return False
        return self._slot.offer(outcome)


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
        self._queue: queue.Queue[Job] = queue.Queue(maxsize=_QUEUE_DEPTH)
        self._thread = threading.Thread(target=self._run, daemon=True)
        # Set once by stop(); read by the loop, submit() and the flip callback.
        self._stopping = threading.Event()
        # Guards every read and every rebind of self._settings.profiles (D-19).
        self._profiles_lock = threading.Lock()
        self._flip_coordinator: WorkerFlipCoordinator | None = None
        self._current_job_id: str | None = None
        self._auto_generated = False

    def start(self) -> None:
        """Start the worker thread."""
        self._thread.start()
        logger.info("ScanWorker started")

    def stop(self) -> bool:
        """
        Stop the worker without waiting on its queue, and report whether it stopped.

        Stopping sets a flag and shuts the queue down; it never enqueues
        anything, so a full queue cannot hold it (C-09).  Jobs still queued are
        abandoned on purpose, and so is a scan inside a SANE read: their rows
        stay active and the next startup's recovery fails them (D-07, D-13).
        An open flip wait is answered with Abort, so a job parked at the flip
        prompt lets the thread go at once.

        The join is bounded by ``STOP_JOIN_SECONDS`` (D-08).  When this returns
        ``False`` the thread is still running and may still write to the job
        store, so the caller must leave the store and the Paperless client open
        (D-09).  Calling it again, or on a worker never started, is safe.

        Returns:
            Whether the worker thread has stopped.

        """
        self._stopping.set()
        # immediate=True discards queued-but-unstarted jobs (D-07/D-13) and
        # wakes a get() blocked on the empty queue with queue.ShutDown.
        self._queue.shutdown(immediate=True)
        coordinator = self._flip_coordinator
        if coordinator is not None:
            # Pre-answering Abort is exactly the shutdown intent: a job waiting
            # at the prompt aborts now, and one still in pass A aborts the
            # moment it asks.  Arming first lets the signal land either way.
            coordinator.arm()
            coordinator.signal_abort()
        if self._thread.is_alive():
            self._thread.join(timeout=STOP_JOIN_SECONDS)
        stopped = not self._thread.is_alive()
        if stopped:
            logger.info("ScanWorker stopped")
        return stopped

    def submit(self, job: Job) -> SubmitResult:
        """
        Offer a job to the worker without ever blocking (C-09).

        The caller creates the job row first, so the worker never dequeues an
        id with no row, and records the rejection itself when this does not
        return ``ACCEPTED`` (D-05).

        Args:
            job: The Job to process.

        Returns:
            ``ACCEPTED`` when the job is queued, ``QUEUE_FULL`` when the queue
            has no room, or ``DOWN`` when the worker is not started, is
            stopping, or has stopped.

        """
        if not self._thread.is_alive() or self._stopping.is_set():
            return SubmitResult.DOWN
        # Two except clauses rather than one bracketless PEP 758 clause: the
        # two exceptions mean different things to the caller.
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            return SubmitResult.QUEUE_FULL
        except queue.ShutDown:
            return SubmitResult.DOWN
        logger.info("Job %s submitted to worker queue", job.id)
        return SubmitResult.ACCEPTED

    def continue_flip(self, job_id: str) -> bool:
        """
        Answer ``job_id``'s flip prompt with Continue, starting its pass B.

        Args:
            job_id: The job the operator is answering.

        Returns:
            Whether the answer was claimed.  ``False`` means it was dropped:
            ``job_id`` is not the job waiting at the flip prompt, that job has
            not reached the prompt yet, or the prompt was already answered.

        """
        return self._signal_flip(job_id, "continue")

    def abort_flip(self, job_id: str) -> bool:
        """
        Answer ``job_id``'s flip prompt with Abort, failing it before pass B.

        Args:
            job_id: The job the operator is answering.

        Returns:
            Whether the answer was claimed.  ``False`` means it was dropped:
            ``job_id`` is not the job waiting at the flip prompt, that job has
            not reached the prompt yet, or the prompt was already answered.

        """
        return self._signal_flip(job_id, "abort")

    def flip_answer(self, job_id: str) -> FlipOutcome | None:
        """
        Report the claimed flip answer for ``job_id``, if it is the live job.

        25-11's status rendering reads this to tell an answered prompt from an
        open one.

        Args:
            job_id: The job whose answer is wanted.

        Returns:
            The claimed answer when ``job_id`` is the job with the live flip
            coordinator, otherwise ``None`` -- including while it is unanswered.

        """
        coordinator = self._flip_coordinator
        if coordinator is None or coordinator.job_id != job_id:
            return None
        return coordinator.answer

    def _signal_flip(self, job_id: str, action: Literal["continue", "abort"]) -> bool:
        """
        Deliver one flip signal to ``job_id``'s coordinator and log the result.

        The coordinator is read once, and ``job_id`` is compared with that
        coordinator's own job id rather than with ``_current_job_id``: one
        snapshot, so the check and the signal cannot straddle a job boundary
        and deliver a click meant for one job to the next (CR-01).

        Args:
            job_id: The job the operator is answering.
            action: Which answer the operator gave.

        Returns:
            Whether the signal claimed the answer.

        """
        coordinator = self._flip_coordinator
        if coordinator is None or coordinator.job_id != job_id:
            logger.info(
                "Manual duplex: %s for job %s dropped: "
                "not the job waiting at the flip prompt",
                action,
                job_id,
            )
            return False
        claimed = (
            coordinator.signal_continue()
            if action == "continue"
            else coordinator.signal_abort()
        )
        if claimed:
            logger.info("Manual duplex: %s for job %s claimed", action, job_id)
        elif not coordinator.armed:
            logger.info(
                "Manual duplex: %s for job %s dropped: not yet at the flip prompt",
                action,
                job_id,
            )
        else:
            logger.info(
                "Manual duplex: %s for job %s dropped: already answered: %s",
                action,
                job_id,
                coordinator.answer,
            )
        return claimed

    @property
    def is_alive(self) -> bool:
        """Whether the worker thread is currently running."""
        return self._thread.is_alive()

    @property
    def current_job_id(self) -> str | None:
        """ID of the currently processing job, or None."""
        return self._current_job_id

    def profile_names(self) -> list[str]:
        """
        List the configured profile names, in configuration order.

        Request threads (the index dropdown) and the worker share one lock for
        every profile read (D-19); once the routes are ``def`` handlers on the
        threadpool (ROBU-05) that concurrency is real.

        Returns:
            A new list, so the caller can keep or change it freely.

        """
        with self._profiles_lock:
            return list(self._settings.profiles)

    def has_profile(self, name: str) -> bool:
        """
        Report whether a profile is configured, under the profile lock (D-19).

        ROBU-08's unknown-profile check on a request thread reads through here.

        Args:
            name: The profile name to look for.

        Returns:
            Whether ``name`` is a configured profile.

        """
        with self._profiles_lock:
            return name in self._settings.profiles

    def get_profile(self, name: str) -> ProfileConfig | None:
        """
        Look up a profile under the profile lock (D-19).

        The worker's own lookup for a job goes through here, sharing the lock
        with request threads.

        Args:
            name: The profile name to look up.

        Returns:
            The profile, or ``None`` when no profile has that name.

        """
        with self._profiles_lock:
            return self._settings.profiles.get(name)

    def _set_profiles(self, profiles: Mapping[str, ProfileConfig]) -> None:
        """
        Replace the configured profiles with a new dict, under the lock (D-19).

        The mapping is rebound, never mutated in place.  A reader that took the
        old dict without the lock -- ``run_pipeline`` on the worker thread, for
        example -- keeps a consistent view of it, and no locked reader can
        observe a dict part-way through an update.

        Args:
            profiles: The complete new set of profiles.

        """
        replacement = dict(profiles)
        with self._profiles_lock:
            self._settings.profiles = replacement

    def _run(self) -> None:
        """
        Worker loop: process jobs until stop() sets the stop flag.

        There is no sentinel.  An idle ``get`` wakes every
        ``_IDLE_TICK_SECONDS``, and stop()'s queue shutdown wakes it at once
        with ``queue.ShutDown``.
        """
        while not self._stopping.is_set():
            try:
                job = self._queue.get(timeout=_IDLE_TICK_SECONDS)
            except queue.Empty:
                continue
            except queue.ShutDown:
                break
            if self._stopping.is_set():
                # Dequeued just as stopping began: not started.  Its row stays
                # PENDING and the next startup's recovery fails it (D-07).
                break
            self._process_job(job)

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
            # Update in-memory settings by rebinding a merged dict under the
            # profile lock, never by mutating the live one (D-19).
            with self._profiles_lock:
                current = dict(self._settings.profiles)
            self._set_profiles({**current, **profiles})
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
        profile = self.get_profile(job.profile)
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
                if self._stopping.is_set():
                    # Research Pitfall 5: stop() ran while this job was still
                    # in pass A, found no prompt to answer, and returned to its
                    # join.  Abort now, or the wait would hold the thread for
                    # flip_timeout_seconds after shutdown began.
                    coordinator.signal_abort()
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
            # No result argument: outcome, warning and all three page counts
            # stay NULL.  NULL means "never recorded"; 0 would claim a
            # measurement a job that never reached the scanner did not make.
            if self._stopping.is_set():
                # Shutdown ended this job, not the operator: stop() answered
                # its flip wait with Abort.  This is the worker thread's own
                # final write, so D-07's "no shutdown-time write" -- which is
                # about the lifespan writing over a running thread -- holds.
                self._job_store.finish_job(
                    job.id,
                    JobState.ERROR,
                    error=RESTART_REASON,
                    error_category=None,
                )
                logger.info("Job %s ended by shutdown: %s", job.id, exc)
            else:
                category = classify_error(exc)
                self._job_store.finish_job(
                    job.id,
                    JobState.ERROR,
                    error=str(exc),
                    error_category=category,
                )
                logger.error(
                    "Job %s failed (%s): %s", job.id, category.value.lower(), exc
                )
        finally:
            self._flip_coordinator = None
            self._current_job_id = None
            self._job_store.prune(
                self._settings.output.history_retention_days,
                self._settings.output.history_max_rows,
            )
