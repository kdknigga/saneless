"""
End-to-end proof of Phase 23: the real worker driving the real pipeline.

What makes this module different from every other test file in the repository:
it is the only one that runs the real :func:`saneless.pipeline.run_pipeline`
inside the real :class:`saneless.worker.ScanWorker`.  All 49 ``ScanWorker(...)``
construction sites in ``tests/test_worker.py`` replace the pipeline entry
point in the worker's own module namespace, which makes them tests of the
worker's *handling* of a result rather than tests of the result.  This module
patches run_pipeline nowhere, and an acceptance grep enforces that the
patch target's dotted name does not appear below.

Exactly two things are stubbed, and nothing else:

* **the scanner** -- a ``MagicMock(spec=ScannerBackend)`` handing back PIL
  images, because there is no SANE device in CI;
* **the HTTP layer** -- an ``httpx.MockTransport`` passed through
  ``PaperlessClient(..., _transport=...)``, the seam ``tests/test_paperless.py``
  already uses 24 times.  It sits *below* ``httpx.Client``, so the real
  ``upload_document`` and ``poll_task`` bodies execute, retries and all.

Everything else is production code: real PDF assembly, real empty-page
filtering, real preservation into ``<data_dir>/failed/``, a real file-backed
SQLite job store, and a real background thread.  Every assertion reads the
**persisted job row** through ``store.get_job`` rather than a mock's call list,
because a row in the database is the thing a user's web page and ``saneless
jobs`` actually render (T-23-38).

**On speed.**  All the cases together sleep for well under a second, using
only seams that already exist:

* ``max_retries=1`` makes both exponential-backoff pauses in
  ``upload_document`` unreachable (measured 3.00 s at 3 retries, 1.00 s at 2,
  0.00 s at 1).  The consume-directory case is the one that needs it.
* ``paperless_task_timeout`` is 0 for the poll-timeout case, so ``poll_task``'s
  monotonic deadline has already passed when the first poll comes back without
  a terminal status.  See ``_TIMEOUT_BUDGET`` for why it is 0 and not 0.05.
* ``flip_timeout_seconds`` is 0 for the flip-timeout case, so the flip wait
  resolves in microseconds.  See ``_FLIP_TIMEOUT_BUDGET``.
* The other cases reach a terminal status, or fall back, on the first
  request, before any sleep, and cost nothing.

The sleep primitive itself is **not** patched anywhere here, and no flat
pause appears in this module -- an acceptance grep enforces both.  Patching it
in ``saneless.paperless`` would disable the ``min(delay, remaining)`` deadline
clamp that plan 23-04 added, which is part of what these cases prove; Phase 32
(M-34) owns that sweep.  No ``sleep_fn`` parameter and no configurable backoff
base were added either: those would be production seams existing only for
tests, for a problem two existing constructor parameters already solve.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from unittest.mock import MagicMock

import httpx
import pytest
from PIL import Image, ImageDraw

from saneless.config import (
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
)
from saneless.job import JobStore
from saneless.paperless import PaperlessClient
from saneless.scanner.base import ScannerBackend
from saneless.vocabulary import TERMINAL_STATES, JobState, ScanOutcome
from saneless.worker import ScanWorker
from tests.conftest import scan_batch

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from saneless.job import Job

_DOCUMENTS_PATH = "/api/documents/post_document/"
_TASKS_PATH = "/api/tasks/"

_DEVICE = "test:device:001"
_TITLE = "Quarterly Report"

# The scans run under a profile that is deliberately NOT the one named
# "default".  auto_profiles.is_bare_default matches a profile set of exactly
# one untouched profile called "default", and on that match the worker tries to
# generate profiles from the stub scanner and write them to whatever
# saneless.toml resolve_config_path() finds on the machine running the suite.
# Settings requires a "default" profile to exist, so it is defined alongside
# this one: two entries make is_bare_default return on its length check and
# _maybe_auto_generate return on its first line.
_PROFILE = "e2e"

_SIMPLEX_SOURCE = "Flatbed"
# A manual-duplex profile names a real feeder source and says ``duplex =
# "manual"``: source is a pure SANE value and no longer selects the strategy.
_DUPLEX_SOURCE = "ADF"
# The deprecated one-key form, kept as its own case so DPLX-02's "still loads
# AND scans" is proven through the real pipeline, not only at config load.
_LEGACY_DUPLEX_SOURCE = "ADF Manual Duplex"

# The shipped default, used by every case that does not time out a poll: their first
# poll is terminal, so the value never actually elapses and production's own
# number is the honest thing to run with.
_PRODUCTION_TIMEOUT = 300

# Zero, not the 0.05 s the plan suggested.  OutputConfig.paperless_task_timeout
# is typed ``int``, so pydantic rejects 0.05 outright (a float with a fractional
# part is not a lax-mode int) and assigning it afterwards would be a type lie
# that ty and pyrefly are right to reject.  Widening the production field to
# float purely for a test was not worth it.  Zero costs no wall clock at all and
# proves the same property end to end: poll_task computes
# ``deadline = monotonic() + timeout``, issues its first poll, sees no terminal
# status, finds no time remaining and raises PaperlessTimeoutError -- which is
# the raise this case exists to follow into the preservation guard.  The
# ``min(delay, remaining)`` backoff clamp itself is proven by
# ``tests/test_paperless.py``'s dedicated wall-clock test at the unit level.
_TIMEOUT_BUDGET = 0

# The shipped flip wait, for every case whose operator answers the prompt: the
# answer arrives first, so the bound never elapses.
_PRODUCTION_FLIP_TIMEOUT = 600

# Zero, for the same reasons as _TIMEOUT_BUDGET.  flip_timeout_seconds is typed
# ``int``, so pydantic rejects a fractional float like 0.05 outright, and
# assigning one afterwards would be a type lie that ty and pyrefly are right to
# reject.  Zero costs no wall clock -- ``threading.Event().wait(0)`` was
# measured at 4 microseconds -- and proves the same property end to end:
# nothing answered within the bound, so the coordinator resolves TIMED_OUT and
# the pipeline raises before pass B.
_FLIP_TIMEOUT_BUDGET = 0

_FAILURE_TASK = "e2e-task-failure"
_PENDING_TASK = "e2e-task-never-finishes"
_PAPERLESS_MESSAGE = "Document consumption failed: unsupported PDF producer"


def _unexpected(request: httpx.Request) -> httpx.Response:
    """
    Answer a request no case expected, in a way that names itself.

    A silently-tolerated stray request is how an end-to-end test stops being
    one.  Answering non-200 turns a mis-dispatch into a PaperlessError quoting
    the method and path, instead of letting the run drift into a timeout whose
    message says nothing about the real mistake.

    Args:
        request: The request that reached a handler which was not expecting it.

    Returns:
        A 418 whose body names the offending method and path.

    """
    return httpx.Response(
        418,
        text=(
            f"the end-to-end transport was not expecting "
            f"{request.method} {request.url.path}"
        ),
    )


def _v9_tasks(task_id: str, status: str) -> list[dict[str, object]]:
    """
    Build a paperless-ngx API **v9** ``/api/tasks/`` body.

    v9 answers with a bare list and spells its statuses in uppercase.

    Args:
        task_id: The task the client asked about.
        status: The uppercase status to report.

    Returns:
        The decoded body, ready to hand to ``httpx.Response(json=...)``.

    """
    return [{"task_id": task_id, "status": status}]


def _v10_tasks(task_id: str, status: str, error_message: str) -> dict[str, object]:
    """
    Build a paperless-ngx API **v10** ``/api/tasks/`` body.

    v10 paginates the list into ``{"count", "next", "previous", "results"}``,
    spells its statuses in lowercase, and carries the failure text in
    ``result_data["error_message"]`` rather than a flat ``result`` string.
    OUTC-11's tolerance for all three differences is exercised at the unit
    level; this is the e2e layer proving it against the real pipeline.

    Args:
        task_id: The task the client asked about.
        status: The lowercase status to report.
        error_message: The failure text, nested where v10 puts it.

    Returns:
        The decoded body, ready to hand to ``httpx.Response(json=...)``.

    """
    task: dict[str, object] = {
        "task_id": task_id,
        "status": status,
        "result_data": {"error_message": error_message},
    }
    return {"count": 1, "next": None, "previous": None, "results": [task]}


def _accepting_handler() -> Callable[[httpx.Request], httpx.Response]:
    """
    Accept every upload and report SUCCESS on the first poll, in the v9 shape.

    Each upload is issued its own task id and the poll refuses to recognise an
    id it never handed out, so the duplex case -- which uploads twice -- cannot
    pass by polling the same task twice or by polling a task that was never
    created.

    Returns:
        A fresh handler with its own issued-task list, so no state leaks
        between parametrised cases.

    """
    issued: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _DOCUMENTS_PATH:
            task_id = f"e2e-task-{len(issued) + 1}"
            issued.append(task_id)
            return httpx.Response(200, json=task_id)
        if request.url.path == _TASKS_PATH:
            polled = str(request.url.params.get("task_id", ""))
            if polled not in issued:
                return _unexpected(request)
            return httpx.Response(200, json=_v9_tasks(polled, "SUCCESS"))
        return _unexpected(request)

    return handler


def _paperless_failure_handler() -> Callable[[httpx.Request], httpx.Response]:
    """
    Accept the upload, then report that paperless-ngx refused the document.

    This is the case that runs the **v10** wire shape end to end: a paginated
    body, a lowercase ``"failure"``, and the message under
    ``result_data["error_message"]``.

    Returns:
        A handler answering the upload with a task id and every poll with a
        terminal failure.

    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _DOCUMENTS_PATH:
            return httpx.Response(200, json=_FAILURE_TASK)
        if request.url.path == _TASKS_PATH:
            return httpx.Response(
                200,
                json=_v10_tasks(_FAILURE_TASK, "failure", _PAPERLESS_MESSAGE),
            )
        return _unexpected(request)

    return handler


