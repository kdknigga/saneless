"""
Tests for the web worker's side of a multi-page scan.

Two things are specified here.  ``WorkerPassCoordinator`` is the coordinator a
web job's pipeline asks between passes: it claims an answer only for the prompt
that is open and only from the answers that prompt offers, and a stopping
server interrupts it at once.  ``ScanWorker`` carries the per-scan multi-page
choice from ``submit`` to the pipeline, persists every multi-page wait on the
job row, and lets request threads read and answer the open prompt.

The worker tests replace ``run_pipeline`` with a fake that announces one wait
and asks one prompt, so only the worker's own behaviour is observed.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, override

import pytest

from saneless.job import ErrorCategory, Job, JobState, JobStore
from saneless.pipeline import AnswerSlot, PipelineEvent, ScanResult
from saneless.vocabulary import (
    TERMINAL_STATES,
    PassAnswer,
    PassPrompt,
    PassWait,
    ScanOutcome,
    SubmitResult,
)
from saneless.worker import (
    DEFAULT_SCAN_OPTIONS,
    ScanOptions,
    ScanWorker,
    WorkerPassCoordinator,
)
from tests.conftest import poll_until

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from unittest.mock import MagicMock

    from saneless.config import Settings
    from saneless.pipeline import PipelineRequest

# How long a test waits for the worker thread to reach a point.  Far below
# pytest-timeout's ceiling, so a hang fails with a message rather than SIGALRM.
_BUDGET = 2.0

# How long a prompt that someone is expected to answer stays open.  Long enough
# that answering it inside _BUDGET is never a race, and long enough that an
# interrupted wait is plainly shorter than it.
_OPEN_PROMPT_TIMEOUT = 5.0

# How long the fake pipeline holds at a gate the test is expected to open.  It
# is a bound, not a pause: a test that fails before opening the gate still lets
# the worker thread go, so stop() in the fixture's teardown returns.
_GATE_BOUND = 5.0

_NEXT_PASS_OFFERED = frozenset(
    {PassAnswer.NEXT, PassAnswer.RESCAN, PassAnswer.FINISH, PassAnswer.ABORT}
)
_BLANK_OFFERED = frozenset(
    {PassAnswer.RESCAN, PassAnswer.SKIP_BLANKS, PassAnswer.KEEP_BLANKS}
)
_RETRY_OFFERED = frozenset({PassAnswer.NEXT, PassAnswer.FINISH, PassAnswer.ABORT})

_LOCKED = "database is locked"


@pytest.fixture(autouse=True)
def _mock_scanner_reports_no_devices(mock_scanner: MagicMock) -> None:
    """
    Answer the shared mock scanner's ``get_devices`` with a deliberate ``[]``.

    Every worker started over ``default_settings`` runs startup profile
    generation first.  "No scanners found" is a real answer: generation logs
    its WARNING and keeps the bare default profile.
    """
    mock_scanner.get_devices.return_value = []


def _prompt(
    number: int = 1,
    *,
    wait: PassWait = PassWait.NEXT_PASS,
    offered: frozenset[PassAnswer] = _NEXT_PASS_OFFERED,
    timeout: float = 0.0,
) -> PassPrompt:
    """
    Build a one-page multi-page prompt with only the fields a test cares about.

    Returns:
        The prompt.

    """
    return PassPrompt(
        number=number,
        wait=wait,
        pages_kept=1,
        offered=offered,
        timeout_seconds=timeout,
    )


def _success_result() -> ScanResult:
    """Return the ScanResult a successful pipeline run would produce."""
    return ScanResult(
        outcome=ScanOutcome.SUCCESS,
        pages_scanned=1,
        pages_removed=0,
        pages_uploaded=1,
    )


class _Asker:
    """
    Run one ``ask`` on a thread of its own, so the test can answer it.

    Args:
        coordinator: The coordinator to ask.
        prompt: The prompt to ask it.

    """

    def __init__(self, coordinator: WorkerPassCoordinator, prompt: PassPrompt) -> None:
        """Start the ask."""
        self._coordinator = coordinator
        self._prompt = prompt
        self._answers: list[PassAnswer] = []
        self._thread = threading.Thread(target=self._ask, daemon=True)
        self._thread.start()

    def _ask(self) -> None:
        """Ask, and keep the answer."""
        self._answers.append(self._coordinator.ask(self._prompt))

    def result(self) -> PassAnswer:
        """
        Wait for the ask to return, and return its answer.

        Returns:
            What ``ask`` returned.

        """
        self._thread.join(_BUDGET)
        assert not self._thread.is_alive(), "ask never returned"
        assert len(self._answers) == 1
        return self._answers[0]


def _wait_until_open(coordinator: WorkerPassCoordinator, number: int) -> None:
    """Wait until ``coordinator`` has published prompt ``number``."""
    assert poll_until(
        lambda: (
            (prompt := coordinator.open_prompt) is not None and prompt.number == number
        ),
        _BUDGET,
    ), f"prompt {number} was never published"


class TestWorkerPassCoordinator:
    """
    The web pass coordinator answers only the open prompt, once.

    A multi-page job asks the same question many times, so each prompt gets a
    fresh claim and a number, and an answer must name the prompt it answers.
    Nothing here sleeps: an unanswered wait has a zero timeout, and an answered
    one is answered as soon as its prompt is published.
    """

    def test_the_coordinator_is_bound_to_one_job(self) -> None:
        """A coordinator carries the id of the job whose prompts it answers."""
        coordinator = WorkerPassCoordinator("job-1", stopping=threading.Event())
        assert coordinator.job_id == "job-1"
        assert coordinator.open_prompt is None
        assert coordinator.claimed is None

    def test_asking_while_stopping_is_interrupted_without_a_prompt(self) -> None:
        """A stop that came first answers the next prompt before anyone sees it."""
        stopping = threading.Event()
        stopping.set()
        coordinator = WorkerPassCoordinator("job-1", stopping=stopping)
        prompt = _prompt(timeout=_OPEN_PROMPT_TIMEOUT)

        started = time.monotonic()
        answer = coordinator.ask(prompt)

        assert answer is PassAnswer.INTERRUPTED
        assert time.monotonic() - started < _OPEN_PROMPT_TIMEOUT / 2
        assert coordinator.open_prompt is None

    def test_stopping_follows_the_worker_stop_flag(self) -> None:
        """The run reads the worker's own flag before it starts another pass."""
        stopping = threading.Event()
        coordinator = WorkerPassCoordinator("job-1", stopping=stopping)
        before = coordinator.stopping

        stopping.set()

        assert (before, coordinator.stopping) == (False, True)

    def test_an_answer_claimed_before_a_stop_keeps_its_meaning(self) -> None:
        """The stop cannot replace a claimed answer; ``stopping`` is what reports it."""
        stopping = threading.Event()
        coordinator = WorkerPassCoordinator("job-1", stopping=stopping)
        prompt = _prompt(timeout=_OPEN_PROMPT_TIMEOUT)

        asker = _Asker(coordinator, prompt)
        _wait_until_open(coordinator, 1)
        assert coordinator.answer(1, PassAnswer.NEXT) is True
        stopping.set()
        assert coordinator.interrupt_for_shutdown() is False

        assert asker.result() is PassAnswer.NEXT
        assert coordinator.stopping is True

    def test_an_unanswered_prompt_times_out(self) -> None:
        """Nobody answering is the clock's answer, recorded as the claim."""
        coordinator = WorkerPassCoordinator("job-1", stopping=threading.Event())
        prompt = _prompt()

        assert coordinator.ask(prompt) is PassAnswer.TIMED_OUT
        assert coordinator.claimed == (prompt, PassAnswer.TIMED_OUT)
        assert coordinator.open_prompt is None

    def test_only_an_offered_answer_to_the_open_prompt_is_claimed(self) -> None:
        """Stale numbers, unoffered answers and second answers are all dropped."""
        coordinator = WorkerPassCoordinator("job-1", stopping=threading.Event())
        offered = frozenset({PassAnswer.NEXT, PassAnswer.RESCAN, PassAnswer.ABORT})
        prompt = _prompt(3, offered=offered, timeout=_OPEN_PROMPT_TIMEOUT)

        asker = _Asker(coordinator, prompt)
        _wait_until_open(coordinator, 3)
        assert coordinator.open_prompt == prompt

        assert coordinator.answer(2, PassAnswer.NEXT) is False
        assert coordinator.answer(3, PassAnswer.FINISH) is False
        assert coordinator.answer(3, PassAnswer.TIMED_OUT) is False
        assert coordinator.answer(3, PassAnswer.INTERRUPTED) is False
        assert coordinator.claimed is None
        assert coordinator.answer(3, PassAnswer.NEXT) is True

        assert asker.result() is PassAnswer.NEXT
        assert coordinator.answer(3, PassAnswer.ABORT) is False
        assert coordinator.claimed == (prompt, PassAnswer.NEXT)
        assert coordinator.open_prompt is None

    def test_a_shutdown_interrupts_the_open_prompt(self) -> None:
        """A server stop answers a waiting prompt with INTERRUPTED at once."""
        coordinator = WorkerPassCoordinator("job-1", stopping=threading.Event())
        prompt = _prompt(timeout=_OPEN_PROMPT_TIMEOUT)

        asker = _Asker(coordinator, prompt)
        _wait_until_open(coordinator, 1)
        started = time.monotonic()
        assert coordinator.interrupt_for_shutdown() is True

        assert asker.result() is PassAnswer.INTERRUPTED
        assert time.monotonic() - started < _OPEN_PROMPT_TIMEOUT / 2
        assert coordinator.claimed == (prompt, PassAnswer.INTERRUPTED)

    def test_a_shutdown_with_no_open_prompt_claims_nothing(self) -> None:
        """Before any prompt, and after one resolved, there is nothing to answer."""
        coordinator = WorkerPassCoordinator("job-1", stopping=threading.Event())
        assert coordinator.interrupt_for_shutdown() is False

        prompt = _prompt()
        assert coordinator.ask(prompt) is PassAnswer.TIMED_OUT
        assert coordinator.interrupt_for_shutdown() is False
        assert coordinator.claimed == (prompt, PassAnswer.TIMED_OUT)

    def test_each_prompt_gets_a_fresh_claim(self) -> None:
        """An answer to prompt 1 never answers prompt 2."""
        coordinator = WorkerPassCoordinator("job-1", stopping=threading.Event())
        first = _prompt(1, timeout=_OPEN_PROMPT_TIMEOUT)
        second = _prompt(2, timeout=_OPEN_PROMPT_TIMEOUT)

        asker = _Asker(coordinator, first)
        _wait_until_open(coordinator, 1)
        assert coordinator.answer(1, PassAnswer.NEXT) is True
        assert asker.result() is PassAnswer.NEXT

        asker = _Asker(coordinator, second)
        _wait_until_open(coordinator, 2)
        assert coordinator.claimed is None
        # A delayed double-click on the first prompt's button.
        assert coordinator.answer(1, PassAnswer.NEXT) is False
        assert coordinator.open_prompt == second
        assert coordinator.answer(2, PassAnswer.FINISH) is True
        assert asker.result() is PassAnswer.FINISH
        assert coordinator.claimed == (second, PassAnswer.FINISH)

    def test_the_next_question_announced_withdraws_the_acknowledgement(self) -> None:
        """
        From the next question's announcement to its prompt, nothing is acknowledged.

        The run persists its next waiting state before it publishes that
        question, so an answer to the previous one must not be shown against
        the new state.  The claim itself stays, for the document count.
        """
        coordinator = WorkerPassCoordinator("job-1", stopping=threading.Event())
        first = _prompt(1, timeout=_OPEN_PROMPT_TIMEOUT)
        asker = _Asker(coordinator, first)
        _wait_until_open(coordinator, 1)
        assert coordinator.answer(1, PassAnswer.NEXT) is True
        assert asker.result() is PassAnswer.NEXT
        before = coordinator.acknowledged

        coordinator.announce_next_question()
        between = coordinator.acknowledged
        claimed = coordinator.claimed
        assert coordinator.ask(_prompt(2)) is PassAnswer.TIMED_OUT
        after = coordinator.acknowledged

        assert (before, between, after) == (
            PassAnswer.NEXT,
            None,
            PassAnswer.TIMED_OUT,
        )
        assert claimed == (first, PassAnswer.NEXT)

    def test_a_second_prompt_unanswered_times_out_on_its_own(self) -> None:
        """A claimed first prompt leaves nothing behind for the second."""
        coordinator = WorkerPassCoordinator("job-1", stopping=threading.Event())
        first = _prompt(1, timeout=_OPEN_PROMPT_TIMEOUT)

        asker = _Asker(coordinator, first)
        _wait_until_open(coordinator, 1)
        assert coordinator.answer(1, PassAnswer.NEXT) is True
        assert asker.result() is PassAnswer.NEXT

        assert coordinator.ask(_prompt(2)) is PassAnswer.TIMED_OUT


