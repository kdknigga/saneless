"""HTTP route handlers for the saneless web UI."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from saneless.vocabulary import (
    FlipOutcome,
    JobState,
    WorkerHealth,
    worker_health_detail,
)

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


def _get_cached_or_fetch(
    cache: MetadataCache,
    paperless: PaperlessClient,
    resource: str,
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

    Args:
        worker: The scan worker, for the id of the job in flight.
        job_store: The job store to read the job from.

    Returns:
        The current job, else the most recent job, else None when there has
        never been one.

    """
    job = None
    if worker.current_job_id:
        job = job_store.get_job(worker.current_job_id)
    if job is None:
        recent = job_store.list_recent(limit=1)
        job = recent[0] if recent else None
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

    Populates profile selector from settings, fetches tags and
    correspondents from cache or paperless-ngx, and loads recent
    job history from the database.
    """
    state = request.app.state
    profiles = list(state.settings.profiles.keys())
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


@router.post("/api/scan")
def start_scan(
    request: Request,
    profile: str = Form(...),
    title: str = Form(default=""),
    tags: list[int] = _TAGS_FORM_DEFAULT,
    correspondent: int | None = Form(default=None),
) -> Response:
    """
    Start a new scan job from form submission.

    Creates a job in the store and submits it to the worker queue.
    Returns the status partial for HTMX swap.

    Args:
        request: The incoming HTTP request.
        profile: Scan profile name.
        title: Document title (auto-generated if empty).
        tags: List of paperless-ngx tag IDs.
        correspondent: Optional paperless-ngx correspondent ID.

    """
    state = request.app.state
    if not title:
        title = f"Scan {datetime.now(tz=UTC).strftime('%Y-%m-%d %H:%M')}"

    job = state.job_store.create_job(
        profile=profile,
        title=title,
        tags=tags,
        correspondent=correspondent,
    )
    state.worker.submit(job)

    # A job created by this request cannot have a flip answer yet.
    return state.templates.TemplateResponse(
        request,
        "partials/status.html",
        {"job": job, "flip_answer": None},
    )


@router.get("/api/jobs/current/status")
def current_job_status(request: Request) -> Response:
    """
    Poll the current or most recent job status.

    Returns the status partial template for HTMX polling swap.  While an
    answered job is still recorded ``AWAITING_FLIP``, the partial shows the
    acknowledgment rather than the flip buttons (CR-01).
    """
    state = request.app.state
    return state.templates.TemplateResponse(
        request,
        "partials/status.html",
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
def invalidate_cache(request: Request, resource: str) -> Response:
    """
    Invalidate a specific cache entry and return fresh data.

    After clearing the cached entry, fetches and returns the updated
    partial for the specified resource.

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
    """
    state = request.app.state
    claimed = state.worker.continue_flip(job_id)
    return state.templates.TemplateResponse(
        request,
        "partials/status.html",
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
    """
    state = request.app.state
    claimed = state.worker.abort_flip(job_id)
    return state.templates.TemplateResponse(
        request,
        "partials/status.html",
        _status_context(
            state.worker,
            state.job_store,
            claimed=(job_id, FlipOutcome.ABORTED) if claimed else None,
        ),
    )
