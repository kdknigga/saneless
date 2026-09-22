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
    device_type_of,
    generate_profiles,
    is_bare_default,
    write_profiles_to_config,
)
from .config import (
    CONFIG_FILENAME,
    config_file_state,
    config_search_paths,
    profile_storage_for_loaded,
)
from .exceptions import ConfigError, ScanCancelledError
from .job import JobResult
from .pipeline import (
    SCAN_LABEL_FRONT,
    FlipAnswerSlot,
    FlipCoordinator,
    PipelineEvent,
    PipelineRequest,
    run_pipeline,
)
from .vocabulary import (
    ACTIVE_STATES,
    RESTART_REASON,
    ConfigFileState,
    ErrorCategory,
    FlipOutcome,
    JobState,
    ProfileStorage,
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

# How long stop() waits for the worker thread before reporting it still alive.
# Five seconds leaves room for uvicorn inside Docker's 10 s SIGKILL grace.
# Deliberately not configurable.  Read at call time, so tests can shorten it.
STOP_JOIN_SECONDS: Final = 5.0

# The idle loop's queue.get() timeout.  stop() does not rely on it -- the queue
# shutdown wakes a blocked get() at once -- so it only sets how often an idle
# worker gets a turn for housekeeping.  Read at call time.
_IDLE_TICK_SECONDS: Final = 5.0

# How many unstarted jobs may wait behind the running one.  Not configurable:
# a submit beyond it is reported as QUEUE_FULL rather than queued.
_QUEUE_DEPTH: Final = 10

# How many loop-level failures in a row -- the loop's own job store writes, or
# the idle prune, raising -- make the worker degraded.  A pipeline
# failure is a job failure and never counts.  Three rides out one transient
# error without calling the store broken.  Not configurable.
_DEGRADED_AFTER: Final = 3

# How many idle ticks in a row the owed-write retry may fail before the worker
# is degraded.  Kept apart from _DEGRADED_AFTER's loop count: the guard
# already counted the failure behind a guard debt, and a request-side
# owe_rejection debt was never counted, so a streak of failed retries is its
# own evidence that the store is not healing.  Three rides out a fault that
# heals within a tick or two, and at the 5 s idle tick bounds a stuck row's
# silent window to about 15 s.  Not configurable.  Read at call time, so tests
# can change it.
_OWED_RETRY_DEGRADED_AFTER: Final = 3

# How often an idle worker prunes job history.  Prune left the per-job
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
    ERROR that invites a duplicate scan.  Frozen, so the flush's
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
    ``auto_generated = true``, and the worker never forces, so a flagged one
    is skipped too.  A file that spells out a bare ``[profiles.default]``
    therefore keeps it.  Swapping the generated ``default`` into memory anyway
    would give this run one ``default`` and every later run another.

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
    claims the answer first is the answer; anything arriving later is dropped.

    It is also bound to one job and accepts a signal only once armed.  The
    worker arms it when the job announces ``AWAITING_FLIP``, which is after
    pass A has finished.  Until then a signal is dropped, not queued: a stale
    Abort double-clicked at the previous job's prompt, or a Continue sent
    during pass A, would otherwise pre-answer a prompt nobody has seen yet and
    abort the wrong job or start pass B on an unflipped stack.
    ``FlipCoordinator`` itself is unchanged: arming is this class's detail,
    not part of the contract the pipeline waits on, and not part of the
    shared ``FlipAnswerSlot`` either.

    The claim itself -- first answer wins, and a waiter that wakes always finds
    an answer -- is ``FlipAnswerSlot``'s, shared with the CLI coordinator so a
    fix to it reaches both.  Arming is a one-way latch checked before
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
        # woken by that claim, cannot read the marker before it is set.
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
        first keeps its own meaning.

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
        the slot, leaving the answer untouched either way.

        Args:
            outcome: The answer the operator is offering.

        Returns:
            Whether ``outcome`` became the answer.

        """
        if not self._armed.is_set():
            return False
        return self._slot.offer(outcome)


def _ended_by_shutdown(
    exc: Exception, coordinator: WorkerFlipCoordinator | None
) -> bool:
    """
    Say whether a job ended because shutdown answered its flip wait with Abort.

    ``stop()`` claims the answer as soon as a manual-duplex job is current,
    including while pass A is still scanning, so the coordinator's marker alone
    does not mean the job ended there.  Only a ``ScanCancelledError`` -- the
    pipeline coming back through that aborted flip -- is a shutdown ending.  A
    jam, an empty feeder or any other pass-A failure raised after the claim
    stays a failure, with its own text, category and traceback.

    Args:
        exc: What ended the job.
        coordinator: The job's flip coordinator, if it has one.

    Returns:
        True only for a cancel whose flip answer shutdown claimed.

    """
    return (
        isinstance(exc, ScanCancelledError)
        and coordinator is not None
        and coordinator.aborted_by_shutdown
    )


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
        """
        Store the worker's dependencies and create its job queue.

        The worker thread is created here but not started; ``start()`` launches it.
        """
        self._scanner = scanner
        self._paperless = paperless
        self._settings = settings
        self._job_store: JobStore = job_store
        self._queue: queue.Queue[Job] = queue.Queue(maxsize=_QUEUE_DEPTH)
        self._thread = threading.Thread(target=self._run, daemon=True)
        # Set once by stop(); read by the loop, submit() and the flip callback.
        self._stopping = threading.Event()
        # Guards every read and every rebind of self._settings.profiles.
        self._profiles_lock = threading.Lock()
        self._flip_coordinator: WorkerFlipCoordinator | None = None
        self._current_job_id: str | None = None
        # Pass A's page count for the job in flight, written by the pipeline's
        # pass-count callback on the worker thread and read by request threads
        # rendering the status area.  Its own lock rather than
        # _profiles_lock: they guard unrelated state and sharing one would make
        # a status render wait behind a profile swap for no reason.
        self._front_pages_lock = threading.Lock()
        self._front_pages: int | None = None
        # Held for the whole of a job's pipeline call, and for the whole of the
        # startup capability read, so nothing else can be inside SANE at the
        # same time.  It is needed because
        # scanner/sane_backend.py provides no mutual exclusion of its own:
        # _refuse_if_wedged fires on a read that is already *stuck* rather than
        # one that is merely running, and _INIT_LOCK guards sane_init and
        # sane_exit only.  On the net backend a status-strip probe landing
        # mid-scan would therefore be a second RPC on the control wire the scan
        # is using -- a lost sheet, not a slow page.
        #
        # The advisory alternative, "skip the scanner check while
        # current_job_id is not None", was rejected: a reader can see None,
        # enter get_devices(), and have a job start a microsecond later.
        self._scanner_gate = threading.Lock()
        # What became of the one startup persist attempt.  Recorded rather
        # than recomputed because _persist_generated_profiles returns None for
        # two different situations -- no config file was loaded, and one was
        # loaded and could not be written -- and the Profiles row has to tell
        # them apart.  A fresh os.access() probe at check time cannot
        # substitute: the failure that matters is EBUSY on a single-file bind
        # mount, where the directory is writable, os.access says yes, and only
        # the rename fails.
        #
        # The default is an in-memory member, not PERSISTED: before any attempt
        # nothing is on disk, and that is the one answer that could mislead.
        #
        # Written on the worker thread, read on request threads, and no lock:
        # see the profile_storage property's docstring for why that is a
        # decision rather than an omission.
        self._profile_storage: ProfileStorage = ProfileStorage.IN_MEMORY_NO_CONFIG_FILE
        # Loop-level failures in a row.  Touched only by the worker thread.
        self._consecutive_loop_failures = 0
        # Idle ticks in a row whose owed-write retry raised, with no landed
        # retry, recovery or cleanly recorded job in between.  Touched only by
        # the worker thread.
        self._failed_flush_ticks = 0
        # When the idle loop last pruned.  Starts now: the startup prune is the
        # lifespan's, so the first idle prune is an interval away.
        self._last_prune = time.monotonic()
        # Terminal job-row writes owed by id, each kept as the write it was
        # meant to be: the loop's own terminal writes that failed, kept with
        # their real outcome, failures even the loop guard could not write,
        # and rejected submits whose REJECTED write failed in the request.
        # Every idle tick retries them, and a streak of failed retries
        # degrades the worker.  Shared by the worker thread (the guard and the
        # idle flush) and request threads (owe_rejection), so every read and
        # write goes through _unrecorded_lock.
        self._unrecorded_lock = threading.Lock()
        self._unrecorded_failures: dict[str, _OwedWrite] = {}
        # Set and cleared by the worker thread (and by mark_recovery_pending,
        # before the thread exists); read by request threads through health and
        # submit(), so an Event rather than a bare bool.
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
        raised at startup.  The app still starts, ``/health`` answers a
        truthful 503 and scans are rejected, instead of the service refusing to
        come up over a store that may recover.  The first successful idle probe
        then fails the rows the previous process left active with
        ``RESTART_REASON`` and clears degraded.
        """
        self._restart_recovery_pending = True
        self._degraded.set()

    def owe_rejection(self, job_id: str, error: str) -> None:
        """
        Take over a refused submit's REJECTED write that the request could not make.

        The scan route calls this when finishing a refused submit's row as
        ``ERROR`` with ``ErrorCategory.REJECTED`` raised, so the row is not
        left PENDING with no marker and the Scan button disabled.  The id was
        refused by :meth:`submit` and never enqueued, so no job can be running
        under it.

        The worker writes it on its next idle tick, or through the recovery
        path while degraded, sharing the flush of the loop guard's owed
        failures.  If the worker thread is not running (DOWN) no tick comes:
        ``/health`` reports 503 meanwhile, and the next startup's
        ``fail_active_jobs(RESTART_REASON)`` ends the row.

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
        writes one, its row is PENDING with no REJECTED marker.  Failures the
        loop guard could not write are left out on purpose, because those jobs
        ran and the status area still reports each as the job that just ended.
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
        The worker's health, as ``/health`` reports it.

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
        anything, so a full queue cannot hold it.  Jobs still queued are
        abandoned on purpose, and so is a scan inside a SANE read: their rows
        stay active and the next startup's recovery fails them.  An open flip
        wait is answered with Abort, so a job parked at the flip prompt lets
        the thread go at once.

        The join is bounded by ``STOP_JOIN_SECONDS``.  When this returns
        ``False`` the thread is still running and may still write to the job
        store, so the caller must leave the store and the Paperless client
        open.  Calling it again, or on a worker never started, is safe.

        Returns:
            Whether the worker thread has stopped.

        """
        self._stopping.set()
        # immediate=True discards queued-but-unstarted jobs and
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
        Offer a job to the worker without ever blocking.

        The caller creates the job row first, so the worker never dequeues an
        id with no row, and records the rejection itself when this does not
        return ``ACCEPTED``.

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
            # Nobody is asked to feed paper into a job whose outcome
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

        The web status rendering reads this to tell an answered prompt from an
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
        and deliver a click meant for one job to the next.

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

    @property
    def front_pages(self) -> int | None:
        """
        Pass A's page count for the job in flight, or ``None``.

        It is set while the current job is in manual duplex, from the moment
        pass A finishes, and is cleared however the job ends.  The status area
        reads it only when the job it is rendering is in
        ``JobState.SCANNING_REVERSE``: at any other moment the number either
        does not exist yet or is about to be superseded by the row's own
        ``pages_scanned``.

        Deliberately not a ``Job`` column.  A column would need a schema
        migration of every existing job database, and the value would be a
        poor column anyway -- it is meaningful for the length of one pass and
        meaningless the instant the job ends, which is the opposite of what the
        job table stores.

        Returns:
            The count, or ``None`` when no manual-duplex pass A has finished.

        """
        with self._front_pages_lock:
            return self._front_pages

    @property
    def scanner_gate(self) -> threading.Lock:
        """
        The lock held whenever this worker is inside SANE.

        The contract for a caller is one move and one move only: probe with
        ``acquire(blocking=False)`` and, when that fails, **skip** the scanner
        check and report it as unknown for this cycle.  Never block on it.  A
        background refresher that waited here would queue behind a scan that
        can legitimately run for minutes, and would then enter SANE at some
        arbitrary later moment with the freshness its own caller assumed long
        gone.  A caller that succeeds owns the scanner until it releases, so it
        must use ``with`` or an equivalent ``try``/``finally``.

        Returns:
            The gate itself, so the caller can make that non-blocking attempt.

        """
        return self._scanner_gate

    @property
    def profile_storage(self) -> ProfileStorage:
        """
        What became of the profiles generated at startup.

        Three outcomes, kept apart because the Profiles row means to tell a
        household member which one happened: ``PERSISTED`` (they are in the
        config file and survive a restart), ``IN_MEMORY_NO_CONFIG_FILE``
        (saneless has no file to save to -- expected, and nothing to
        investigate) and ``IN_MEMORY_UNWRITABLE`` (saneless has one and could
        not write it -- worth looking at).

        A worker whose startup generation never ran -- because the settings
        were not the bare default, or because the scanner could not be read --
        attempted no write, so it reports what
        ``config.profile_storage_for_loaded`` says about the settings it
        loaded.  That is the same function ``saneless doctor`` calls, which is
        what keeps the strip and the command on one Profiles row.

        Thread discipline, stated because the silence would otherwise read as
        an oversight: the attribute behind this property is rebound only on the
        worker thread, only during the single startup generation step, and is
        then read unchanged by request threads for the life of the process.  A
        single enum rebind is atomic under the GIL, so there is no torn read to
        protect against, and it is deliberately left unlocked.  Its sibling
        ``front_pages`` has a dedicated lock for the opposite reason: that
        value is rewritten repeatedly while a job runs.

        Returns:
            The recorded outcome of the single startup persist attempt, or the
            loaded-settings fact when no attempt was made.

        """
        return self._profile_storage

    def profile_names(self) -> list[str]:
        """
        List the configured profile names, in configuration order.

        Request threads (the index dropdown) and the worker share one lock for
        every profile read; the routes are ``def`` handlers on the threadpool,
        so that concurrency is real.

        Returns:
            A new list, so the caller can keep or change it freely.

        """
        with self._profiles_lock:
            return list(self._settings.profiles)

    def get_profile(self, name: str) -> ProfileConfig | None:
        """
        Look up a profile under the profile lock.

        The worker's own lookup for a job goes through here, sharing the lock
        with request threads.  So does the scan route's unknown-profile check:
        a ``None`` here is the "no such profile" answer, so one locked lookup
        both validates the name and yields the profile.

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
        Replace the configured profiles with a new dict, under the lock.

        The mapping is rebound, never mutated in place.  A reader that took the
        old dict without the lock -- ``run_pipeline`` on the worker thread, for
        example -- keeps a consistent view of it, and no locked reader can
        observe a dict part-way through an update.

        This is the one place the profiles are rebound: startup generation
        swaps through it too, with its bare-default re-check as ``only_if``.

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

        This runs on the worker thread before it takes any job, so the server
        is already answering requests while it works.  A page loaded meanwhile
        may list only ``default`` until it is reloaded; a job submitted
        meanwhile waits in the queue and then runs against the generated set.

        It is tried once per start.  A scanner failure is logged with the real
        exception class and the bare default is kept; the cause is never
        guessed.  Restarting saneless, or ``saneless auto-profiles``, retries.

        The profiles are written only to ``settings.config_path``, the file
        these settings were loaded from.  With no loaded file they are used in
        memory for this run (INFO); when the loaded file cannot be written they
        are used in memory too (WARNING).  Nothing is ever written to a path
        worked out afresh here.

        The swap happens under the profile lock, after re-checking that the set
        is still the bare default.

        Memory matches what the file will load after a restart.  When the file
        already defines ``default``, the write keeps it, so the loaded
        ``default`` is kept in memory too rather than the generated one.
        """
        with self._profiles_lock:
            bare = is_bare_default(self._settings)
            loaded = self._settings.profiles
        if not bare:
            # Nothing was generated, so nothing was persisted -- but the
            # profiles in hand came from the loaded file, and the Profiles row
            # must not tell a household member they are in memory and lost on
            # restart when they are in the file they just edited.
            self._profile_storage = profile_storage_for_loaded(self._settings)
            return
        profiles = self._read_generated_profiles()
        if profiles is None:
            # A SANE failure during generation leaves the loaded profiles in
            # place; they are no more in-memory than they were a moment ago.
            self._profile_storage = profile_storage_for_loaded(self._settings)
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
            # Gated for the same reason _scan_job is, and it
            # is a real second entry into SANE rather than a precaution:
            # get_devices() is an enumeration RPC on the net backend's control
            # wire, and get_capabilities() opens the device and reads its
            # option list.  Held across both, because a probe slipping between
            # them is inside SANE just as surely as one during either.
            #
            # No re-entrancy hazard: this runs once, as the worker thread's
            # first act, strictly before any job -- so the gate is never
            # already held by this thread when it arrives here.
            #
            # That same fact makes this the health checks' one known gate
            # contender with no scan anywhere in sight: _current_job_id is
            # still None here, and the lifespan starts the refresher right
            # after the worker, so this window is exactly the cold-start poll's
            # window.  A check that loses this gate has therefore *not* lost it
            # to a scan and must not report one -- which is why checks.py has
            # _scanner_busy() beside _scanner_skipped().
            with self._scanner_gate:
                devices = self._scanner.get_devices()
                if not devices:
                    logger.warning(
                        "Auto-profiles: no scanners found, using bare default"
                    )
                    return None
                device_id = self._settings.scanner.device or devices[0].name
                caps = self._scanner.get_capabilities(device_id)
            return generate_profiles(caps, device_type_of(devices, device_id))
        except Exception as exc:
            # The exception class is named, never interpreted.  The old
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

    def _log_no_config_file(self) -> None:
        """
        Say why the generated profiles were not written, naming the real reason.

        Two situations end up here and they want different sentences.  Usually
        nothing was found and the fix is to create a file, so the message
        lists the places that were looked at.  But a file under the superseded
        name sitting in one of those directories is also "nothing loaded", and
        telling that operator to create a file -- while naming the very
        directory the file they already wrote is in, without mentioning it --
        is how one appliance came to report four symptoms and no cause.

        The state comes from ``config_file_state``, the same derivation the
        startup log, the Configuration row, ``doctor`` and the CLI read, so
        this message cannot come to disagree with them.  Nothing is written
        and the superseded-name file is only named: a rename is the operator's
        to make, and doing it for them would be this process deciding which of
        two files holds the configuration.
        """
        discovery = self._settings.config_discovery
        if (
            discovery is not None
            and config_file_state(self._settings) is ConfigFileState.STALE_ONLY
        ):
            logger.info(
                "Auto-profiles: no config file was loaded, so the generated "
                "profiles are used for this run only and were not written; %s "
                "was ignored because saneless reads %s, not the old name; "
                "rename it to keep them",
                discovery.stale[0].absolute(),
                CONFIG_FILENAME,
            )
            return
        # The recorded search when there is one, so the list is what this
        # process actually looked at rather than what a fresh call would
        # return; settings built directly carry no recording and fall back.
        searched = (
            tuple(path.absolute() for path in discovery.searched)
            if discovery is not None
            else config_search_paths()
        )
        logger.info(
            "Auto-profiles: no config file was loaded, so the generated "
            "profiles are used for this run only and were not written; pass "
            "--config or create one of %s to keep them",
            ", ".join(str(path) for path in searched),
        )

    def _persist_generated_profiles(
        self, profiles: dict[str, ProfileConfig]
    ) -> ProfileWriteResult | None:
        """
        Write generated profiles to the loaded config file, if there is one.

        Failure to write is logged, never raised, whatever it raises: the
        profiles are still used in memory for this run.  Success is logged in
        the same group vocabulary the CLI prints.

        Args:
            profiles: The generated profiles.

        Returns:
            What the write did to the file, or ``None`` when nothing was
            persisted because no file was loaded or the write failed.

        """
        config_path = self._settings.config_path
        if config_path is None:
            self._log_no_config_file()
            self._profile_storage = ProfileStorage.IN_MEMORY_NO_CONFIG_FILE
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
            self._profile_storage = ProfileStorage.IN_MEMORY_UNWRITABLE
            return None
        except Exception as exc:
            # Anything else -- a tomlkit container error, say; a parse or UTF-8
            # failure is already a ConfigError -- must not throw away the
            # generated profiles either.  Unexpected, so the traceback is
            # logged too; the exception class is named, never interpreted.
            logger.warning(
                "Auto-profiles: could not write %s (%s); the generated "
                "profiles are used for this run only and will not survive a "
                "restart",
                config_path,
                type(exc).__name__,
                exc_info=True,
            )
            # The same outcome as the branch above, and deliberately so: two
            # causes, one fact.  A file was loaded, and the profiles did not
            # reach it.  The row says that; the log says why.
            self._profile_storage = ProfileStorage.IN_MEMORY_UNWRITABLE
            return None
        logger.info(
            "Auto-profiles: %s: %s",
            result.path,
            "; ".join(result.describe()) or "no changes",
        )
        self._profile_storage = ProfileStorage.PERSISTED
        return result

    def _run(self) -> None:
        """
        Worker loop: process jobs until stop() sets the stop flag.

        There is no sentinel.  An idle ``get`` wakes every
        ``_IDLE_TICK_SECONDS`` for housekeeping, and stop()'s queue shutdown
        wakes it at once with ``queue.ShutDown``.

        Nothing ends the loop but stopping.  A pipeline failure is recorded by
        ``_process_job`` itself; whatever still escapes it is a failure of the
        loop's own job store writes, which is logged, counted towards degraded,
        and answered with one best-effort terminal write so the row does not
        sit active until restart: the terminal write the loop was making, if
        that is what failed, otherwise an ERROR.  If that write fails too, idle
        ticks retry it until it lands.

        Before any job, the thread generates profiles.  A job submitted
        meanwhile waits in the queue and then runs against the generated set.
        After the loop, it tries once more to write whatever is still owed.
        """
        try:
            self._generate_startup_profiles()
        except Exception:
            # _generate_startup_profiles catches what it expects itself; this
            # is the backstop that keeps a surprise from ending the thread
            # before it has taken a single job.
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
                # PENDING and the next startup's recovery fails it.
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
                # "in a row" against a store that just accepted writes.  It
                # does not clear degraded: only a successful idle probe does.
                self._consecutive_loop_failures = 0
                self._failed_flush_ticks = 0
        self._flush_before_exit()

    def _flush_before_exit(self) -> None:
        """
        Try once to write every owed row before the thread exits.

        Owed writes live only in memory.  Left unwritten, an owed rejection
        comes back after a restart as a PENDING row that the next startup's
        recovery ends as "server restarted", for a scan that never started,
        and until then it shows as the live job.  This is the worker thread's
        own write, so the lifespan's rule against writing over a running
        thread holds.  A failure is only logged: the next startup's recovery
        still ends the rows.
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
        its own text and category.  Only a job that came back through a flip
        answer the shutdown itself claimed is recorded as a restart.

        ``_best_effort_fail`` records a loop-level failure through this.  A
        pipeline exception in ``_scan_job`` does not: that path tells its three
        endings -- shutdown, cancel and failure -- apart itself, because a
        cancel is written CANCELLED rather than ERROR.

        Args:
            exc: What ended the job.
            coordinator: The job's flip coordinator, if it has one.

        Returns:
            ``RESTART_REASON`` with no category when the job ended at a flip
            answer shutdown claimed, otherwise the exception's own text and its classified
            category.

        """
        if _ended_by_shutdown(exc, coordinator):
            return RESTART_REASON, None
        return str(exc), classify_error(exc)

    def _record_loop_failure(self) -> None:
        """Count one loop-level failure, degrading at ``_DEGRADED_AFTER``."""
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
        DONE or FALLBACK, never an ERROR that invites a duplicate scan.
        Otherwise the job is recorded as failed with the loop failure's text.

        The store just raised, so this may raise too; that is only logged.
        The write is remembered instead, and the next idle tick retries it --
        through the recovery path while degraded -- until the store accepts
        it, so the row does not sit active until a restart.

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
        exception -- and so does every idle tick after it.

        Args:
            job_id: The job row to write.
            owed: The terminal write to make.

        Raises:
            Exception: Whatever the store raises; it stays a loop-level
                failure.

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
        accepts writes, without a restart or a scan.  A failed retry is not a
        loop-level failure: the guard already counted the failure behind a
        guard debt.  But ``_OWED_RETRY_DEGRADED_AFTER`` failed ticks in a row
        degrade the worker, so a store that is not healing reaches ``/health``
        instead of only the logs; a retry that lands ends the streak, and so
        does a job whose store writes all landed.  The probe and the degraded
        clear stay the degraded worker's business.  A prune failure is a
        loop-level failure, and it can never fail a job: no job is running on
        an idle tick.
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
            logger.warning("Idle history prune failed", exc_info=True)
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

        Before clearing, recovery ends the rows the loop could not, so none is
        left sitting active until a restart.  This runs on an Empty tick, so
        the queue is empty, no job is current, and every submit was rejected
        while degraded: an active row is an orphan.  A submit racing the clear
        below is accepted, and its row is new, so it is neither of the rows
        written here.

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
            # the idle tick, where its failure cannot fail a job.
            self._flip_coordinator = None
            self._current_job_id = None
            # Cleared here rather than at the start of the next job, so no
            # observer can ever read the previous job's count against a row
            # that has already moved on.
            with self._front_pages_lock:
                self._front_pages = None

    def _scan_job(self, job: Job) -> None:
        """
        Run one job through the pipeline and record how it ended.

        A pipeline failure is a job failure: it is recorded as ERROR here and
        this returns normally.  That includes a store write inside a pipeline
        callback, which reaches here through ``run_pipeline``.  A cancel is
        recorded as CANCELLED, and a flip answer claimed by shutdown as ERROR
        with ``RESTART_REASON``; neither is a failure.  The loop's own store
        writes -- SCANNING before the pipeline, and the terminal write of
        either outcome -- are never swallowed, so their failure escapes to
        ``_run`` as a loop-level failure.  A failed terminal write is owed
        first, with the outcome it meant to record.

        Args:
            job: The Job to process.

        """
        self._job_store.update_state(job.id, JobState.SCANNING)

        # Flip machinery follows profile.duplex alone, the same field
        # run_pipeline reads to choose the strategy.  source is a pure SANE
        # value and is never consulted: a second copy of the detection rule
        # here could drift from the pipeline's and skip the flip wait.
        profile = self.get_profile(job.profile)
        is_manual_duplex = profile is not None and profile.duplex == "manual"

        coordinator = WorkerFlipCoordinator(job.id) if is_manual_duplex else None
        self._flip_coordinator = coordinator

        def _thumbnail_cb(thumb: str, _jid: str = job.id) -> None:
            self._job_store.update_thumbnail(_jid, thumb)

        # The back count is ignored on purpose.  It arrives a moment before the
        # ScanResult that carries the run's real total, so storing it would
        # replace the number the operator is reading with one that is about to
        # be replaced again -- a flicker in place of information.
        def _pass_count_cb(label: str, count: int) -> None:
            if label != SCAN_LABEL_FRONT:
                return
            with self._front_pages_lock:
                self._front_pages = count

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
                # cannot accept a click sent during pass A.
                coordinator.arm()
                if self._stopping.is_set():
                    # stop() may have run while this job was still in pass A,
                    # found no prompt to answer, and returned to its join.
                    # Abort now, or the wait would hold the thread for
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
            pass_count_callback=_pass_count_cb,
            flip_coordinator=coordinator,
        )
        try:
            # The gate covers the whole pipeline call, which is the whole of
            # this job's contact with the scanner -- both passes, the flip wait
            # between them, and the assembly and upload that follow.  Wrapping
            # only the scan_pages calls would leave the flip wait ungated, and
            # a manual-duplex job spends most of its life there with the feeder
            # loaded and the device open.
            #
            # ``with`` rather than acquire/release, so every exit path -- a
            # jam, an Abort, a shutdown, a Paperless failure -- hands the
            # scanner back.  The release happens before the except block runs.
            with self._scanner_gate:
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
            # that is the loop's failure; the write is owed first, so the
            # pipeline's own ending is what lands later.  While stopping this
            # is the worker thread's own final write, so the rule against a
            # shutdown-time write -- which is about the lifespan writing over
            # a running thread -- holds.
            #
            # Three endings, three branches.  The shutdown check stays first:
            # stop() answers the flip wait with Abort, and that Abort reaches
            # the pipeline exactly as an operator's does, so it comes back as
            # ScanCancelledError too.  It is a shutdown only when the job
            # really came back through that aborted flip: stop() claims the
            # answer while pass A is still scanning, and a jam or an empty
            # feeder there is a failure with its own text, category and
            # traceback, not a restart.
            if _ended_by_shutdown(exc, coordinator):
                self._finish_or_owe(
                    job.id, _OwedWrite(JobState.ERROR, error=RESTART_REASON)
                )
                logger.info("Job %s ended by shutdown: %s", job.id, exc)
            elif isinstance(exc, ScanCancelledError):
                # The operator ended the scan.  Not a failure, so no category,
                # no ERROR line and no traceback.
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
                # the operator needs to find the cause.
                kind = category.value.lower()
                logger.exception("Job %s failed (%s): %s", job.id, kind, exc)
            return
        # The terminal state is derived from the outcome the pipeline returned,
        # never assumed.  The mapping below is a match with assert_never, so a
        # future third ScanOutcome member fails the type gate at edit time
        # rather than falling silently into an else.  The upload has already
        # happened, so a failed write is owed with this outcome and replayed
        # as it is, never recorded as an ERROR that invites a rescan.
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