def _never_finishing_handler() -> Callable[[httpx.Request], httpx.Response]:
    """
    Accept the upload, then answer every poll with an empty v10 page.

    A 200 carrying no task is deliberately *not* an error -- a task is not
    always visible immediately after the upload that created it -- so this
    drives the client all the way to its deadline rather than to an early
    raise.

    Returns:
        A handler that accepts once and then stalls forever, politely.

    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _DOCUMENTS_PATH:
            return httpx.Response(200, json=_PENDING_TASK)
        if request.url.path == _TASKS_PATH:
            return httpx.Response(
                200,
                json={"count": 0, "next": None, "previous": None, "results": []},
            )
        return _unexpected(request)

    return handler


def _server_error_handler() -> Callable[[httpx.Request], httpx.Response]:
    """
    Refuse every upload with a 500, so the consume directory is the only route.

    A poll reaching this handler would mean the fallback was not taken and the
    client invented a task id, so the tasks path is left to ``_unexpected``.

    Returns:
        A handler answering the upload path with a retryable server error.

    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _DOCUMENTS_PATH:
            return httpx.Response(500, text="paperless-ngx is restarting")
        return _unexpected(request)

    return handler


@dataclass(frozen=True)
class _Case:
    """
    One end-to-end outcome: how the world is built, and how it must end.

    A record rather than a tuple because heterogeneous cases with a dozen
    fields each are unreadable positionally, and because ``label`` doubles as
    the pytest test id.

    Attributes:
        label: The pytest id, and the name a failure reports.
        handler_factory: Builds this case's MockTransport handler.  A factory,
            not a handler, so per-case counters start fresh every run.
        expected_state: The persisted JobState the run must end in.
        expected_outcome: The persisted ScanOutcome, or None when the run
            failed -- D-01: a failure raises, so the column stays NULL rather
            than claiming a measurement nobody made.
        expected_pages: (scanned, removed, uploaded), all None on a failure.
        expected_failed_pdfs: How many PDFs must survive in <data_dir>/failed/.
        expected_consume_pdfs: How many files must sit in the consume directory.
        source: The profile's scan source, handed to the device verbatim.
        duplex: The profile's ``duplex`` key; ``"manual"`` takes the two-pass
            path.  None omits the key entirely, which is what lets the legacy
            ``source`` form be translated at load -- an explicit value, even
            ``"none"``, is never overwritten by that inference.
        scan_passes: Page count per scan_pages call.  Two entries means two
            passes, and two different numbers means a duplex mismatch.
        task_timeout: settings.output.paperless_task_timeout for this case.
        flip_timeout: settings.output.flip_timeout_seconds for this case.
        with_consume_dir: Whether the client is given a consume directory.
        awaits_flip: Whether the run parks in AWAITING_FLIP at all.
        operator_flips: Whether the test answers the flip prompt with
            Continue.  False leaves the wait to run out, which is the whole
            point of the flip-timeout case.
        warning_contains: A fragment the persisted warning must contain; None
            means the warning column must be NULL.
        error_contains: Fragments the persisted error must contain; empty means
            the error column must be NULL.

    """

    label: str
    handler_factory: Callable[[], Callable[[httpx.Request], httpx.Response]]
    expected_state: JobState
    expected_outcome: ScanOutcome | None
    expected_pages: tuple[int | None, int | None, int | None]
    expected_failed_pdfs: int
    expected_consume_pdfs: int
    source: str = _SIMPLEX_SOURCE
    duplex: Literal["none", "hardware", "manual"] | None = None
    scan_passes: tuple[int, ...] = (2,)
    task_timeout: int = _PRODUCTION_TIMEOUT
    flip_timeout: int = _PRODUCTION_FLIP_TIMEOUT
    with_consume_dir: bool = False
    awaits_flip: bool = False
    operator_flips: bool = True
    warning_contains: str | None = None
    error_contains: tuple[str, ...] = ()