class _AnsweredBetweenReads(AnswerSlot[PassAnswer]):
    """
    A slot the operator answers between one read of it and the next.

    The first read of ``answer`` finds the prompt open and every later read
    finds it answered, which is what a click landing mid-read looks like.
    """

    def __init__(self, answer: PassAnswer) -> None:
        """
        Start unread.

        Args:
            answer: What every read after the first returns.

        """
        super().__init__()
        self._late = answer
        self._reads = 0

    @property
    @override
    def answer(self) -> PassAnswer | None:
        """
        Report the prompt open on the first read, and answered afterwards.

        Returns:
            ``None`` the first time; the late answer every time after.

        """
        self._reads += 1
        return None if self._reads == 1 else self._late


class _FakePipeline:
    """
    A ``run_pipeline`` stand-in that announces one wait and asks one prompt.

    Every run records its request.  When the request carries a pass
    coordinator and a prompt was given, the run announces ``event``, waits for
    ``go`` if one was given, asks the prompt, and then holds at ``hold`` if one
    was given, so a test can look at the worker while the job is still live.

    Args:
        prompt: The prompt to ask, or ``None`` to ask nothing.
        event: The event to announce first, or ``None``.
        go: A gate the run waits at before asking, or ``None``.
        hold: A gate the run waits at after asking, or ``None``.

    """

    def __init__(
        self,
        prompt: PassPrompt | None = None,
        *,
        event: PipelineEvent | None = None,
        go: threading.Event | None = None,
        hold: threading.Event | None = None,
    ) -> None:
        """Prepare the run."""
        self._prompt = prompt
        self._event = event
        self._go = go
        self._hold = hold
        self.requests: list[PipelineRequest] = []
        self.answers: list[PassAnswer] = []
        self.ask_seconds: list[float] = []

    def __call__(
        self,
        _scanner: object,
        _paperless: object,
        _settings: object,
        request: PipelineRequest,
    ) -> ScanResult:
        """Record the request, announce, ask, and hold."""
        self.requests.append(request)
        if self._event is not None and request.status_callback is not None:
            request.status_callback(self._event)
        if self._go is not None:
            self._go.wait(_GATE_BOUND)
        coordinator = request.pass_coordinator
        if coordinator is not None and self._prompt is not None:
            started = time.monotonic()
            self.answers.append(coordinator.ask(self._prompt))
            self.ask_seconds.append(time.monotonic() - started)
        if self._hold is not None:
            self._hold.wait(_GATE_BOUND)
        return _success_result()


