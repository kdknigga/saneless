"""HTTP route handlers for the saneless web UI."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from starlette.responses import Response

    from saneless.paperless import PaperlessClient
    from saneless.web.cache import MetadataCache

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter()

_TAGS_FORM_DEFAULT = Form(default=[])


def _get_cached_or_fetch(
    cache: MetadataCache,
    paperless: PaperlessClient,
    resource: str,
) -> list[dict[str, object]]:
    """
    Retrieve metadata from cache or fetch from paperless-ngx.

    Falls back to an empty list if the paperless API is unreachable,
    ensuring the UI always loads even when paperless-ngx is down.

    Args:
        cache: Metadata cache instance.
        paperless: Paperless-ngx API client.
        resource: Resource name ('tags' or 'correspondents').

    Returns:
        List of metadata dicts, or empty list on error.

    """
    data = cache.get(resource)
    if data is not None:
        return data
    try:
        if resource == "tags":
            data = paperless.get_tags()
        else:
            data = paperless.get_correspondents()
        cache.set(resource, data)
    except Exception:
        logger.warning(
            "Failed to fetch %s from paperless-ngx, using empty list",
            resource,
        )
        data = []
    return data


@router.get("/")
async def index(request: Request) -> Response:
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

    current_job = None
    if state.worker.current_job_id:
        current_job = state.job_store.get_job(state.worker.current_job_id)
    if current_job is None:
        recent = state.job_store.list_recent(limit=1)
        current_job = recent[0] if recent else None

    jobs = state.job_store.list_recent(limit=50)

    return state.templates.TemplateResponse(
        request,
        "index.html",
        {
            "profiles": profiles,
            "tags": tags,
            "correspondents": correspondents,
            "job": current_job,
            "jobs": jobs,
        },
    )


@router.get("/health", response_model=None)
async def health(request: Request) -> dict[str, str] | JSONResponse:
    """
    Health check endpoint for container orchestration.

    Returns 200 with ``{"status": "ok"}`` when the worker thread is
    alive, or 503 with error detail when the worker is down.
    """
    if request.app.state.worker.is_alive:
        return {"status": "ok"}
    return JSONResponse(
        status_code=503,
        content={"status": "error", "detail": "worker thread is down"},
    )


@router.get("/api/paperless/test", response_model=None)
async def paperless_test(request: Request) -> dict[str, str] | JSONResponse:
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
async def start_scan(
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

    return state.templates.TemplateResponse(
        request,
        "partials/status.html",
        {"job": job},
    )


@router.get("/api/jobs/current/status")
async def current_job_status(request: Request) -> Response:
    """
    Poll the current or most recent job status.

    Returns the status partial template for HTMX polling swap.
    """
    state = request.app.state
    job = None
    if state.worker.current_job_id:
        job = state.job_store.get_job(state.worker.current_job_id)
    if job is None:
        recent = state.job_store.list_recent(limit=1)
        job = recent[0] if recent else None

    return state.templates.TemplateResponse(
        request,
        "partials/status.html",
        {"job": job},
    )


@router.get("/api/tags")
async def get_tags(request: Request) -> Response:
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
async def get_correspondents(request: Request) -> Response:
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
async def invalidate_cache(request: Request, resource: str) -> Response:
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
async def job_history(request: Request) -> Response:
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
async def continue_flip(request: Request) -> Response:
    """
    Signal the worker to continue with pass B of manual duplex.

    Returns the updated status partial.
    """
    state = request.app.state
    state.worker.continue_flip()
    state.worker.wait_transition(timeout=2.0)
    job = None
    if state.worker.current_job_id:
        job = state.job_store.get_job(state.worker.current_job_id)
    return state.templates.TemplateResponse(
        request,
        "partials/status.html",
        {"job": job},
    )


@router.post("/api/flip/abort")
async def abort_flip(request: Request) -> Response:
    """
    Signal the worker to abort the current manual duplex scan.

    Returns the updated status partial.
    """
    state = request.app.state
    state.worker.abort_flip()
    job = None
    if state.worker.current_job_id:
        job = state.job_store.get_job(state.worker.current_job_id)
    return state.templates.TemplateResponse(
        request,
        "partials/status.html",
        {"job": job},
    )
