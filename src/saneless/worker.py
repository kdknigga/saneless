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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from .auto_profiles import (
    generate_profiles,
    is_bare_default,
    write_profiles_to_config,
)
from .config import config_search_paths
from .exceptions import ConfigError, ScanCancelledError
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
    ErrorCategory,
    FlipOutcome,
    JobState,
    SubmitResult,
    WorkerHealth,
    classify_error,
    job_state_for,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from .auto_profiles import ProfileWriteResult
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

# How many loop-level failures in a row -- the loop's own job store writes, or
# the idle prune, raising -- make the worker degraded (D-10).  A pipeline
# failure is a job failure and never counts.  Three rides out one transient
# error without calling the store broken.  Not configurable.
_DEGRADED_AFTER: Final = 3

# How many idle ticks in a row the owed-write retry may fail before the worker
# is degraded (WR-10).  Kept apart from _DEGRADED_AFTER's loop count: the guard
# already counted the failure behind a guard debt, and a request-side
# owe_rejection debt was never counted, so a streak of failed retries is its
# own evidence that the store is not healing.  Three rides out a fault that
# heals within a tick or two, and at the 5 s idle tick bounds a stuck row's
# silent window to about 15 s.  Not configurable.  Read at call time, so tests
# can change it.
_OWED_RETRY_DEGRADED_AFTER: Final = 3

# How often an idle worker prunes job history (D-13).  Prune left the per-job
# path so its failure can never fail a job; hourly keeps a long-running
# appliance inside history_max_rows.  The startup prune is the lifespan's.  Not
# configurable.  Read at call time, so tests can shorten it.
_PRUNE_INTERVAL_SECONDS: Final = 3600.0


@dataclass(frozen=True, slots=True)
class _OwedWrite:
    """
    One terminal job-row write the worker still owes, exactly as it was meant.

    The owed value is the whole ``finish_job`` call, not just an error text:
    a success-path write that failed after Paperless accepted the document
    must be replayed as DONE or FALLBACK with its result, never turned into an
    ERROR that invites a duplicate scan (WR-02).  Frozen, so the flush's
    delete-if-unchanged check compares values.

    Attributes:
        state: The terminal state to record.
        result: What the scan produced, for a DONE or FALLBACK write.
        error: The job-row error text, for an ERROR write, or the cancel's
            message, for a CANCELLED write.
        category: The error's category, for an ERROR write.  A CANCELLED
            write, like a shutdown's ERROR, has none.

    """

    state: JobState
    result: JobResult | None = None
    error: str | None = None
    category: ErrorCategory | None = None