class _FailingStateWrite:
    """
    ``JobStore.update_state`` that raises on its first calls for one state.

    Every call records the state it was asked to write; the first
    ``failures`` writes of ``state`` raise as a locked database would, and
    every other call reaches the real store.

    Args:
        original: The bound ``update_state`` being replaced.
        state: The state whose writes fail.
        failures: How many of that state's writes fail.

    """

    def __init__(
        self, original: Callable[..., None], state: JobState, failures: int
    ) -> None:
        """Wrap ``original``."""
        self._original = original
        self._state = state
        self._failures = failures
        self._lock = threading.Lock()
        self._seen: list[JobState] = []

    @property
    def seen(self) -> list[JobState]:
        """Every state the worker asked to write so far, in order."""
        with self._lock:
            return list(self._seen)

    def __call__(
        self,
        job_id: str,
        state: JobState,
        error: str | None = None,
        error_category: ErrorCategory | None = None,
    ) -> None:
        """Record the state, then raise or delegate."""
        with self._lock:
            failing = state is self._state and self._seen.count(state) < self._failures
            self._seen.append(state)
        if failing:
            raise sqlite3.OperationalError(_LOCKED)
        self._original(job_id, state, error=error, error_category=error_category)


@pytest.fixture
def running(
    mock_scanner: MagicMock,
    mock_paperless: MagicMock,
    default_settings: Settings,
) -> Iterator[tuple[ScanWorker, JobStore]]:
    """
    Start a worker over the shared mocks, and stop it afterwards.

    Yields:
        The started worker and the store it writes to.

    """
    store = JobStore()
    worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
    worker.start()
    try:
        yield worker, store
    finally:
        worker.stop()
        store.close()