_CASES = [
    _Case(
        label="success",
        handler_factory=_accepting_handler,
        expected_state=JobState.DONE,
        expected_outcome=ScanOutcome.SUCCESS,
        expected_pages=(2, 0, 2),
        expected_failed_pdfs=0,
        expected_consume_pdfs=0,
    ),
    _Case(
        label="paperless-failure",
        handler_factory=_paperless_failure_handler,
        expected_state=JobState.ERROR,
        expected_outcome=None,
        expected_pages=(None, None, None),
        expected_failed_pdfs=1,
        expected_consume_pdfs=0,
        error_contains=(_FAILURE_TASK, "FAILURE", _PAPERLESS_MESSAGE),
    ),
    _Case(
        label="poll-timeout",
        handler_factory=_never_finishing_handler,
        expected_state=JobState.ERROR,
        expected_outcome=None,
        expected_pages=(None, None, None),
        expected_failed_pdfs=1,
        expected_consume_pdfs=0,
        task_timeout=_TIMEOUT_BUDGET,
        error_contains=(_PENDING_TASK, "did not finish"),
    ),
    _Case(
        label="consume-dir-fallback",
        handler_factory=_server_error_handler,
        expected_state=JobState.FALLBACK,
        expected_outcome=ScanOutcome.FALLBACK,
        expected_pages=(2, 0, 2),
        expected_failed_pdfs=0,
        expected_consume_pdfs=1,
        with_consume_dir=True,
        warning_contains="title, tags and correspondent",
    ),
    _Case(
        label="duplex-mismatch",
        handler_factory=_accepting_handler,
        expected_state=JobState.DONE,
        expected_outcome=ScanOutcome.SUCCESS,
        expected_pages=(5, 0, 5),
        expected_failed_pdfs=0,
        expected_consume_pdfs=0,
        source=_DUPLEX_SOURCE,
        duplex="manual",
        scan_passes=(3, 2),
        awaits_flip=True,
        warning_contains="Page count mismatch: 3 fronts, 2 backs",
    ),
    _Case(
        label="legacy-duplex-source",
        handler_factory=_accepting_handler,
        expected_state=JobState.DONE,
        expected_outcome=ScanOutcome.SUCCESS,
        expected_pages=(4, 0, 4),
        expected_failed_pdfs=0,
        expected_consume_pdfs=0,
        source=_LEGACY_DUPLEX_SOURCE,
        scan_passes=(2, 2),
        awaits_flip=True,
    ),
    _Case(
        label="flip-timeout",
        handler_factory=_accepting_handler,
        expected_state=JobState.ERROR,
        expected_outcome=None,
        expected_pages=(None, None, None),
        # The timeout raises before pass B and before assembly, so there is no
        # PDF to preserve: Phase 23's guard spans upload and poll only.
        expected_failed_pdfs=0,
        expected_consume_pdfs=0,
        source=_DUPLEX_SOURCE,
        duplex="manual",
        scan_passes=(3, 3),
        flip_timeout=_FLIP_TIMEOUT_BUDGET,
        awaits_flip=True,
        operator_flips=False,
        error_contains=("flip wait timed out", "nobody confirmed"),
    ),
]


