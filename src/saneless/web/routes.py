"""HTTP route handlers for the saneless web UI."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Literal, assert_never

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from saneless.vocabulary import (
    QUEUE_FULL_JOB_ERROR,
    TITLE_MAX_LENGTH,
    WORKER_DEGRADED_JOB_ERROR,
    WORKER_DOWN_JOB_ERROR,
    ErrorCategory,
    FlipOutcome,
    JobState,
    RequestRejection,
    SubmitResult,
    WorkerHealth,
    worker_health_detail,
)
from saneless.web.errors import RequestRejected

if TYPE_CHECKING:
    from starlette.responses import Response

    from saneless.job import Job, JobStore
    from saneless.paperless import PaperlessClient
    from saneless.web.cache import MetadataCache
    from saneless.worker import ScanWorker

__all__ = ["router"]

logger = logging.getLogger(__name__)

# Every handler below is a plain ``def`` on purpose.  Each one calls blocking
# code -- sync httpx to Paperless, sqlite through the job store, the worker --
# and FastAPI runs ``def`` handlers on its threadpool, so a slow Paperless call
# cannot stall ``/health`` or the status poll (ROBU-05, M-01).  The shared state
# they touch is locked: the JobStore's RLock, the worker's profile lock and the
# metadata cache's per-key locks.
router = APIRouter()

_TAGS_FORM_DEFAULT = Form(default=[])

# The only metadata resources the cache holds.  A runtime alias, not a
# TYPE_CHECKING import, because FastAPI reads it to validate the ``resource``
# query parameter: anything else is a 422 instead of reaching the cache (N-20).
MetadataResource = Literal["tags", "correspondents"]


def _get_cached_or_fetch(
    cache: MetadataCache,
    paperless: PaperlessClient,
    resource: MetadataResource,
) -> list[dict[str, object]]:
    """
    Retrieve metadata from cache or fetch from paperless-ngx.

    The fetch goes through the cache's single-flight ``get_or_fetch``, so
    concurrent requests for the same resource make one Paperless call
    (ROBU-05).  Falls back to an empty list if the paperless API is
    unreachable, ensuring the UI always loads even when paperless-ngx is down.

    Args:
        cache: Metadata cache instance.
        paperless: Paperless-ngx API client.
        resource: Resource name ('tags' or 'correspondents').

    Returns:
        List of metadata dicts, or empty list on error.

    """
    fetch = paperless.get_tags if resource == "tags" else paperless.get_correspondents
    try:
        data = cache.get_or_fetch(resource, fetch)
    except Exception:
        logger.warning(
            "Failed to fetch %s from paperless-ngx, using empty list",
            resource,
        )
        data = []
    return data


def _current_or_recent_job(worker: ScanWorker, job_store: JobStore) -> Job | None:
    """
    Find the job the status area should report: the current one, else the latest.

    The worker clears its current job id in ``_process_job``'s ``finally``, so a
    request landing as a job ends -- a flip Continue or Abort in particular --
    finds no current job.  Falling back to the most recent job makes that
    request report the job that just ended instead of the idle "Ready to scan."
    copy, which would claim nothing happened (M-02).  Every route that renders
    the status area uses this one lookup, so none of them can drift (D-17).

    The fallback skips rows rejected at submit (D-06).  A refused submit --
    queue full, worker down or degraded -- writes a REJECTED row that is newer
    than the job running at the time, yet it never ran, so it cannot be "the
    job that just ended".  ``JobStore.latest_run_job`` leaves those rows out;
    D-17's contract is otherwise unchanged, and history still lists them.

    Args:
        worker: The scan worker, for the id of the job in flight.
        job_store: The job store to read the job from.

    Returns:
        The current job, else the most recent job that ran, else None when no
        job has ever run.

    """
    job = None
    if worker.current_job_id:
        job = job_store.get_job(worker.current_job_id)
    if job is None:
        job = job_store.latest_run_job()
    return job


def _status_context(
    worker: ScanWorker,
    job_store: JobStore,
    claimed: tuple[str, FlipOutcome] | None = None,
) -> dict[str, object]:
    """
    Build the context ``partials/status.html`` renders from.

    The job comes from ``_current_or_recent_job`` (D-17), and the store's
    recorded state still selects the branch the partial renders, so D-16's
    objection to a route asserting state the store has not recorded does not
    apply.  What this adds is ``flip_answer``: for a job the store still reads
    as ``AWAITING_FLIP``, whether its flip wait has already been answered, and
    with what.  That is a fact the worker genuinely holds, and it lets the
    partial acknowledge the answer instead of re-rendering the Continue and
    Abort buttons as though the click did nothing (CR-01).

    ``claimed`` exists because the worker may already have cleared its flip
    coordinator by the time the route reads it: a route that just claimed an
    answer is authoritative for its own job.  It is used only when it names
    the job being rendered, so a posted foreign job id cannot acknowledge a
    job nobody answered (T-25-49).

    Args:
        worker: The scan worker, for the job in flight and its flip answer.
        job_store: The job store to read the job from.
        claimed: The job id and answer this request itself claimed, if any.

    Returns:
        ``{"job": ..., "flip_answer": ...}`` where ``flip_answer`` is None
        unless the rendered job is ``AWAITING_FLIP`` and has been answered.

    """
    job = _current_or_recent_job(worker, job_store)
    answer: FlipOutcome | None = None
    if job is not None and job.state is JobState.AWAITING_FLIP:
        if claimed is not None and claimed[0] == job.id:
            answer = claimed[1]
        else:
            answer = worker.flip_answer(job.id)
    return {"job": job, "flip_answer": answer}


@router.get("/")
def index(request: Request) -> Response:
    """
    Render the main page with scan form, status, and job history.

    Populates profile selector from the worker's profile set (read under its
    profile lock, D-19), fetches tags and correspondents from cache or
    paperless-ngx, and loads recent job history from the database.
    """
    state = request.app.state
    profiles = state.worker.profile_names()
    tags = _get_cached_or_fetch(state.cache, state.paperless, "tags")
    correspondents = _get_cached_or_fetch(
        state.cache, state.paperless, "correspondents"
    )

    status = _status_context(state.worker, state.job_store)

    jobs = state.job_store.list_recent(limit=50)

    return state.templates.TemplateResponse(
        request,
        "index.html",
        {
            "profiles": profiles,
            "tags": tags,
            "correspondents": correspondents,
            **status,
            "jobs": jobs,
            # The title input's maxlength; templates own no vocabulary (ROBU-08).
            "title_max_length": TITLE_MAX_LENGTH,
        },
    )


@router.get("/health", response_model=None)
def health(request: Request) -> dict[str, str] | JSONResponse:
    """
    Health check endpoint for container orchestration.

    Returns 200 with ``{"status": "ok"}`` when the worker is healthy.
    Otherwise 503, whose detail distinguishes a failing job store ("job store
    failing") from a dead worker thread ("worker thread is down") (D-10).
    Docker's HEALTHCHECK marks the container unhealthy on a 503 but does not
    restart it (documented in 26-14).
    """
    worker_health = request.app.state.worker.health
    if worker_health is WorkerHealth.HEALTHY:
        return {"status": "ok"}
    return JSONResponse(
        status_code=503,
        content={"status": "error", "detail": worker_health_detail(worker_health)},
    )


@router.get("/api/paperless/test", response_model=None)
def paperless_test(request: Request) -> dict[str, str] | JSONResponse:
    """
    Test paperless-ngx connection status.

    Returns JSON with status: connected, token_rejected, unreachable,
    or error with detail on unexpected failures.
    """
    try:
        status = request.app.state.paperless.test_connection()
        return {"status": status}
    except Exception as exc:
        logger.warning("Paperless connection test failed: %s", exc)
        return JSONResponse(
            status_code=502,
            content={"status": "error", "detail": type(exc).__name__},
        )


@dataclass(frozen=True, slots=True)
class _ScanForm:
    """The validated fields of one scan submission."""

    profile: str
    title: str
    tags: list[int]
    correspondent: int | None


def _unhealthy_rejection(
    worker_health: WorkerHealth,
) -> tuple[RequestRejection, str] | None:
    """
    Map the worker's health to the rejection a scan submit gets, if any.

    Args:
        worker_health: The worker's health at the time of the request.

    Returns:
        ``None`` when the worker is healthy, otherwise the rejection to raise
        and the job-row error text that records it (D-05, D-11).

    """
    match worker_health:
        case WorkerHealth.HEALTHY:
            outcome = None
        case WorkerHealth.DEGRADED:
            outcome = (RequestRejection.WORKER_DEGRADED, WORKER_DEGRADED_JOB_ERROR)
        case WorkerHealth.DOWN:
            outcome = (RequestRejection.WORKER_DOWN, WORKER_DOWN_JOB_ERROR)
        case _:
            assert_never(worker_health)
    return outcome


def _record_refused_submit(job_store: JobStore, form: _ScanForm, *, error: str) -> bool:
    """
    Record a submit refused before any job row existed, in one statement.

    D-05 departs from C-09's "create the row only after enqueue" because the
    user wants the refused attempt visible in history.  The row is written
    already ``ERROR`` with ``ErrorCategory.REJECTED``, the marker that keeps it
    out of the status area (D-06).

    ``create_rejected_job`` is a single ``INSERT``.  Create-then-finish would be
    two transactions, and a failure between them would leave a PENDING row with
    no marker that nothing ever reconciles, disabling the Scan button for good
    (WR-01).  With one statement a failure leaves no row at all.

    Args:
        job_store: The job store to write to.
        form: The submitted scan fields the row records.
        error: The job-row error text for the rejection.

    Returns:
        Whether the row was written.  ``False`` means the store refused the
        write, so the rendered error must not reload Job History.

    """
    try:
        job_store.create_rejected_job(
            profile=form.profile,
            title=form.title,
            error=error,
            tags=form.tags,
            correspondent=form.correspondent,
        )
    except Exception:
        logger.warning(
            "Could not record the rejected scan in job history", exc_info=True
        )
        return False
    return True


def _reject_created_job(
    worker: ScanWorker, job_store: JobStore, job_id: str, *, error: str
) -> bool:
    """
    Mark a job row the worker then refused as a REJECTED error (D-05, D-06).

    The row had to exist before ``put_nowait``, or the worker could dequeue an
    id with no row (D-05), so this refusal is recorded by finishing that row.
    The ``ErrorCategory.REJECTED`` marker is what keeps it out of the status
    area (D-06).

    The row already exists, so a failed write cannot simply be dropped: the
    row would stay PENDING with no marker, disabling the Scan button until a
    restart.  It is owed to the worker instead, which records it on its next
    idle tick (WR-01).

    Args:
        worker: The worker a failed write is owed to.
        job_store: The job store to write to.
        job_id: The row already created for this submit.
        error: The job-row error text for the rejection.

    Returns:
        Whether the row was written.  ``False`` means the store refused the
        write, so the rendered error must not reload Job History yet.

    """
    try:
        job_store.finish_job(
            job_id,
            JobState.ERROR,
            error=error,
            error_category=ErrorCategory.REJECTED,
        )
    except Exception:
        logger.warning(
            "Could not record the rejected scan in job history; "
            "the worker will record it",
            exc_info=True,
        )
        worker.owe_rejection(job_id, error)
        return False
    return True


@router.post("/api/scan")
def start_scan(
    request: Request,
    profile: Annotated[str, Form()],
    title: Annotated[str, Form(max_length=TITLE_MAX_LENGTH)] = "",
    tags: list[int] = _TAGS_FORM_DEFAULT,
    correspondent: Annotated[int | None, Form()] = None,
) -> Response:
    """
    Start a new scan job from form submission.

    Input is validated before any job row exists: a title over
    ``TITLE_MAX_LENGTH`` or a profile that is not configured (checked under
    the worker's profile lock) is a 422 and writes nothing (ROBU-08, D-19).

    A valid submit creates the job row and offers it to the worker, returning
    the status partial once the job is queued.  That response re-renders the
    Scan button out-of-band from server state (ROBU-04) and clears the
    ``#status-message`` slot out-of-band, so an error left there by an earlier
    rejected submit disappears (D-03).  A refused submit records a
    REJECTED error row and raises: 429 with ``Retry-After`` when the queue is
    full, 503 when the worker is down or degraded (ROBU-02, D-05, D-11).  The
    rendered error reloads Job History only when that row was written.

    Args:
        request: The incoming HTTP request.
        profile: Scan profile name.
        title: Document title (auto-generated if empty).
        tags: List of paperless-ngx tag IDs.
        correspondent: Optional paperless-ngx correspondent ID.

    Raises:
        RequestRejected: The profile is unknown, or the submit was refused.

    """
    state = request.app.state
    if not state.worker.has_profile(profile):
        raise RequestRejected(RequestRejection.UNKNOWN_PROFILE)
    if not title:
        title = f"Scan {datetime.now(tz=UTC).strftime('%Y-%m-%d %H:%M')}"
    form = _ScanForm(
        profile=profile, title=title, tags=tags, correspondent=correspondent
    )

    unhealthy = _unhealthy_rejection(state.worker.health)
    if unhealthy is not None:
        rejection, error = unhealthy
        written = _record_refused_submit(state.job_store, form, error=error)
        raise RequestRejected(rejection, refresh_history=written)

    job = state.job_store.create_job(
        profile=form.profile,
        title=form.title,
        tags=form.tags,
        correspondent=form.correspondent,
    )
    # Annotated because app.state is untyped; assert_never needs the real type.
    result: SubmitResult = state.worker.submit(job)
    match result:
        case SubmitResult.ACCEPTED:
            # A job created by this request cannot have a flip answer yet.
            # Only a successful scan clears the status-message slot (D-03).
            return state.templates.TemplateResponse(
                request,
                "partials/status_response.html",
                {"job": job, "flip_answer": None, "clear_message": True},
            )
        case SubmitResult.QUEUE_FULL:
            rejection, error = RequestRejection.QUEUE_FULL, QUEUE_FULL_JOB_ERROR
        case SubmitResult.DOWN:
            rejection, error = RequestRejection.WORKER_DOWN, WORKER_DOWN_JOB_ERROR
        case SubmitResult.DEGRADED:
            rejection, error = (
                RequestRejection.WORKER_DEGRADED,
                WORKER_DEGRADED_JOB_ERROR,
            )
        case _:
            assert_never(result)
    written = _reject_created_job(state.worker, state.job_store, job.id, error=error)
    raise RequestRejected(rejection, refresh_history=written)


@router.get("/api/jobs/current/status")
def current_job_status(request: Request) -> Response:
    """
    Poll the current or most recent job status.

    Returns the status partial template for HTMX polling swap.  While an
    answered job is still recorded ``AWAITING_FLIP``, the partial shows the
    acknowledgment rather than the flip buttons (CR-01).

    The response re-renders the Scan button out-of-band from server state
    (ROBU-04).  It never clears ``#status-message``: a poll carrying that clear
    would erase a rejection shown mid-scan within a second (D-03).
    """
    state = request.app.state
    return state.templates.TemplateResponse(
        request,
        "partials/status_response.html",
        _status_context(state.worker, state.job_store),
    )


@router.get("/api/tags")
def get_tags(request: Request) -> Response:
    """
    Fetch tag options for the dropdown selector.

    Uses cached data when available, falling back to a fresh fetch
    from paperless-ngx. Returns empty options on API errors.
    """
    state = request.app.state
    tags = _get_cached_or_fetch(state.cache, state.paperless, "tags")
    return state.templates.TemplateResponse(
        request,
        "partials/tags.html",
        {"tags": tags},
    )


@router.get("/api/correspondents")
def get_correspondents(request: Request) -> Response:
    """
    Fetch correspondent options for the dropdown selector.

    Uses cached data when available, falling back to a fresh fetch
    from paperless-ngx. Returns empty options on API errors.
    """
    state = request.app.state
    correspondents = _get_cached_or_fetch(
        state.cache, state.paperless, "correspondents"
    )
    return state.templates.TemplateResponse(
        request,
        "partials/correspondents.html",
        {"correspondents": correspondents},
    )


@router.post("/api/cache/invalidate")
def invalidate_cache(request: Request, resource: MetadataResource) -> Response:
    """
    Invalidate a specific cache entry and return fresh data.

    After clearing the cached entry, fetches and returns the updated
    partial for the specified resource.  Any other resource name is a 422
    before the cache is touched (N-20).

    Args:
        request: The incoming HTTP request.
        resource: Resource name to invalidate ('tags' or 'correspondents').

    """
    state = request.app.state
    state.cache.invalidate(resource)

    if resource == "tags":
        tags = _get_cached_or_fetch(state.cache, state.paperless, "tags")
        return state.templates.TemplateResponse(
            request,
            "partials/tags.html",
            {"tags": tags},
        )

    correspondents = _get_cached_or_fetch(
        state.cache, state.paperless, "correspondents"
    )
    return state.templates.TemplateResponse(
        request,
        "partials/correspondents.html",
        {"correspondents": correspondents},
    )


@router.get("/api/jobs/history")
def job_history(request: Request) -> Response:
    """
    Fetch the job history table body.

    Returns the history partial with the most recent jobs for
    HTMX swap into the history table.
    """
    state = request.app.state
    jobs = state.job_store.list_recent(limit=50)
    return state.templates.TemplateResponse(
        request,
        "partials/history.html",
        {"jobs": jobs},
    )


@router.post("/api/flip/continue")
def continue_flip(request: Request, job_id: str = Form(...)) -> Response:
    """
    Answer the named job's flip prompt with Continue, starting its pass B.

    The posted ``job_id`` scopes the answer (CR-01).  A Continue for any other
    job, one sent before that job reached the flip prompt, or one arriving
    after the prompt was already answered is dropped -- and the route still
    returns the current status rather than an error (D-16).  Nothing is
    waited for: the route renders whatever the store has recorded and the
    one-second poll picks up pass B from there.

    While the store still reads ``AWAITING_FLIP`` for an answered job, the
    partial acknowledges the answer in place of the Continue and Abort
    buttons, so a claimed or repeated click never re-renders a prompt that
    looks unanswered (CR-01).

    The response re-renders the Scan button out-of-band from server state
    (ROBU-04) and leaves ``#status-message`` alone (D-03).
    """
    state = request.app.state
    claimed = state.worker.continue_flip(job_id)
    return state.templates.TemplateResponse(
        request,
        "partials/status_response.html",
        _status_context(
            state.worker,
            state.job_store,
            claimed=(job_id, FlipOutcome.CONTINUED) if claimed else None,
        ),
    )


@router.post("/api/flip/abort")
def abort_flip(request: Request, job_id: str = Form(...)) -> Response:
    """
    Answer the named job's flip prompt with Abort, failing it before pass B.

    The posted ``job_id`` scopes the answer (CR-01): a double-clicked Abort
    cannot land on the next queued job.  An Abort for any other job, one sent
    before that job reached the flip prompt, or one arriving after Continue
    already answered is dropped rather than reported as an error (D-16): the
    route returns the current status either way.

    While the store still reads ``AWAITING_FLIP`` for an answered job, the
    partial shows "Aborting scan..." (or the answer that won) in place of the
    buttons, so the response never invites a second click (CR-01).

    The response re-renders the Scan button out-of-band from server state
    (ROBU-04) and leaves ``#status-message`` alone (D-03).
    """
    state = request.app.state
    claimed = state.worker.abort_flip(job_id)
    return state.templates.TemplateResponse(
        request,
        "partials/status_response.html",
        _status_context(
            state.worker,
            state.job_store,
            claimed=(job_id, FlipOutcome.ABORTED) if claimed else None,
        ),
    )