def _finished(store: JobStore, job_id: str) -> Job:
    """Wait for ``job_id`` to end, and return its row."""
    assert poll_until(
        lambda: (
            (job := store.get_job(job_id)) is not None and job.state in TERMINAL_STATES
        ),
        _BUDGET,
    ), f"job {job_id} never finished"
    job = store.get_job(job_id)
    assert job is not None
    return job


@dataclass(frozen=True, slots=True)
class _WaitCase:
    """
    One multi-page wait, as the pipeline announces it and the row records it.

    Attributes:
        event: What the pipeline announces.
        state: The row state it must become.
        wait: The prompt's question.
        offered: The answers the prompt accepts.
        answer: The answer the test gives.
        late: A second, offered answer that must then be dropped.

    """

    event: PipelineEvent
    state: JobState
    wait: PassWait
    offered: frozenset[PassAnswer]
    answer: PassAnswer
    late: PassAnswer


_WAITS = [
    pytest.param(
        _WaitCase(
            PipelineEvent.AWAITING_NEXT_PASS,
            JobState.AWAITING_NEXT_PASS,
            PassWait.NEXT_PASS,
            _NEXT_PASS_OFFERED,
            PassAnswer.NEXT,
            PassAnswer.FINISH,
        ),
        id="next-pass",
    ),
    pytest.param(
        _WaitCase(
            PipelineEvent.AWAITING_BLANK_DECISION,
            JobState.AWAITING_BLANK_DECISION,
            PassWait.BLANK_DECISION,
            _BLANK_OFFERED,
            PassAnswer.SKIP_BLANKS,
            PassAnswer.KEEP_BLANKS,
        ),
        id="blank-decision",
    ),
    pytest.param(
        _WaitCase(
            PipelineEvent.AWAITING_RETRY,
            JobState.AWAITING_RETRY,
            PassWait.RETRY,
            _RETRY_OFFERED,
            PassAnswer.NEXT,
            PassAnswer.ABORT,
        ),
        id="retry",
    ),
]