def _pages(count: int) -> list[Image.Image]:
    """
    Draw pages with enough ink that the real empty-page filter keeps them.

    Empty-page detection is left enabled, as it is by default in production, so
    the pages have to be genuinely non-blank or ``_drop_empty_pages`` raises
    "All pages were detected as empty" and every case fails for the wrong
    reason.

    Args:
        count: How many pages to produce.

    Returns:
        A list of distinct, clearly non-empty RGB images.

    """
    pages: list[Image.Image] = []
    for index in range(count):
        page = Image.new("RGB", (120, 160), "white")
        draw = ImageDraw.Draw(page)
        draw.rectangle((10, 10, 110, 40 + index * 10), fill="black")
        pages.append(page)
    return pages


def _build_scanner(scan_passes: tuple[int, ...]) -> MagicMock:
    """
    Stub the scanner, and only the scanner.

    ``side_effect`` is a list of iterators, one per pass, so a manual-duplex
    run gets a different page count from each of its two ``scan_pages`` calls
    and a simplex run gets exactly one.  A second call on a simplex case would
    raise StopIteration rather than silently rescanning.

    The flip coordination is not the scanner's job -- ``_scan_manual_duplex``
    waits on the worker's flip coordinator, and the test answers it from the
    main thread once the job is observed parked in AWAITING_FLIP (or, for the
    flip-timeout case, deliberately never answers it).

    Args:
        scan_passes: Page count for each successive scan_pages call.

    Returns:
        A MagicMock constrained to the ScannerBackend interface.

    """
    scanner = MagicMock(spec=ScannerBackend)
    scanner.scan_pages.side_effect = [
        scan_batch(_pages(count)) for count in scan_passes
    ]
    return scanner