def _profiles_after_persist(
    loaded: Mapping[str, ProfileConfig],
    generated: Mapping[str, ProfileConfig],
    result: ProfileWriteResult | None,
) -> dict[str, ProfileConfig]:
    """
    Choose the profiles to use in memory, matching what a restart will load.

    ``write_profiles_to_config`` never touches a same-name profile without
    ``auto_generated = true`` (D-01), and the worker never forces, so a flagged
    one is skipped too.  A file that spells out a bare ``[profiles.default]``
    therefore keeps it.  Swapping the generated ``default`` into memory anyway
    would give this run one ``default`` and every later run another (WR-03).

    Args:
        loaded: The bare default profile set the settings were loaded with.
        generated: The profiles generated from the scanner.
        result: What the write did to the config file, or ``None`` when
            nothing was persisted and the generated set is for this run only.

    Returns:
        A new dict: the generated set when nothing was persisted, otherwise
        each generated profile that was persisted, with the loaded profile
        kept for every name the write did not persist.

    """
    if result is None:
        return dict(generated)
    persisted = result.persisted
    profiles: dict[str, ProfileConfig] = {}
    for name, profile in generated.items():
        if name in persisted:
            profiles[name] = profile
        elif name in loaded:
            profiles[name] = loaded[name]
    for name, profile in loaded.items():
        profiles.setdefault(name, profile)
    return profiles


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
        # Held across a shutdown's claim and its marker, so the worker thread,
        # woken by that claim, cannot read the marker before it is set (WR-06).
        self._shutdown_lock = threading.Lock()
        self._aborted_by_shutdown = False

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

    def abort_for_shutdown(self) -> bool:
        """
        Answer the wait with Abort because the server is stopping.

        Arms first, so the answer lands whether the job is at the prompt or
        still in pass A.  Only a claim made here marks the job as ended by
        shutdown: an operator's Abort, a Continue, or a timeout that claimed
        first keeps its own meaning (WR-06, D-15).

        Returns:
            Whether this shutdown claimed the answer.

        """
        self.arm()
        with self._shutdown_lock:
            claimed = self._slot.offer(FlipOutcome.ABORTED)
            if claimed:
                self._aborted_by_shutdown = True
        return claimed

    @property
    def aborted_by_shutdown(self) -> bool:
        """Whether :meth:`abort_for_shutdown` claimed this coordinator's answer."""
        with self._shutdown_lock:
            return self._aborted_by_shutdown

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
        # Idle ticks in a row whose owed-write retry raised (WR-10), with no
        # landed retry, recovery or cleanly recorded job in between (IN-09).
        # Touched only by the worker thread.
        self._failed_flush_ticks = 0
        # When the idle loop last pruned.  Starts now: the startup prune is the
        # lifespan's (26-09), so the first idle prune is an interval away.
        self._last_prune = time.monotonic()
        # Terminal job-row writes owed by id, each kept as the write it was
        # meant to be: the loop's own terminal writes that failed, kept with
        # their real outcome (WR-02), failures even the loop guard could not
        # write, and rejected submits whose REJECTED write failed in the
        # request (WR-01).  Every idle tick
        # retries them (CR-01), and a streak of failed retries degrades the
        # worker (WR-10).  Shared by the worker thread (the guard and the
        # idle flush) and request threads (owe_rejection), so every read and
        # write goes through _unrecorded_lock.
        self._unrecorded_lock = threading.Lock()
        self._unrecorded_failures: dict[str, _OwedWrite] = {}
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

    def owe_rejection(self, job_id: str, error: str) -> None:
        """
        Take over a refused submit's REJECTED write that the request could not make.

        The scan route calls this when finishing a refused submit's row as
        ``ERROR`` with ``ErrorCategory.REJECTED`` raised, so the row is not
        left PENDING with no marker and the Scan button disabled (WR-01).  The
        id was refused by :meth:`submit` and never enqueued, so no job can be
        running under it.

        The worker writes it on its next idle tick, or through the recovery
        path while degraded, sharing the flush of the loop guard's owed
        failures (CR-01, D-12).  If the worker thread is not running (DOWN) no
        tick comes: ``/health`` reports 503 meanwhile, and the next startup's
        ``fail_active_jobs(RESTART_REASON)`` ends the row (D-13).

        Args:
            job_id: The refused submit's job row.
            error: The job-row error text for the rejection.

        """
        owed = _OwedWrite(JobState.ERROR, error=error, category=ErrorCategory.REJECTED)
        with self._unrecorded_lock:
            self._unrecorded_failures[job_id] = owed

    def owed_rejection_ids(self) -> frozenset[str]:
        """
        List the refused submits whose REJECTED write the worker still owes.

        The status area must not show these as a live job: until the worker
        writes one, its row is PENDING with no REJECTED marker (IN-08, D-06).
        Failures the loop guard could not write are left out on purpose, because
        those jobs ran and D-17 still reports them as the job that just ended.
        ``classify_error`` never yields REJECTED, so a REJECTED entry can only
        come from :meth:`owe_rejection`.

        An id leaves the set only after its row is written ERROR/REJECTED,
        which ``JobStore.latest_run_job`` already skips, so no poll can see it
        as live in between.

        Returns:
            An immutable snapshot of the owed rejection ids, taken under the
            lock the worker thread and request threads share.

        """
        with self._unrecorded_lock:
            return frozenset(
                job_id
                for job_id, owed in self._unrecorded_failures.items()
                if owed.category is ErrorCategory.REJECTED
            )

    @property
    def health(self) -> WorkerHealth:
        """
        The worker's health, as ``/health`` reports it (26-10).

        ``DOWN`` when the thread is not running -- not started, or stopped --
        which ``/health`` reports as "worker thread is down".  ``DEGRADED``
        when the job store has failed the loop ``_DEGRADED_AFTER`` times in a
        row, or the owed-write retry has failed ``_OWED_RETRY_DEGRADED_AFTER``
        idle ticks in a row, and no recovery has succeeded since, reported as
        "job store failing".  Otherwise ``HEALTHY``.
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
            # moment it asks.  Only this claim records the restart reason.
            coordinator.abort_for_shutdown()
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

    def _set_profiles(
        self,
        profiles: Mapping[str, ProfileConfig],
        *,
        only_if: Callable[[Settings], bool] | None = None,
    ) -> bool:
        """
        Replace the configured profiles with a new dict, under the lock (D-19).

        The mapping is rebound, never mutated in place.  A reader that took the
        old dict without the lock -- ``run_pipeline`` on the worker thread, for
        example -- keeps a consistent view of it, and no locked reader can
        observe a dict part-way through an update.

        This is the one place the profiles are rebound: startup generation
        swaps through it too, with its bare-default re-check as ``only_if``
        (IN-01).

        Args:
            profiles: The complete new set of profiles.
            only_if: A check on the current settings, run under the same lock
                as the swap; when it returns ``False`` nothing is replaced.

        Returns:
            Whether the profiles were replaced.

        """
        replacement = dict(profiles)
        with self._profiles_lock:
            if only_if is not None and not only_if(self._settings):
                return False
            self._settings.profiles = replacement
        return True

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

        WR-03: memory matches what the file will load after a restart.  When
        the file already defines ``default``, the write keeps it, so the loaded
        ``default`` is kept in memory too rather than the generated one.
        """
        with self._profiles_lock:
            bare = is_bare_default(self._settings)
            loaded = self._settings.profiles
        if not bare:
            return
        profiles = self._read_generated_profiles()
        if profiles is None:
            return
        result = self._persist_generated_profiles(profiles)
        # Re-checked under the lock: only the exact bare default is ever
        # replaced, so a set customised meanwhile is left alone.
        self._set_profiles(
            _profiles_after_persist(loaded, profiles, result),
            only_if=is_bare_default,
        )

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

    def _persist_generated_profiles(
        self, profiles: dict[str, ProfileConfig]
    ) -> ProfileWriteResult | None:
        """
        Write generated profiles to the loaded config file, if there is one.

        Failure to write is logged, never raised, whatever it raises: the
        profiles are still used in memory for this run (D-17, D-18, WR-04).
        Success is logged in the same group vocabulary the CLI prints (D-04).

        Args:
            profiles: The generated profiles.

        Returns:
            What the write did to the file, or ``None`` when nothing was
            persisted because no file was loaded or the write failed.

        """
        config_path = self._settings.config_path
        if config_path is None:
            logger.info(
                "Auto-profiles: no config file was loaded, so the generated "
                "profiles are used for this run only and were not written; pass "
                "--config or create one of %s to keep them",
                ", ".join(str(path) for path in config_search_paths()),
            )
            return None
        try:
            result = write_profiles_to_config(config_path, profiles)
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
            return None
        except Exception as exc:
            # WR-04: anything else -- a tomlkit container error, say; a parse
            # or UTF-8 failure is already a ConfigError (D-05, D-12) -- must
            # not throw away the generated profiles either (D-18).  Unexpected, so the traceback
            # is logged too; the exception class is named, never interpreted.
            logger.warning(
                "Auto-profiles: could not write %s (%s); the generated "
                "profiles are used for this run only and will not survive a "
                "restart",
                config_path,
                type(exc).__name__,
                exc_info=True,
            )
            return None
        logger.info(
            "Auto-profiles: %s: %s",
            result.path,
            "; ".join(result.describe()) or "no changes",
        )
        return result

    def _run(self) -> None:
        """
        Worker loop: process jobs until stop() sets the stop flag.

        There is no sentinel.  An idle ``get`` wakes every
        ``_IDLE_TICK_SECONDS`` for housekeeping, and stop()'s queue shutdown
        wakes it at once with ``queue.ShutDown``.

        Nothing ends the loop but stopping (ROBU-01, C-09).  A pipeline failure
        is recorded by ``_process_job`` itself; whatever still escapes it is a
        failure of the loop's own job store writes, which is logged, counted
        towards degraded (D-10), and answered with one best-effort terminal
        write so the row does not sit active until restart (research Pitfall
        6): the terminal write the loop was making, if that is what failed
        (WR-02), otherwise an ERROR.  If that write fails too, idle ticks retry
        it until it lands (CR-01).

        Before any job, the thread generates profiles (D-14).  A job submitted
        meanwhile waits in the queue and then runs against the generated set.
        After the loop, it tries once more to write whatever is still owed
        (IN-06).
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
                # A job whose store writes all landed breaks the run, and the
                # owed-write streak too: idle ticks either side of it are not
                # "in a row" against a store that just accepted writes
                # (IN-09).  It does not clear degraded: only a successful idle
                # probe does.
                self._consecutive_loop_failures = 0
                self._failed_flush_ticks = 0
        self._flush_before_exit()

    def _flush_before_exit(self) -> None:
        """
        Try once to write every owed row before the thread exits (IN-06).

        Owed writes live only in memory.  Left unwritten, an owed rejection
        comes back after a restart as a PENDING row that the next startup's
        recovery ends as "server restarted", for a scan that never started,
        and until then it shows as the live job.  This is the worker thread's
        own write, so the lifespan's rule against writing over a running
        thread holds (D-07, D-09).  A failure is only logged: the next
        startup's recovery still ends the rows (D-13).
        """
        try:
            self._flush_unrecorded_failures()
        except Exception:
            logger.warning(
                "Could not write owed job records before stopping; the next "
                "startup's recovery ends those rows",
                exc_info=True,
            )

    @staticmethod
    def _failure_record(
        exc: Exception, coordinator: WorkerFlipCoordinator | None = None
    ) -> tuple[str, ErrorCategory | None]:
        """
        Choose the error text and category a failed job is recorded with.

        Stopping alone is not a cause: a Paperless error, a jam or an
        operator's Abort that happens inside the shutdown join window keeps
        its own text and category.  Only a flip answer the shutdown itself
        claimed is recorded as a restart (WR-06, D-15).

        ``_best_effort_fail`` records a loop-level failure through this.  A
        pipeline exception in ``_scan_job`` does not: that path tells its three
        endings -- shutdown, cancel and failure -- apart itself, because a
        cancel is written CANCELLED rather than ERROR (D-01).

        Args:
            exc: What ended the job.
            coordinator: The job's flip coordinator, if it has one.

        Returns:
            ``RESTART_REASON`` with no category when shutdown claimed the flip
            answer, otherwise the exception's own text and its classified
            category.

        """
        if coordinator is not None and coordinator.aborted_by_shutdown:
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
        Try once to record how ``job`` ended after a loop-level failure.

        When the failed write was the loop's own terminal write, it is already
        owed with the outcome it meant to record, and that write is the one
        retried: a DONE or FALLBACK whose write failed after the upload stays
        DONE or FALLBACK, never an ERROR that invites a duplicate scan (WR-02).
        Otherwise the job is recorded as failed with the loop failure's text.

        The store just raised, so this may raise too; that is only logged.
        The write is remembered instead, and the next idle tick retries it --
        through the recovery path while degraded -- until the store accepts it
        (research Pitfall 6, CR-01).

        Args:
            job: The job the loop was handling.
            exc: The loop-level failure.

        """
        with self._unrecorded_lock:
            owed = self._unrecorded_failures.get(job.id)
        if owed is None:
            error, category = self._failure_record(exc)
            owed = _OwedWrite(JobState.ERROR, error=error, category=category)
        try:
            self._write_owed(job.id, owed)
        except Exception:
            logger.warning(
                "Could not record job %s as %s; it stays active until the "
                "job store recovers",
                job.id,
                owed.state.value.lower(),
                exc_info=True,
            )
            with self._unrecorded_lock:
                self._unrecorded_failures[job.id] = owed
        else:
            self._forget_owed(job.id, owed)

    def _write_owed(self, job_id: str, owed: _OwedWrite) -> None:
        """
        Make one owed terminal write, exactly as it was meant.

        Args:
            job_id: The job row to write.
            owed: The terminal write to make.

        """
        self._job_store.finish_job(
            job_id,
            owed.state,
            result=owed.result,
            error=owed.error,
            error_category=owed.category,
        )

    def _forget_owed(self, job_id: str, owed: _OwedWrite) -> None:
        """
        Drop ``job_id``'s owed write once written, unless it was re-owed since.

        Args:
            job_id: The job row just written.
            owed: The write that landed.

        """
        with self._unrecorded_lock:
            if self._unrecorded_failures.get(job_id) == owed:
                del self._unrecorded_failures[job_id]

    def _finish_or_owe(self, job_id: str, owed: _OwedWrite) -> None:
        """
        Make one of the loop's own terminal writes, owing it if the store raises.

        The write is owed before the exception propagates, so the guard in
        ``_run`` retries this write -- not an ERROR built from the store's
        exception -- and so does every idle tick after it (WR-02).

        Args:
            job_id: The job row to write.
            owed: The terminal write to make.

        Raises:
            Exception: Whatever the store raises; it stays a loop-level
                failure (D-10).

        """
        try:
            self._write_owed(job_id, owed)
        except Exception:
            with self._unrecorded_lock:
                self._unrecorded_failures[job_id] = owed
            raise

    def _idle_housekeeping(self) -> None:
        """
        Use an idle tick: retry owed writes (probing while degraded), then prune.

        Owed writes come first and are retried on every tick, degraded or not,
        so a row the guard could not end reaches ERROR as soon as the store
        accepts writes, without a restart or a scan (CR-01).  A failed retry is
        not a loop-level failure: the guard already counted the failure behind
        a guard debt (D-10).  But ``_OWED_RETRY_DEGRADED_AFTER`` failed ticks in
        a row degrade the worker, so a store that is not healing reaches
        ``/health`` instead of only the logs (WR-10); a retry that lands ends
        the streak, and so does a job whose store writes all landed (IN-09).
        The probe and the degraded clear stay the degraded
        worker's business (D-12).  A prune failure is a loop-level failure
        (D-10), and it can never fail a job: no job is running on an idle tick
        (D-13).
        """
        if self._degraded.is_set():
            self._try_recover()
        else:
            try:
                self._flush_unrecorded_failures()
            except Exception:
                self._failed_flush_ticks += 1
                # The first failure of a streak is worth an operator's eye; the
                # rest of the same streak would only repeat it.
                if self._failed_flush_ticks == 1:
                    logger.warning(
                        "Owed job store writes failed; retrying on the next idle tick",
                        exc_info=True,
                    )
                else:
                    logger.debug(
                        "Owed job store writes failed; retrying on the next idle tick",
                        exc_info=True,
                    )
                if self._failed_flush_ticks >= _OWED_RETRY_DEGRADED_AFTER:
                    self._degraded.set()
                    logger.warning(
                        "Scan worker degraded after %d idle ticks in a row failed "
                        "to write owed job records; rejecting scans until the "
                        "store recovers",
                        self._failed_flush_ticks,
                    )
            else:
                self._failed_flush_ticks = 0
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
        Write the terminal rows the worker still owes, dropping each once written.

        This runs only on an Empty tick -- from ``_idle_housekeeping`` or
        ``_try_recover`` -- so no job is current, and an owed id belongs to a
        job the loop already abandoned: none can be the job in flight.  With
        nothing owed it returns without touching the store.

        The owed entries are snapshotted under ``_unrecorded_lock``, and every
        store write runs outside it, so a request thread calling
        :meth:`owe_rejection` never waits on the store.  An entry is dropped
        only if it is unchanged since the snapshot; one owed after the
        snapshot waits for the next tick.

        Raises:
            Exception: Whatever the store raises.  Rows already written stay
                written and are no longer owed; the rest wait for the next
                tick.

        """
        with self._unrecorded_lock:
            owed_writes = list(self._unrecorded_failures.items())
        for job_id, owed in owed_writes:
            self._write_owed(job_id, owed)
            self._forget_owed(job_id, owed)
            logger.info(
                "Recorded job %s as %s now that the job store accepts writes",
                job_id,
                owed.state.value.lower(),
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
        Clearing degraded also ends any owed-write streak, so a returning fault
        needs a fresh streak.
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
        self._failed_flush_ticks = 0
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
        callback, which reaches here through ``run_pipeline``.  A cancel is
        recorded as CANCELLED, and a flip answer claimed by shutdown as ERROR
        with ``RESTART_REASON``; neither is a failure (D-01, D-02).  The loop's own
        store writes -- SCANNING before the pipeline, and the terminal write of
        either outcome -- are never swallowed, so their failure escapes to
        ``_run`` as a loop-level failure (D-10).  A failed terminal write is
        owed first, with the outcome it meant to record (WR-02).

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
                    coordinator.abort_for_shutdown()
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
            # No result argument: outcome, warning and all three page counts
            # stay NULL.  NULL means "never recorded"; 0 would claim a
            # measurement a job that never reached the scanner did not make.
            # If this write raises, the job ending could not be recorded, and
            # that is the loop's failure (D-10); the write is owed first, so
            # the pipeline's own ending is what lands later (WR-02).  While
            # stopping this is the worker thread's own final write, so D-07's
            # "no shutdown-time write" -- which is about the lifespan writing
            # over a running thread -- holds.
            #
            # Three endings, three branches (D-01).  The shutdown check stays
            # first: stop() answers the flip wait with Abort, and that Abort
            # reaches the pipeline exactly as an operator's does, so it comes
            # back as ScanCancelledError too (D-02, WR-06).
            if coordinator is not None and coordinator.aborted_by_shutdown:
                self._finish_or_owe(
                    job.id, _OwedWrite(JobState.ERROR, error=RESTART_REASON)
                )
                logger.info("Job %s ended by shutdown: %s", job.id, exc)
            elif isinstance(exc, ScanCancelledError):
                # The operator ended the scan.  Not a failure, so no category,
                # no ERROR line and no traceback (N-08).
                self._finish_or_owe(
                    job.id, _OwedWrite(JobState.CANCELLED, error=str(exc))
                )
                logger.info("Job %s cancelled: %s", job.id, exc)
            else:
                category = classify_error(exc)
                self._finish_or_owe(
                    job.id,
                    _OwedWrite(JobState.ERROR, error=str(exc), category=category),
                )
                # Inside the except block, so the record carries the traceback
                # the operator needs to find the cause (EXC-05).
                kind = category.value.lower()
                logger.exception("Job %s failed (%s): %s", job.id, kind, exc)
            return
        # The terminal state is derived from the outcome the pipeline returned,
        # never assumed.  The mapping below is a match with assert_never, so a
        # future third ScanOutcome member fails the type gate at edit time
        # rather than falling silently into an else.  The upload has already
        # happened, so a failed write is owed with this outcome and replayed
        # as it is, never recorded as an ERROR that invites a rescan (WR-02).
        self._finish_or_owe(
            job.id,
            _OwedWrite(
                job_state_for(result.outcome),
                result=JobResult(
                    outcome=result.outcome,
                    warning=result.warning,
                    pages_scanned=result.pages_scanned,
                    pages_removed=result.pages_removed,
                    pages_uploaded=result.pages_uploaded,
                ),
            ),
        )