# A document of three kept pages whose last accepted pass kept one, asked one
# of two questions and answered: the count the busy line should then show.
_KEPT_CASES = [
    pytest.param(PassWait.NEXT_PASS, PassAnswer.NEXT, 3, id="next"),
    pytest.param(
        PassWait.NEXT_PASS, PassAnswer.RESCAN, 2, id="rescan-drops-the-last-pass"
    ),
    pytest.param(
        PassWait.BLANK_DECISION,
        PassAnswer.RESCAN,
        3,
        id="rescan-of-an-unaccepted-pass",
    ),
]


class TestScanWorkerMultiPage:
    """The worker carries the multi-page choice in and each prompt out."""

    def test_the_default_options_are_a_single_pass(self) -> None:
        """Submitting without options is the single-pass scan it always was."""
        assert ScanOptions() == DEFAULT_SCAN_OPTIONS
        assert DEFAULT_SCAN_OPTIONS.multi_page is False

    def test_the_choice_reaches_the_pipeline_request(
        self,
        running: tuple[ScanWorker, JobStore],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A multi-page submit gets a bound coordinator; a plain one gets none."""
        worker, store = running
        fake = _FakePipeline()
        monkeypatch.setattr("saneless.worker.run_pipeline", fake)

        multi = store.create_job("default", "Multi")
        assert worker.submit(multi, ScanOptions(multi_page=True)) is (
            SubmitResult.ACCEPTED
        )
        _finished(store, multi.id)
        plain = store.create_job("default", "Plain")
        assert worker.submit(plain) is SubmitResult.ACCEPTED
        _finished(store, plain.id)

        assert len(fake.requests) == 2
        multi_request, plain_request = fake.requests
        assert multi_request.multi_page is True
        coordinator = multi_request.pass_coordinator
        assert isinstance(coordinator, WorkerPassCoordinator)
        assert coordinator.job_id == multi.id
        assert plain_request.multi_page is False
        assert plain_request.pass_coordinator is None

    @pytest.mark.parametrize("case", _WAITS)
    def test_a_wait_is_persisted_and_its_prompt_answered(
        self,
        case: _WaitCase,
        running: tuple[ScanWorker, JobStore],
    ) -> None:
        """The row shows the wait, and only the open prompt's job may answer it."""
        worker, store = running
        answer, late = case.answer, case.late
        prompt = _prompt(
            wait=case.wait, offered=case.offered, timeout=_OPEN_PROMPT_TIMEOUT
        )
        hold = threading.Event()
        fake = _FakePipeline(prompt, event=case.event, hold=hold)
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr("saneless.worker.run_pipeline", fake)
            try:
                job = store.create_job("default", "Waiting")
                worker.submit(job, ScanOptions(multi_page=True))
                assert poll_until(
                    lambda: worker.pass_prompt(job.id) is not None, _BUDGET
                ), "the prompt was never published"
                row = store.get_job(job.id)
                assert row is not None
                assert row.state is case.state
                assert worker.pass_prompt(job.id) == prompt
                assert worker.pass_prompt("other") is None
                assert worker.pass_answer(job.id) is None

                assert worker.answer_pass("other", 1, answer) is False
                assert worker.answer_pass(job.id, 1, answer) is True
                assert poll_until(lambda: len(fake.answers) == 1, _BUDGET)
                assert worker.pass_answer(job.id) is answer
                assert worker.pass_answer("other") is None
                assert worker.pass_prompt(job.id) is None
                assert worker.answer_pass(job.id, 1, late) is False
            finally:
                hold.set()
            finished = _finished(store, job.id)

        assert fake.answers == [answer]
        assert finished.state is JobState.DONE

    @pytest.mark.parametrize(
        ("failures", "row_state"),
        [(1, JobState.AWAITING_NEXT_PASS), (2, JobState.SCANNING)],
        ids=["once", "twice"],
    )
    def test_a_waiting_state_write_is_tried_twice(
        self,
        failures: int,
        row_state: JobState,
        running: tuple[ScanWorker, JobStore],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The prompt renders from the row, so its write gets one retry, no more."""
        worker, store = running
        updates = _FailingStateWrite(
            store.update_state, JobState.AWAITING_NEXT_PASS, failures
        )
        monkeypatch.setattr(store, "update_state", updates)
        hold = threading.Event()
        fake = _FakePipeline(event=PipelineEvent.AWAITING_NEXT_PASS, hold=hold)
        monkeypatch.setattr("saneless.worker.run_pipeline", fake)
        try:
            job = store.create_job("default", "Locked At A Wait")
            worker.submit(job, ScanOptions(multi_page=True))
            assert poll_until(
                lambda: updates.seen.count(JobState.AWAITING_NEXT_PASS) == 2, _BUDGET
            )
            row = store.get_job(job.id)
            assert row is not None
            reached = row.state
        finally:
            hold.set()
        _finished(store, job.id)

        assert reached is row_state
        assert updates.seen.count(JobState.AWAITING_NEXT_PASS) == 2

    def test_a_stop_interrupts_the_open_prompt_at_once(
        self,
        running: tuple[ScanWorker, JobStore],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stopping answers a waiting prompt rather than waiting its timeout out."""
        worker, store = running
        prompt = _prompt(timeout=_OPEN_PROMPT_TIMEOUT)
        fake = _FakePipeline(prompt, event=PipelineEvent.AWAITING_NEXT_PASS)
        monkeypatch.setattr("saneless.worker.run_pipeline", fake)

        job = store.create_job("default", "Stopped At A Prompt")
        worker.submit(job, ScanOptions(multi_page=True))
        assert poll_until(lambda: worker.pass_prompt(job.id) is not None, _BUDGET)
        assert worker.stop() is True

        assert fake.answers == [PassAnswer.INTERRUPTED]
        assert fake.ask_seconds[0] < _OPEN_PROMPT_TIMEOUT / 2

    def test_a_prompt_asked_after_a_stop_is_interrupted_at_once(
        self,
        running: tuple[ScanWorker, JobStore],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A stop that lands mid-pass leaves no later prompt waiting its timeout."""
        worker, store = running
        prompt = _prompt(timeout=_OPEN_PROMPT_TIMEOUT)
        go = threading.Event()
        fake = _FakePipeline(prompt, event=PipelineEvent.AWAITING_NEXT_PASS, go=go)
        monkeypatch.setattr("saneless.worker.run_pipeline", fake)

        job = store.create_job("default", "Stopped Mid Pass")
        worker.submit(job, ScanOptions(multi_page=True))
        assert poll_until(lambda: len(fake.requests) == 1, _BUDGET)
        stopped: list[bool] = []
        stopper = threading.Thread(target=lambda: stopped.append(worker.stop()))
        stopper.start()
        try:
            assert poll_until(worker._stopping.is_set, _BUDGET)
        finally:
            go.set()
        stopper.join(_OPEN_PROMPT_TIMEOUT * 2)

        assert stopped == [True]
        assert fake.answers == [PassAnswer.INTERRUPTED]
        assert fake.ask_seconds[0] < _OPEN_PROMPT_TIMEOUT / 2

    def test_an_answer_is_not_acknowledged_once_the_next_question_is_announced(
        self,
        running: tuple[ScanWorker, JobStore],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A poll between the next wait's write and its prompt shows no stale answer."""
        worker, store = running
        first = _prompt(timeout=_OPEN_PROMPT_TIMEOUT)
        announced = threading.Event()
        hold = threading.Event()

        def fake(
            _scanner: object,
            _paperless: object,
            _settings: object,
            request: PipelineRequest,
        ) -> ScanResult:
            callback = request.status_callback
            coordinator = request.pass_coordinator
            assert callback is not None
            assert coordinator is not None
            callback(PipelineEvent.AWAITING_NEXT_PASS)
            coordinator.ask(first)
            callback(PipelineEvent.SCANNING)
            callback(PipelineEvent.AWAITING_RETRY)
            announced.set()
            hold.wait(_GATE_BOUND)
            return _success_result()

        monkeypatch.setattr("saneless.worker.run_pipeline", fake)
        try:
            job = store.create_job("default", "Moved On")
            worker.submit(job, ScanOptions(multi_page=True))
            assert poll_until(lambda: worker.pass_prompt(job.id) is not None, _BUDGET)
            assert worker.answer_pass(job.id, 1, PassAnswer.NEXT) is True
            assert announced.wait(_BUDGET)
            row = store.get_job(job.id)
            assert row is not None
            state = row.state
            answer = worker.pass_answer(job.id)
            kept = worker.pages_kept
        finally:
            hold.set()
        _finished(store, job.id)

        assert state is JobState.AWAITING_RETRY
        assert answer is None
        assert kept == 1

    def test_a_finished_job_has_no_prompt_to_answer(
        self,
        running: tuple[ScanWorker, JobStore],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A late click after the job ended answers nothing."""
        worker, store = running
        fake = _FakePipeline(_prompt(), event=PipelineEvent.AWAITING_NEXT_PASS)
        monkeypatch.setattr("saneless.worker.run_pipeline", fake)

        job = store.create_job("default", "Over")
        worker.submit(job, ScanOptions(multi_page=True))
        _finished(store, job.id)
        assert poll_until(lambda: worker.current_job_id is None, _BUDGET)

        assert fake.answers == [PassAnswer.TIMED_OUT]
        assert worker.pass_prompt(job.id) is None
        assert worker.pass_answer(job.id) is None
        assert worker.answer_pass(job.id, 1, PassAnswer.NEXT) is False
        assert worker.pages_kept is None

    @pytest.mark.parametrize(("wait", "answer", "expected"), _KEPT_CASES)
    def test_pages_kept_counts_the_document_while_a_pass_scans(
        self,
        wait: PassWait,
        answer: PassAnswer,
        expected: int,
        running: tuple[ScanWorker, JobStore],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A Re-scan of the last accepted pass takes its pages off the count."""
        worker, store = running
        prompt = PassPrompt(
            number=1,
            wait=wait,
            pages_kept=3,
            offered=frozenset({PassAnswer.NEXT, PassAnswer.RESCAN}),
            timeout_seconds=_OPEN_PROMPT_TIMEOUT,
            last_pass_pages=1,
            last_pass_kept=1,
        )
        hold = threading.Event()
        fake = _FakePipeline(prompt, hold=hold)
        monkeypatch.setattr("saneless.worker.run_pipeline", fake)
        try:
            job = store.create_job("default", "Counting")
            worker.submit(job, ScanOptions(multi_page=True))
            assert poll_until(lambda: worker.pass_prompt(job.id) is not None, _BUDGET)
            assert worker.answer_pass(job.id, 1, answer) is True
            kept = worker.pages_kept
        finally:
            hold.set()
        _finished(store, job.id)

        assert kept == expected

    def test_pages_kept_reads_the_prompt_and_its_answer_once(
        self, running: tuple[ScanWorker, JobStore]
    ) -> None:
        """
        A prompt answered mid-read still reports its own count, never zero.

        Reading the claim and the open prompt separately could find the prompt
        open in the first read and answered in the second, and so neither.
        """
        worker, _store = running
        coordinator = WorkerPassCoordinator("job-1", stopping=threading.Event())
        coordinator._prompt = PassPrompt(
            number=2,
            wait=PassWait.NEXT_PASS,
            pages_kept=3,
            offered=_NEXT_PASS_OFFERED,
            timeout_seconds=_OPEN_PROMPT_TIMEOUT,
            last_pass_pages=1,
            last_pass_kept=1,
        )
        coordinator._slot = _AnsweredBetweenReads(PassAnswer.NEXT)
        worker._pass_coordinator = coordinator
        try:
            kept = worker.pages_kept
        finally:
            worker._pass_coordinator = None

        assert kept == 3

    def test_pages_kept_is_none_without_a_multi_page_job(
        self,
        running: tuple[ScanWorker, JobStore],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An idle worker and a single-pass job have no document count."""
        worker, store = running
        assert worker.pages_kept is None
        hold = threading.Event()
        fake = _FakePipeline(hold=hold)
        monkeypatch.setattr("saneless.worker.run_pipeline", fake)
        try:
            job = store.create_job("default", "Single Pass")
            worker.submit(job)
            assert poll_until(lambda: len(fake.requests) == 1, _BUDGET)
            kept = worker.pages_kept
        finally:
            hold.set()
        _finished(store, job.id)

        assert kept is None


# A second question's timeout, longer than the first's by far more than the
# time between the two asks, so its deadline is later whatever the clock does.
_LONGER_PROMPT_TIMEOUT = _OPEN_PROMPT_TIMEOUT * 4


class TestPassDeadline:
    """
    An open multi-page question reports when it times out.

    The deadline is the moment the question was asked plus the prompt's own
    timeout, and it is reported for the running multi-page job only.
    """

    def test_no_deadline_before_a_question(
        self,
        running: tuple[ScanWorker, JobStore],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A job that has announced its wait but not asked yet has no deadline."""
        worker, store = running
        assert worker.pass_deadline("no-such-job") is None
        go = threading.Event()
        prompt = _prompt(timeout=_OPEN_PROMPT_TIMEOUT)
        fake = _FakePipeline(prompt, event=PipelineEvent.AWAITING_NEXT_PASS, go=go)
        monkeypatch.setattr("saneless.worker.run_pipeline", fake)
        try:
            job = store.create_job("default", "Not Asked Yet")
            worker.submit(job, ScanOptions(multi_page=True))
            assert poll_until(
                lambda: (
                    (row := store.get_job(job.id)) is not None
                    and row.state is JobState.AWAITING_NEXT_PASS
                ),
                _BUDGET,
            ), "the wait was never announced"
            before_asking = worker.pass_deadline(job.id)

            go.set()
            assert poll_until(lambda: worker.pass_prompt(job.id) is not None, _BUDGET)
            once_asked = worker.pass_deadline(job.id)
            assert worker.answer_pass(job.id, 1, PassAnswer.NEXT) is True
        finally:
            go.set()
        _finished(store, job.id)

        assert before_asking is None
        assert once_asked is not None

    def test_deadline_is_asked_at_plus_the_prompt_timeout(
        self,
        running: tuple[ScanWorker, JobStore],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The deadline is the ask's UTC time plus the prompt's timeout."""
        worker, store = running
        prompt = _prompt(timeout=_OPEN_PROMPT_TIMEOUT)
        hold = threading.Event()
        fake = _FakePipeline(prompt, event=PipelineEvent.AWAITING_NEXT_PASS, hold=hold)
        monkeypatch.setattr("saneless.worker.run_pipeline", fake)
        try:
            job = store.create_job("default", "Asked")
            before = datetime.now(tz=UTC)
            worker.submit(job, ScanOptions(multi_page=True))
            assert poll_until(lambda: worker.pass_prompt(job.id) is not None, _BUDGET)
            after = datetime.now(tz=UTC)
            deadline = worker.pass_deadline(job.id)

            assert worker.answer_pass(job.id, 1, PassAnswer.NEXT) is True
            assert poll_until(lambda: len(fake.answers) == 1, _BUDGET)
            answered = worker.pass_deadline(job.id)
        finally:
            hold.set()
        _finished(store, job.id)

        assert deadline is not None
        asked_at = deadline - timedelta(seconds=_OPEN_PROMPT_TIMEOUT)
        assert asked_at.tzinfo is UTC
        assert before <= asked_at <= after
        assert answered is None

    def test_a_new_question_moves_the_deadline(self) -> None:
        """Each question is timed from its own ask, with its own timeout."""
        coordinator = WorkerPassCoordinator("job-1", stopping=threading.Event())
        assert coordinator.open_deadline is None

        asker = _Asker(coordinator, _prompt(1, timeout=_OPEN_PROMPT_TIMEOUT))
        _wait_until_open(coordinator, 1)
        first = coordinator.open_deadline
        assert coordinator.answer(1, PassAnswer.NEXT) is True
        assert asker.result() is PassAnswer.NEXT
        between = coordinator.open_deadline

        before = datetime.now(tz=UTC)
        asker = _Asker(coordinator, _prompt(2, timeout=_LONGER_PROMPT_TIMEOUT))
        _wait_until_open(coordinator, 2)
        after = datetime.now(tz=UTC)
        second = coordinator.open_deadline
        assert coordinator.answer(2, PassAnswer.FINISH) is True
        assert asker.result() is PassAnswer.FINISH

        assert first is not None
        assert between is None
        assert second is not None
        asked_at = second - timedelta(seconds=_LONGER_PROMPT_TIMEOUT)
        assert before <= asked_at <= after
        assert second - first >= timedelta(
            seconds=_LONGER_PROMPT_TIMEOUT - _OPEN_PROMPT_TIMEOUT
        )

    def test_no_deadline_for_another_job(
        self,
        running: tuple[ScanWorker, JobStore],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Only the running multi-page job's open question has a deadline."""
        worker, store = running
        prompt = _prompt(timeout=_OPEN_PROMPT_TIMEOUT)
        hold = threading.Event()
        fake = _FakePipeline(prompt, event=PipelineEvent.AWAITING_NEXT_PASS, hold=hold)
        monkeypatch.setattr("saneless.worker.run_pipeline", fake)
        try:
            job = store.create_job("default", "Someone Else's")
            worker.submit(job, ScanOptions(multi_page=True))
            assert poll_until(lambda: worker.pass_prompt(job.id) is not None, _BUDGET)
            own = worker.pass_deadline(job.id)
            other = worker.pass_deadline("other-id")
            assert worker.answer_pass(job.id, 1, PassAnswer.NEXT) is True
        finally:
            hold.set()
        _finished(store, job.id)

        assert own is not None
        assert other is None