def _build_settings(tmp_path: Path, case: _Case) -> Settings:
    """
    Build Settings whose directories are three separate subtrees of tmp_path.

    ``failed_dir`` and ``db_path`` both derive from ``data_dir``, so putting
    ``data_dir`` under ``tmp_dir`` would make a correctly preserved scan
    indistinguishable from a leaked temporary directory, and would trip the
    temp-cleanup assertions the pipeline tests rely on (T-23-40).  No absolute
    path outside ``tmp_path`` appears anywhere in this module.

    Args:
        tmp_path: pytest's per-test directory.
        case: The case being built, for its source, duplex mode and timeouts.

    Returns:
        Settings pointing at scratch, durable state and consume directories
        that cannot collide.

    """
    data_dir = tmp_path / "data"
    # sqlite3.connect does not create parent directories and db_path creates
    # nothing, so the store would fail to open without this.
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        scanner=ScannerConfig(device=_DEVICE),
        paperless=PaperlessConfig(
            url="http://paperless.invalid:8000",
            token="e2e-token",
            consume_dir=str(tmp_path / "consume") if case.with_consume_dir else "",
        ),
        output=OutputConfig(
            tmp_dir=str(tmp_path / "scratch"),
            data_dir=str(data_dir),
            log_file=str(tmp_path / "logs" / "saneless.log"),
            paperless_task_timeout=case.task_timeout,
            flip_timeout_seconds=case.flip_timeout,
        ),
        profiles={
            # Settings' field validator requires this one.  Only the worker
            # release test runs a job under it; its presence alongside _PROFILE
            # is what keeps is_bare_default False.
            "default": ProfileConfig(),
            _PROFILE: _build_profile(case),
        },
    )


