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
import time
from typing import TYPE_CHECKING, Final, Literal

from .auto_profiles import (
    generate_profiles,
    is_bare_default,
    write_profiles_to_config,
)
from .config import config_search_paths
from .exceptions import ConfigError
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
    WorkerHealth,
    classify_error,
    job_state_for,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .config import ProfileConfig, Settings
    from .job import Job, JobStore
    from .paperless import PaperlessClient
    from .scanner.base import ScannerBackend
    from .vocabulary import ErrorCategory

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

# How many loop-level failures in a row -- the loop's own job store writes, or
# the idle prune, raising -- make the worker degraded (D-10).  A pipeline
# failure is a job failure and never counts.  Three rides out one transient
# error without calling the store broken.  Not configurable.
_DEGRADED_AFTER: Final = 3

# How often an idle worker prunes job history (D-13).  Prune left the per-job
# path so its failure can never fail a job; hourly keeps a long-running
# appliance inside history_max_rows.  The startup prune is the lifespan's.  Not
# configurable.  Read at call time, so tests can shorten it.
_PRUNE_INTERVAL_SECONDS: Final = 3600.0


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
        # Loop-level failures in a row.  Touched only by the worker thread.
        self._consecutive_loop_failures = 0
        # When the idle loop last pruned.  Starts now: the startup prune is the
        # lifespan's (26-09), so the first idle prune is an interval away.
        self._last_prune = time.monotonic()
        # Jobs whose failure could not be written even by the loop guard, by
        # id, with the error text and category the guard tried to record.
        # Every idle tick retries them (CR-01).  Touched only by the worker
        # thread.
        self._unrecorded_failures: dict[str, tuple[str, ErrorCategory | None]] = {}
        # Set and cleared by the worker thread (and by mark_recovery_pending,
        # before the thread exists); read by request threads through health and
        # submit(), so an Event rather than a bare bool (D-10, D-11).
        self._degraded = threading.Event()
        # Whether the first successful probe must also fail the rows a crashed
        # process left active, because startup recovery could not.
        self._restart_recovery_pending = False

    def start(self) -> None:
        """Start the worker thread."""
        self._thread.start()
        logger.info("ScanWorker started")

    def mark_recovery_pending(self) -> None:
        """
        Start degraded, owing startup's crash recovery to the first good probe.

        The lifespan calls this before ``start()`` when ``fail_active_jobs``
        raised at startup (research Open Question 1).  The app still starts,
        ``/health`` answers a truthful 503 and scans are rejected, instead of
        the service refusing to come up over a store that may recover.  The
        first successful idle probe then fails the rows the previous process
        left active with ``RESTART_REASON`` and clears degraded (D-12, D-13).
        """
        self._restart_recovery_pending = True
        self._degraded.set()

    @property
    def health(self) -> WorkerHealth:
        """
        The worker's health, as ``/health`` reports it (26-10).

        ``DOWN`` when the thread is not running -- not started, or stopped --
        which ``/health`` reports as "worker thread is down".  ``DEGRADED``
        when the job store has failed the loop ``_DEGRADED_AFTER`` times in a
        row and no probe has succeeded since, reported as "job store failing".
        Otherwise ``HEALTHY``.
        """
        if not self._thread.is_alive():
            health = WorkerHealth.DOWN
        elif self._degraded.is_set():
            health = WorkerHealth.DEGRADED
        else:
            health = WorkerHealth.HEALTHY
        return health

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
            has no room, ``DEGRADED`` when the job store is failing, or
            ``DOWN`` when the worker is not started, is stopping, or has
            stopped.

        """
        if not self._thread.is_alive() or self._stopping.is_set():
            return SubmitResult.DOWN
        if self._degraded.is_set():
            # D-11: nobody is asked to feed paper into a job whose outcome
            # could not be recorded.
            return SubmitResult.DEGRADED
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

    def _generate_startup_profiles(self) -> None:
        """
        Generate profiles from the scanner once, as the thread's first act.

        D-14: this runs on the worker thread before it takes any job, so the
        server is already answering requests while it works.  A page loaded
        meanwhile may list only ``default`` until it is reloaded; a job
        submitted meanwhile waits in the queue and then runs against the
        generated set.

        D-15: it is tried once per start.  A scanner failure is logged with the
        real exception class and the bare default is kept; the cause is never
        guessed.  Restarting saneless, or ``saneless auto-profiles``, retries.

        D-16..D-18: the profiles are written only to ``settings.config_path``,
        the file these settings were loaded from.  With no loaded file they are
        used in memory for this run (INFO); when the loaded file cannot be
        written they are used in memory too (WARNING).  Nothing is ever written
        to a path worked out afresh here.

        D-19: the swap happens under the profile lock, after re-checking that
        the set is still the bare default.
        """
        with self._profiles_lock:
            bare = is_bare_default(self._settings)
        if not bare:
            return
        profiles = self._read_generated_profiles()
        if profiles is None:
            return
        self._persist_generated_profiles(profiles)
        with self._profiles_lock:
            # Re-checked under the lock: only the exact bare default is ever
            # replaced.  REPLACE rather than merge -- the one entry being
            # replaced is the untouched default, and a generated set always
            # carries its own ``default`` (DPLX-07).  Rebound, never mutated,
            # so a reader holding the old dict keeps a consistent view.
            if is_bare_default(self._settings):
                self._settings.profiles = dict(profiles)

    def _read_generated_profiles(self) -> dict[str, ProfileConfig] | None:
        """
        Ask the scanner for its capabilities and build profiles from them.

        Returns:
            The generated profiles, or ``None`` when no scanner was found or the
            scanner could not be read -- both logged, with the bare default kept.

        """
        try:
            devices = self._scanner.get_devices()
            if not devices:
                logger.warning("Auto-profiles: no scanners found, using bare default")
                return None
            device_id = self._settings.scanner.device or devices[0].name
            caps = self._scanner.get_capabilities(device_id)
            return generate_profiles(caps)
        except Exception as exc:
            # D-15: the exception class is named, never interpreted.  The old
            # message blamed the network for every failure, a parse error
            # included, and sent operators after faults that were not there.
            logger.warning(
                "Auto-profiles: could not read scanner capabilities (%s); keeping "
                "the bare default profile. Restart saneless or run "
                "'saneless auto-profiles' to retry",
                type(exc).__name__,
                exc_info=True,
            )
            return None

    def _persist_generated_profiles(self, profiles: dict[str, ProfileConfig]) -> None:
        """
        Write generated profiles to the loaded config file, if there is one.

        Failure to write is logged, never raised: the profiles are still used
        in memory for this run (D-17, D-18).

        Args:
            profiles: The generated profiles.

        """
        config_path = self._settings.config_path
        if config_path is None:
            logger.info(
                "Auto-profiles: no config file was loaded, so the generated "
                "profiles are used for this run only and were not written; pass "
                "--config or create one of %s to keep them",
                ", ".join(str(path) for path in config_search_paths()),
            )
            return
        try:
            written = write_profiles_to_config(config_path, profiles)
        except (OSError, ConfigError) as exc:
            # The OSError text goes to the server log for the operator, never
            # into an HTTP response.
            logger.warning(
                "Auto-profiles: could not write %s (%s: %s); the generated "
                "profiles are used for this run only and will not survive a "
                "restart",
                config_path,
                type(exc).__name__,
                exc,
            )
            return
        logger.info(
            "Auto-profiles: wrote %d profile(s) to %s: %s",
            len(written),
            config_path,
            ", ".join(written) or "(none new)",
        )

    def _run(self) -> None:
        """
        Worker loop: process jobs until stop() sets the stop flag.

        There is no sentinel.  An idle ``get`` wakes every
        ``_IDLE_TICK_SECONDS`` for housekeeping, and stop()'s queue shutdown
        wakes it at once with ``queue.ShutDown``.

        Nothing ends the loop but stopping (ROBU-01, C-09).  A pipeline failure
        is recorded by ``_process_job`` itself; whatever still escapes it is a
        failure of the loop's own job store writes, which is logged, counted
        towards degraded (D-10), and answered with one best-effort ERROR write
        so the row does not sit active until restart (research Pitfall 6).  If
        that write fails too, idle ticks retry it until it lands (CR-01).

        Before any job, the thread generates profiles (D-14).  A job submitted
        meanwhile waits in the queue and then runs against the generated set.
        """
        try:
            self._generate_startup_profiles()
        except Exception:
            # _generate_startup_profiles catches what it expects itself; this
            # is the backstop that keeps a surprise from ending the thread
            # before it has taken a single job (ROBU-01).
            logger.exception("Auto-profiles: startup generation failed")
        while not self._stopping.is_set():
            try:
                job = self._queue.get(timeout=_IDLE_TICK_SECONDS)
            except queue.Empty:
                self._idle_housekeeping()
                continue
            except queue.ShutDown:
                break
            if self._stopping.is_set():
                # Dequeued just as stopping began: not started.  Its row stays
                # PENDING and the next startup's recovery fails it (D-07).
                break
            try:
                self._process_job(job)
            except Exception as exc:
                logger.exception("Worker loop failed while handling job %s", job.id)
                self._record_loop_failure()
                self._best_effort_fail(job, exc)
            else:
                # A job whose store writes all landed breaks the run.  It does
                # not clear degraded: only a successful idle probe does.
                self._consecutive_loop_failures = 0

    def _failure_record(self, exc: Exception) -> tuple[str, ErrorCategory | None]:
        """
        Choose the error text and category a failed job is recorded with.

        Args:
            exc: What ended the job.

        Returns:
            ``RESTART_REASON`` with no category while stopping -- shutdown ended
            the job, not the operator -- otherwise the exception's own text and
            its classified category.

        """
        if self._stopping.is_set():
            return RESTART_REASON, None
        return str(exc), classify_error(exc)

    def _record_loop_failure(self) -> None:
        """Count one loop-level failure, degrading at ``_DEGRADED_AFTER`` (D-10)."""
        self._consecutive_loop_failures += 1
        if (
            self._consecutive_loop_failures >= _DEGRADED_AFTER
            and not self._degraded.is_set()
        ):
            self._degraded.set()
            logger.warning(
                "Scan worker degraded after %d consecutive job store failures; "
                "rejecting scans until the store recovers",
                self._consecutive_loop_failures,
            )

    def _best_effort_fail(self, job: Job, exc: Exception) -> None:
        """
        Try once to record ``job`` as failed after a loop-level failure.

        The store just raised, so this may raise too; that is only logged.
        The failure is remembered instead, and the next idle tick retries the
        write -- through the recovery path while degraded -- until the store
        accepts it (research Pitfall 6, CR-01).

        Args:
            job: The job the loop was handling.
            exc: The loop-level failure.

        """
        error, category = self._failure_record(exc)
        try:
            self._job_store.finish_job(
                job.id, JobState.ERROR, error=error, error_category=category
            )
        except Exception:
            logger.warning(
                "Could not record job %s as failed; it stays active until the "
                "job store recovers",
                job.id,
                exc_info=True,
            )
            self._unrecorded_failures[job.id] = (error, category)

    def _idle_housekeeping(self) -> None:
        """
        Use an idle tick: retry owed writes (probing while degraded), then prune.

        Owed writes come first and are retried on every tick, degraded or not,
        so a row the guard could not end reaches ERROR as soon as the store
        accepts writes, without a restart or a scan (CR-01).  The probe and the
        degraded clear stay the degraded worker's business (D-12).  A failed
        retry is only logged: the guard already counted the failure that
        created the debt (D-10).  A prune failure is a loop-level failure
        (D-10), and it can never fail a job: no job is running on an idle tick
        (D-13).
        """
        if self._degraded.is_set():
            self._try_recover()
        else:
            try:
                self._flush_unrecorded_failures()
            except Exception:
                logger.debug(
                    "Owed job store writes failed; retrying on the next idle tick",
                    exc_info=True,
                )
        if time.monotonic() - self._last_prune < _PRUNE_INTERVAL_SECONDS:
            return
        self._last_prune = time.monotonic()
        try:
            self._job_store.prune(
                self._settings.output.history_retention_days,
                self._settings.output.history_max_rows,
            )
        except Exception:
            logger.exception("Idle history prune failed")
            self._record_loop_failure()

    def _flush_unrecorded_failures(self) -> None:
        """
        Write the ERROR rows the loop guard could not, dropping each once written.

        This runs only on an Empty tick -- from ``_idle_housekeeping`` or
        ``_try_recover`` -- so no job is current, and an owed id belongs to a
        job the loop already abandoned: none can be the job in flight.  With
        nothing owed it returns without touching the store.

        Raises:
            Exception: Whatever the store raises.  Rows already written stay
                written and are no longer owed; the rest wait for the next
                tick.

        """
        for job_id, (error, category) in list(self._unrecorded_failures.items()):
            self._job_store.finish_job(
                job_id, JobState.ERROR, error=error, error_category=category
            )
            del self._unrecorded_failures[job_id]
            logger.info(
                "Recorded job %s as failed now that the job store accepts writes",
                job_id,
            )

    def _try_recover(self) -> None:
        """
        Probe the job store and, if it reads and writes again, clear degraded.

        Before clearing, recovery ends the rows the loop could not (research
        Pitfall 6).  This runs on an Empty tick, so the queue is empty, no job
        is current, and every submit was rejected while degraded: an active
        row is an orphan.  A submit racing the clear below is accepted, and its
        row is new, so it is neither of the rows written here.

        Each row gets a text that already exists: the failure the guard tried
        to write, or ``RESTART_REASON`` for rows a failed startup recovery left
        behind.  Any raise leaves the worker degraded to try again next tick;
        it is not counted again, and whatever was written stays written.
        """
        try:
            self._job_store.probe()
        except Exception:
            logger.debug("Job store probe failed; still degraded", exc_info=True)
            return
        try:
            # The guard's own failures first: once ERROR they are no longer
            # active, so the restart recovery below cannot give them its text.
            self._flush_unrecorded_failures()
            if self._restart_recovery_pending:
                self._job_store.fail_active_jobs(RESTART_REASON)
                self._restart_recovery_pending = False
        except Exception:
            logger.debug(
                "Job store recovery write failed; still degraded", exc_info=True
            )
            return
        self._consecutive_loop_failures = 0
        self._degraded.clear()
        logger.info("Scan worker recovered: the job store accepted a write")

    def _process_job(self, job: Job) -> None:
        """
        Execute a single scan job, clearing the live-job state however it ends.

        Args:
            job: The Job to process.

        """
        self._current_job_id = job.id
        try:
            self._scan_job(job)
        finally:
            # Cleared on every ending, a loop-level failure included, so a
            # raise from the SCANNING write cannot leave a stale current job
            # or flip coordinator behind.  No prune here any more: it runs on
            # the idle tick, where its failure cannot fail a job (D-13).
            self._flip_coordinator = None
            self._current_job_id = None

    def _scan_job(self, job: Job) -> None:
        """
        Run one job through the pipeline and record how it ended.

        A pipeline failure is a job failure: it is recorded as ERROR here and
        this returns normally.  That includes a store write inside a pipeline
        callback, which reaches here through ``run_pipeline``.  The loop's own
        store writes -- SCANNING before the pipeline, and the terminal write of
        either outcome -- sit outside any catch, so their failure escapes to
        ``_run`` as a loop-level failure (D-10).

        Args:
            job: The Job to process.

        """
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

        request = PipelineRequest(
            profile_name=job.profile,
            title=job.title,
            # The assembled PDF is named from this id, which is what makes two
            # same-second scans of the same title two files rather than one
            # overwriting the other.
            job_id=job.id,
            tags=job.tags or None,
            correspondent=job.correspondent,
            status_callback=_status_cb,
            thumbnail_callback=_thumbnail_cb,
            flip_coordinator=coordinator,
        )
        try:
            result = run_pipeline(
                self._scanner,
                self._paperless,
                self._settings,
                request,
            )
        except Exception as exc:
            error, category = self._failure_record(exc)
            # No result argument: outcome, warning and all three page counts
            # stay NULL.  NULL means "never recorded"; 0 would claim a
            # measurement a job that never reached the scanner did not make.
            # Outside any further catch on purpose: if this write raises, the
            # job failure could not be recorded, and that is the loop's
            # failure (D-10).  While stopping this is the worker thread's own
            # final write, so D-07's "no shutdown-time write" -- which is about
            # the lifespan writing over a running thread -- holds.
            self._job_store.finish_job(
                job.id,
                JobState.ERROR,
                error=error,
                error_category=category,
            )
            if category is None:
                # Only a shutdown records no category: stop() answered the
                # flip wait with Abort, so the operator did not end this job.
                logger.info("Job %s ended by shutdown: %s", job.id, exc)
            else:
                logger.error(
                    "Job %s failed (%s): %s", job.id, category.value.lower(), exc
                )
            return
        # The terminal state is derived from the outcome the pipeline returned,
        # never assumed.  The mapping below is a match with assert_never, so a
        # future third ScanOutcome member fails the type gate at edit time
        # rather than falling silently into an else.
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