def _build_profile(case: _Case) -> ProfileConfig:
    """
    Build the case's profile, writing ``duplex`` only when the case sets it.

    Omitting the key is not the same as ``duplex = "none"``: the legacy
    ``source`` translation runs only when the key is absent, so the legacy case
    must leave it out to exercise that path at all.

    Args:
        case: The case whose source and duplex mode to use.

    Returns:
        The profile the case scans under.

    """
    if case.duplex is None:
        return ProfileConfig(source=case.source)
    return ProfileConfig(source=case.source, duplex=case.duplex)


def _assert_persisted_row(case: _Case, job: Job) -> None:
    """
    Assert the four facts the database must carry about a finished job.

    Args:
        case: The expectations for this run.
        job: The job row as re-read from the store after the worker finished.

    """
    assert job.state is case.expected_state
    assert job.outcome is case.expected_outcome
    assert (
        job.pages_scanned,
        job.pages_removed,
        job.pages_uploaded,
    ) == case.expected_pages

    if case.warning_contains is None:
        assert job.warning is None
    else:
        assert job.warning is not None
        assert case.warning_contains in job.warning


def _assert_files(case: _Case, job: Job, failed_dir: Path, consume_dir: Path) -> None:
    """
    Assert what survived on a real filesystem, and what the job says about it.

    Args:
        case: The expectations for this run.
        job: The finished job row, for its error message.
        failed_dir: ``<data_dir>/failed/``, created only by a preservation.
        consume_dir: The paperless-ngx consume directory, created only by a
            fallback.

    """
    preserved = sorted(failed_dir.glob("*.pdf")) if failed_dir.exists() else []
    assert len(preserved) == case.expected_failed_pdfs

    if case.error_contains:
        assert job.error is not None
        for fragment in case.error_contains:
            assert fragment in job.error
        # OUTC-04: the error has to name the file the operator must go and
        # find, not merely say that something was kept somewhere.
        for pdf in preserved:
            assert pdf.name in job.error
    else:
        assert job.error is None

    delivered = sorted(consume_dir.iterdir()) if consume_dir.exists() else []
    assert len(delivered) == case.expected_consume_pdfs
    # OUTC-05: the staged dotfile must have been renamed into place, not left
    # behind for paperless-ngx's inotify watcher to trip over.
    assert all(item.suffix == ".pdf" for item in delivered)
    assert not any(item.name.startswith(".") for item in delivered)


class TestFiveOutcomesEndToEnd:
    """The real worker and the real pipeline, through every outcome case."""

    @pytest.mark.parametrize("case", _CASES, ids=[case.label for case in _CASES])
    def test_outcome_is_persisted(
        self,
        case: _Case,
        tmp_path: Path,
        wait_for_state: Callable[..., Job],
    ) -> None:
        """Drive one scan end to end and assert the row it left behind."""
        settings = _build_settings(tmp_path, case)
        consume_dir = tmp_path / "consume"
        store = JobStore(db_path=str(settings.output.db_path))
        paperless = PaperlessClient(
            url=settings.paperless.url,
            token=settings.paperless.token,
            consume_dir=settings.paperless.consume_dir,
            # The measured zero-sleep lever: at 1 attempt neither
            # exponential-backoff pause in upload_document is reachable, taking
            # the fallback case from 3.00 s to 0.00 s.  The fallback behaviour
            # is identical at any retry count -- exhausting them is what
            # triggers it.
            max_retries=1,
            _transport=httpx.MockTransport(case.handler_factory()),
        )
        worker = ScanWorker(
            _build_scanner(case.scan_passes),
            paperless,
            settings,
            store,
        )
        try:
            worker.start()
            job = store.create_job(_PROFILE, _TITLE)
            worker.submit(job)
            if case.awaits_flip and case.operator_flips:
                # The persisted AWAITING_FLIP is observed first because the
                # coordinator only accepts an answer once armed, and the worker
                # arms it as it announces AWAITING_FLIP: a Continue sent any
                # earlier is dropped, not queued (CR-01).
                wait_for_state(store, job.id, JobState.AWAITING_FLIP, 2.0)
                worker.continue_flip(job.id)
            # A 2 s budget, far below pytest-timeout's 60 s SIGALRM.  The
            # signal method delivers to the MAIN thread whichever thread is
            # stuck, so letting this run to the global ceiling would print a
            # traceback for this wait loop rather than for the stuck worker.
            finished = wait_for_state(store, job.id, TERMINAL_STATES, 2.0)
        finally:
            worker.stop()
            paperless.close()
            store.close()

        _assert_persisted_row(case, finished)
        _assert_files(case, finished, settings.output.failed_dir, consume_dir)


class TestFlipTimeoutReleasesTheWorker:
    """A flip wait that runs out fails its job and frees the worker (DPLX-05)."""

    def test_the_next_job_runs_after_a_flip_timeout(
        self,
        tmp_path: Path,
        wait_for_state: Callable[..., Job],
    ) -> None:
        """
        After a timed-out flip, a second submitted job still reaches terminal.

        This is roadmap criterion 3's "releases the scanner for the next job",
        and what that phrase does and does not mean matters.  ``scan_pages``
        opens and closes the device on every call, so the device *handle* is
        already released between pass A and pass B whether or not anyone ever
        flips.  What an unbounded wait actually held was the single worker
        thread, queueing every later job behind a forgotten prompt -- M-07's
        real complaint.  So the proof is a second job getting through, not a
        handle being closed.
        """
        case = next(case for case in _CASES if case.label == "flip-timeout")
        settings = _build_settings(tmp_path, case)
        store = JobStore(db_path=str(settings.output.db_path))
        paperless = PaperlessClient(
            url=settings.paperless.url,
            token=settings.paperless.token,
            consume_dir=settings.paperless.consume_dir,
            max_retries=1,
            _transport=httpx.MockTransport(_accepting_handler()),
        )
        # Pass A of the timed-out job, then the single pass of the simplex job
        # that follows it.  No third batch: the timed-out job must never reach
        # pass B, and if it did the simplex job would find the stub exhausted.
        worker = ScanWorker(_build_scanner((3, 2)), paperless, settings, store)
        try:
            worker.start()
            parked = store.create_job(_PROFILE, "Forgotten Flip")
            worker.submit(parked)
            # Nothing signals the flip.  The 2 s budget is far below
            # pytest-timeout's 60 s SIGALRM, which lands on the main thread.
            timed_out = wait_for_state(store, parked.id, TERMINAL_STATES, 2.0)

            # The simplex "default" profile, so the follow-up job needs no flip
            # of its own and can only be held up by the worker being stuck.
            follow_up = store.create_job("default", "Next In Line")
            worker.submit(follow_up)
            finished = wait_for_state(store, follow_up.id, TERMINAL_STATES, 2.0)
        finally:
            worker.stop()
            paperless.close()
            store.close()

        assert timed_out.state is JobState.ERROR
        assert timed_out.error is not None
        assert "flip wait timed out" in timed_out.error
        assert finished.state is JobState.DONE
        assert finished.pages_scanned == 2
