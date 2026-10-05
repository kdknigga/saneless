"""
The status area one browser sees, and how a status poll is answered.

The page, both status polls, the three answer routes and the scan submit all
render ``partials/status_response.html``, and every one of them must show the
same job, the same prompt and the same Scan button for the same stored state.
The context they render from is built here, from the worker and the job
store, and reaches the template as a ``JobView`` so the owner gate decides,
once, what this viewer may see.  The poll URL each rendering hands the browser
is built here too, carrying a keyed token of what that rendering shows, so a
poll that would render the same thing is answered 204.

A poll that cannot read or render its job is answered here as well, with a
fixed line that backs off and never stops polling.
"""

from __future__ import annotations

import hashlib
import html
import logging
import secrets
import uuid
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final
from urllib.parse import urlencode

from starlette.responses import HTMLResponse, Response

from saneless.vocabulary import (
    HIDDEN_JOB_TITLE,
    IDLE_LINE,
    LOST_CONTACT_LINE,
    PASS_WAIT_STATES,
    JobState,
    busy_line,
    flip_deadline_note,
    flip_heading,
    last_scan_detail,
    last_scan_line,
    non_owner_wait_line,
    page_title,
    pass_heading,
    pass_prompt_copy,
)
from saneless.web import owner, scan_block
from saneless.web.job_view import build_job_view, owns_detail, scrub_for_owner
from saneless.web.services import services

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from starlette.requests import Request

    from saneless.config import Settings
    from saneless.job import Job, JobStore
    from saneless.vocabulary import FlipOutcome, PassAnswer, PassPrompt, PassPromptCopy
    from saneless.web.job_view import JobView
    from saneless.worker import ScanWorker

__all__ = [
    "FOCUS_SCAN",
    "POLL_INTERVAL_SECONDS",
    "STATUS_BACKOFF_SECONDS",
    "StatusFacts",
    "answer_status_poll",
    "last_scan_context",
    "owned_active_job_id",
    "poll_url",
    "status_context",
    "status_facts",
    "with_poll",
]

logger = logging.getLogger(__name__)


def _current_or_recent_job(worker: ScanWorker, job_store: JobStore) -> Job | None:
    """
    Find the job the status area should report: the current one, else the latest.

    The worker clears its current job id as a job ends, so the fallback makes a
    request landing then report that job rather than the idle line.  It skips
    refused submits, including one whose REJECTED write is still owed to the
    worker and so still reads PENDING, because neither ever ran.

    Returns:
        The current job, else the most recent job that ran, else None.

    """
    job = None
    if worker.current_job_id:
        job = job_store.get_job(worker.current_job_id)
    if job is None:
        job = job_store.latest_run_job(exclude_ids=worker.owed_rejection_ids())
    return job


def owned_active_job_id(
    worker: ScanWorker, job_store: JobStore, presented: str | None
) -> str | None:
    """
    Find the newest active job the presenting browser owns, for the page to follow.

    A full page load has no poll URL to carry a followed id, so without this a
    reload would report someone else's running scan to a person whose own job
    is still queued.  Ownership is ``owns_detail``'s rule: a job that recorded
    no owner is never followed, and a refused submit whose REJECTED write is
    still owed is left out because it will never run.

    The token is compared in Python by ``owns_detail``, in constant time, and
    never put into a SQL ``WHERE``; the candidates are bounded by the queue cap.

    Args:
        worker: The scan worker, for the job in flight and the owed refusals.
        job_store: The job store to read the candidates from.
        presented: The owner token this request carries, or None.

    Returns:
        The id of the newest active job this browser owns, or None.

    """
    if presented is None:
        return None
    candidates = list(job_store.list_pending())
    if worker.current_job_id:
        current = job_store.get_job(worker.current_job_id)
        if current is not None and current.is_active:
            candidates.append(current)
    owed = worker.owed_rejection_ids()
    owned = [
        job
        for job in candidates
        if job.id not in owed and owns_detail(presented, job.owner_token)
    ]
    if not owned:
        return None
    return max(owned, key=lambda job: job.created_at).id


def _is_queued(worker: ScanWorker, job: Job | None) -> bool:
    """
    Report whether a job waits in the queue behind the one the worker runs.

    A PENDING job is queued only while the worker runs another; with nothing
    running it is about to start.  The busy line and the tab title both read the
    one answer ``status_context`` computes, so they cannot disagree.

    """
    return (
        job is not None
        and job.state is JobState.PENDING
        and worker.current_job_id not in (None, job.id)
    )


def _busy_line(
    worker: ScanWorker,
    job_store: JobStore,
    job: Job | None,
    *,
    presented: str | None,
    queued: bool,
) -> str | None:
    """
    Compose the one line the status area shows while the rendered job works.

    Built here because templates own no vocabulary.  A queued job is told what
    it waits for and how many jobs are ahead; the running job gets the worker's
    pass counts, which ``busy_line`` reads only in the state each belongs to.
    The queued line names another job, so that job's title reaches only its
    owner, by ``build_job_view``'s rule.

    Args:
        worker: The scan worker, for the job in flight and its front count.
        job_store: The job store, for the running job's title and the position.
        job: The job being rendered, or None when nothing has ever run.
        presented: The owner token this request carries, or None.
        queued: Whether the job waits behind the one the worker is running.

    Returns:
        One line of plain text, or None when there is no job to describe.

    """
    if job is None:
        return None
    running_id = worker.current_job_id
    if queued:
        running = job_store.get_job(running_id) if running_id else None
        ahead = job_store.queue_position(job.id, running=running_id)
        if running is not None and ahead is not None:
            title = (
                running.title
                if owns_detail(presented, running.owner_token)
                else HIDDEN_JOB_TITLE
            )
            return busy_line(job.state, queue_title=title, queue_ahead=ahead)
    if job.id == running_id:
        return busy_line(
            job.state,
            front_pages=worker.front_pages,
            pages_kept=worker.pages_kept,
        )
    return busy_line(job.state)


@dataclass(frozen=True, slots=True)
class StatusFacts:
    """
    The per-request facts a status render needs beyond the worker and store.

    Every field is read off the request in ``status_facts`` and nowhere else, so
    a route added later cannot acquire or lose a fact by forgetting it.
    ``claimed`` and ``claimed_pass`` are the flip and multi-page answers this
    request claimed, each with its job id; they are separate fields so a flip
    answer is never read as a multi-page one.
    """

    settings: Settings
    claimed: tuple[str, FlipOutcome] | None = None
    claimed_pass: tuple[str, PassAnswer] | None = None
    followed_job_id: str | None = None
    owner_token: str | None = None
    scan_blocked: bool = False


def status_facts(
    request: Request,
    *,
    followed_job_id: str | None,
    claimed: tuple[str, FlipOutcome] | None = None,
    claimed_pass: tuple[str, PassAnswer] | None = None,
) -> StatusFacts:
    """
    Read every per-request status fact off the request, in one place.

    Deriving ``owner_token`` and ``scan_blocked`` here means a new status route
    cannot lose the owner's prompt or hand back an enabled Scan button on a
    blocked appliance.  ``followed_job_id`` has no default, so every caller must
    decide which job its area follows; a route that follows nothing passes None.

    ``scan_blocked`` comes from settings loaded once at start, so it cannot
    change while the process runs, and ``POST /api/checks/refresh`` carries
    neither the button nor its reason.  The configured token it unwraps goes to
    the predicate only and is never logged, rendered or echoed; the template
    sees a bool (ASVS 4.0.3 V7.1.1).

    Args:
        request: The incoming request, for its cookies and the app's settings.
        claimed: The job id and flip answer this request itself claimed, if any.
        claimed_pass: The job id and multi-page answer this request itself
            claimed, if any.
        followed_job_id: The job this browser follows -- the one it
            submitted, answered or owns -- or None to report the current job.

    Returns:
        The bundle ``status_context`` reads.

    """
    settings = services(request).settings
    return StatusFacts(
        settings=settings,
        claimed=claimed,
        claimed_pass=claimed_pass,
        followed_job_id=followed_job_id,
        owner_token=owner.presented_owner(request),
        scan_blocked=scan_block.block_for(settings) is not None,
    )


def status_context(
    worker: ScanWorker,
    job_store: JobStore,
    facts: StatusFacts,
) -> dict[str, object]:
    """
    Build the context ``partials/status.html`` renders from.

    Every status response goes through here.  The job reaches the partial as a
    ``JobView`` from ``build_job_view``, never the stored row, so the owner gate
    decides once what this viewer sees, while the stored state selects the
    branch.  ``is_owner``, ``scan_blocked`` and the false ``refresh_checks`` are
    decided here too, so a route added later cannot acquire or lose them by
    forgetting.  ``is_owner`` applies ``owner.is_owner`` to the stored row: a job
    that recorded no owner may be answered by anyone, while its title is
    nobody's.

    A claimed answer in ``facts`` is authoritative for the job it names, because
    the worker may already have cleared its coordinator; it counts only when it
    names the rendered job.  A ``followed_job_id`` that names no row falls back
    to ``_current_or_recent_job``, and only a found id is echoed, so that
    fallback renders exactly as ``GET /api/jobs/current/status`` does.

    A waiting job's deadline is read for every viewer, outside the owner gate,
    because it is a time and carries no job detail.  ``idle_line`` is the
    blocked reason on a blocked appliance, so the area never invites a scan the
    button refuses.  ``page_title`` comes from the job's state, never its title,
    which the tab list and history would show to anyone at the tablet.

    Args:
        worker: The scan worker, for the job in flight and its flip answer.
        job_store: The job store to read the job from.
        facts: The per-request bundle ``status_facts`` built.

    Returns:
        The template context.  ``flip_answer`` is None unless the rendered job
        is ``AWAITING_FLIP`` and has been answered.

    """
    followed = (
        job_store.get_job(facts.followed_job_id) if facts.followed_job_id else None
    )
    job = (
        followed if followed is not None else _current_or_recent_job(worker, job_store)
    )
    answer: FlipOutcome | None = None
    if job is not None and job.state is JobState.AWAITING_FLIP:
        if facts.claimed is not None and facts.claimed[0] == job.id:
            answer = facts.claimed[1]
        else:
            answer = worker.flip_answer(job.id)
    view = (
        build_job_view(job, presented=facts.owner_token, settings=facts.settings)
        if job is not None
        else None
    )
    is_owner = job is not None and owner.is_owner(facts.owner_token, job.owner_token)
    queued = _is_queued(worker, job)
    block = scan_block.block_for(facts.settings)
    deadline = _wait_deadline(worker, job)
    flipping = job is not None and job.state is JobState.AWAITING_FLIP
    return {
        "job": view,
        "flip_answer": answer,
        "flip_heading": (
            flip_heading(view.title)
            if flipping and is_owner and view is not None
            else None
        ),
        "flip_note": (
            flip_deadline_note(
                deadline=deadline,
                timeout_seconds=facts.settings.output.operator_wait_timeout_seconds,
            )
            if flipping
            else None
        ),
        "non_owner_line": (
            non_owner_wait_line(job.state, deadline=deadline)
            if job is not None and (flipping or job.state in PASS_WAIT_STATES)
            else None
        ),
        "pass_heading": (
            pass_heading(view.title)
            if is_owner and view is not None and view.state in PASS_WAIT_STATES
            else None
        ),
        **_pass_wait_context(worker, job, facts, is_owner=is_owner, deadline=deadline),
        "refresh_checks": False,
        "followed_job_id": followed.id if followed is not None else None,
        "busy_line": _busy_line(
            worker, job_store, job, presented=facts.owner_token, queued=queued
        ),
        "queued": queued,
        "idle_line": block.reason if block is not None else IDLE_LINE,
        "page_title": (
            page_title(
                view.state,
                warning=view.warning,
                category=view.error_category,
                queued=queued,
            )
            if view is not None
            else page_title(None)
        ),
        "is_owner": is_owner,
        "scan_blocked": facts.scan_blocked,
    }


def last_scan_context(view: JobView | None) -> dict[str, object]:
    """
    Turn a finished job on a fresh page into the "Last scan" lines.

    Only the full page reports a finished job as the past, so an old result is
    not presented as news to whoever opens the page next; every status response
    keeps the live rendering, so a poll that watches a job end still shows the
    outcome.  The lines come from the view, so the owner gate still decides the
    title and the detail.  The time is ``created_at``, worded "started", because
    a job records no finish time.

    Args:
        view: The job the status context chose, as this viewer may see it, or
            None when no job has ever run.

    Returns:
        The keys that replace the live rendering, or an empty mapping when
        there is no job or it is still active.  ``job`` becomes None, which
        is what selects the idle branch and leaves the Scan button enabled.

    """
    if view is None or view.is_active:
        return {}
    return {
        "job": None,
        "last_job": view,
        "last_scan_line": last_scan_line(
            view.state,
            warning=view.warning,
            category=view.error_category,
            title=view.title,
            created_at=view.created_at,
        ),
        "last_scan_detail": last_scan_detail(
            view.state,
            warning=view.warning,
            category=view.error_category,
            error=view.error,
        ),
        "page_title": page_title(None),
    }


_SEEN_MAX_LENGTH: Final = 64
"""
The longest ``seen`` value a status poll compares against its token.

A longer value cannot match, so it is ignored and the poll answered in full,
never refused with a 422: a refused poll would land in ``#status-message``,
where it would read as the operator's own mistake.
"""

POLL_INTERVAL_SECONDS: Final = 1
"""How often an active status area polls, in seconds."""

_STATUS_TOKEN_BYTES: Final = 12
"""The token's digest size in bytes; the poll URL carries it as hex."""

FOCUS_SCAN: Final = "scan"
"""
The value of a status poll's ``focus`` parameter that asks for the Scan button.

A claimed Abort's own Scan button is still disabled while the scan winds down,
and a disabled button cannot take focus, so the request rides in the poll URL
until a rendering's button is enabled.  The parameter is compared against this
constant and never echoed, so nothing a client sends reaches the markup.
"""


def poll_url(
    followed_job_id: str | None,
    *,
    seen: str,
    attempt: int = 0,
    focus_scan: bool = False,
) -> str:
    """
    Build the URL a status area polls, in the one place any URL for it is built.

    Templates compose no poll URL of their own, so no rendering, the
    template-free fallback included, can drift on the path or the query.

    Args:
        followed_job_id: The job the area follows, or None to follow whatever
            the current job is.
        seen: The token of the rendering the poll's element shows; empty for
            a rendering that has not been hashed, which no poll matches.
        attempt: The poll's retry count, carried only when non-zero.
        focus_scan: Whether the poll still asks for focus on the Scan button,
            carried as ``FOCUS_SCAN`` only when true.

    Returns:
        The path and its query string, not yet HTML-escaped.

    """
    path = (
        f"/api/jobs/{followed_job_id}/status"
        if followed_job_id
        else "/api/jobs/current/status"
    )
    params = {"seen": seen}
    if attempt:
        params["attempt"] = str(attempt)
    if focus_scan:
        params["focus"] = FOCUS_SCAN
    return f"{path}?{urlencode(params)}"


def _followed_in(context: Mapping[str, object]) -> str | None:
    """
    Return the followed job id a status context carries, if any.

    Args:
        context: A context built by ``status_context``.

    Returns:
        The id ``status_context`` found and echoed, or None.

    """
    followed = context.get("followed_job_id")
    return followed if isinstance(followed, str) else None


def _status_token(request: Request, context: Mapping[str, object]) -> str:
    """
    Hash what a status poll would show this viewer, as the poll's ``seen`` token.

    The rendered bytes are hashed, not a chosen set of facts, so every change
    the viewer can see moves the token.  It hashes the canonical *poll*
    rendering, never the calling route's own: an action's extras would make its
    token miss the first poll after it and force one needless swap.
    ``focus_scan`` stays in, because it rides in the poll URL.  The hash is
    keyed with the per-process ``status_token_key``, so a token in an access
    log is unlinkable to the content and nobody else can compute it.

    Returns:
        The token, as hex.

    """
    svc = services(request)
    canonical = svc.templates.get_template("partials/status_response.html").render(
        {
            **context,
            "request": request,
            "poll_url": poll_url(
                _followed_in(context),
                seen="",
                focus_scan=context.get("focus_scan") is True,
            ),
            "poll_interval": POLL_INTERVAL_SECONDS,
            "focus_status_area": False,
            "clear_message": False,
            "refresh_checks": False,
            "terminal_reload": True,
        }
    )
    return _keyed_digest(request, canonical)


def _keyed_digest(request: Request, text: str) -> str:
    """
    Hash a rendering with the process key, as a status poll token.

    The real rendering and the lost-contact fallback both use this one
    definition, so they cannot drift on the key or the digest size.

    """
    return hashlib.blake2b(
        text.encode(),
        key=services(request).status_token_key,
        digest_size=_STATUS_TOKEN_BYTES,
    ).hexdigest()


def with_poll(
    request: Request, context: Mapping[str, object], *, focus_scan: bool = False
) -> tuple[str, dict[str, object]]:
    """
    Add the poll URL and interval to a status context, with its token baked in.

    htmx captures an element's URL once and never re-processes an element a 204
    left in place, so the URL a rendering bakes is the one every poll from it
    presents until something changes.  ``focus_scan``, set by a claimed Abort,
    rides in the poll URL and the token; the first rendering whose Scan button
    is enabled is also the last that polls, so focus moves once.

    Args:
        request: The incoming request.
        context: The status context ``status_context`` built for this viewer.
        focus_scan: Whether this rendering asks for focus on the Scan button.

    Returns:
        The token, and the context extended with ``focus_scan``, ``poll_url``
        and ``poll_interval``.

    """
    focused = {**context, "focus_scan": focus_scan}
    token = _status_token(request, focused)
    return token, {
        **focused,
        "poll_url": poll_url(_followed_in(context), seen=token, focus_scan=focus_scan),
        "poll_interval": POLL_INTERVAL_SECONDS,
    }


def _unchanged(seen: str, token: str) -> bool:
    """
    Report whether a poll's ``seen`` names the rendering it would be sent.

    An empty or over-long value never matches, and the comparison is
    constant-time, so neither the length nor the content can be probed.

    """
    if not seen or len(seen) > _SEEN_MAX_LENGTH:
        return False
    return secrets.compare_digest(seen.encode(), token.encode())


STATUS_BACKOFF_SECONDS: Final = (2, 5, 15)
"""
The intervals a status poll that cannot read its job steps through, in seconds.

The first step is short because the common failure is a moment's lock
contention on the job store; the cap is long because a store that stays broken
gains nothing from faster polls.  Polling never stops, because the page has no
script to restart it; at the cap the fallback is identical, so its poll is
answered 204 and not re-announced.  Before the cap a screen reader may hear the
line once per step, which is accepted.

Read at call time rather than bound into a default, so a test can shorten it.
"""


def _confirmed_job_id(job_id: str | None) -> str | None:
    """
    Return a path job id in canonical form if it is a UUID, else None.

    The lost-contact fallback cannot ask the store whether the id names a job,
    so it echoes back only a well-formed id; anything else follows the current
    route.

    """
    if job_id is None:
        return None
    try:
        return str(uuid.UUID(job_id))
    except ValueError:
        return None


def _lost_contact_fallback(
    request: Request,
    job_id: str | None,
    *,
    attempt: int,
    seen: str,
    focus_scan: bool,
) -> Response:
    """
    Answer a status poll that could not read or render its job.

    Built without a template, so a template fault cannot reach the one path
    that must survive it.  The body is a fixed line with no exception text, no
    out-of-band Scan button, no ``#status-message`` clear and no ``<title>``, so
    nothing the poll could not read is asserted.  Its token hashes the markup
    with an empty ``seen``, as ``_status_token`` does, so at the backoff cap a
    poll presenting it gets a 204; a pending Scan focus is carried on.

    Returns:
        The fallback at 200, or an empty 204 when the poll already shows it.

    """
    step = min(attempt + 1, len(STATUS_BACKOFF_SECONDS))
    interval = STATUS_BACKOFF_SECONDS[step - 1]
    followed = _confirmed_job_id(job_id)
    line = html.escape(LOST_CONTACT_LINE)

    def body(token: str) -> str:
        url = html.escape(
            poll_url(followed, seen=token, attempt=step, focus_scan=focus_scan)
        )
        return (
            f'<div id="status-area" tabindex="-1" hx-get="{url}" '
            f'hx-trigger="every {interval}s" hx-swap="outerHTML">\n'
            f'  <p class="status-fallback">&#9888; {line}</p>\n'
            "</div>\n"
        )

    token = _keyed_digest(request, body(""))
    if _unchanged(seen, token):
        return Response(status_code=204)
    return HTMLResponse(status_code=200, content=body(token))


def answer_status_poll(
    request: Request,
    job_id: str | None,
    *,
    seen: str,
    attempt: int,
    focus_scan: bool,
) -> Response:
    """
    Render a status poll for the followed job, falling back when that fails.

    Both poll routes answer through here, so they cannot differ in how they
    fail.  The render happens inside the guard as well as the context build,
    because ``TemplateResponse`` renders in its constructor.

    Args:
        request: The incoming request.
        job_id: The followed job's id, or None for the current route.
        seen: The token the poll presented.
        attempt: The poll's attempt count, straight from the query string.
        focus_scan: Whether the poll asks for focus on the Scan button.

    Returns:
        The status partial, the lost-contact fallback, or an empty 204.

    """
    counted = min(max(attempt, 0), len(STATUS_BACKOFF_SECONDS))
    svc = services(request)
    try:
        token, context = with_poll(
            request,
            status_context(
                svc.worker,
                svc.job_store,
                status_facts(request, followed_job_id=job_id),
            ),
            focus_scan=focus_scan,
        )
        if _unchanged(seen, token):
            return Response(status_code=204)
        return svc.templates.TemplateResponse(
            request,
            "partials/status_response.html",
            {**context, "terminal_reload": True},
        )
    except Exception:
        logger.exception("Failed to render the job status")
        return _lost_contact_fallback(
            request, job_id, attempt=counted, seen=seen, focus_scan=focus_scan
        )


def _wait_deadline(worker: ScanWorker, job: Job | None) -> datetime | None:
    """
    Read when the rendered job's wait for a person gives up, if it is known.

    Read for every viewer, outside the owner gate: a deadline is a time and
    carries no job detail.  None when the job is not waiting or the worker has
    not yet recorded when the wait began.

    """
    if job is None:
        return None
    if job.state is JobState.AWAITING_FLIP:
        return worker.flip_deadline(job.id)
    if job.state in PASS_WAIT_STATES:
        return worker.pass_deadline(job.id)
    return None


def _pass_wait_context(
    worker: ScanWorker,
    job: Job | None,
    facts: StatusFacts,
    *,
    is_owner: bool,
    deadline: datetime | None,
) -> dict[str, object]:
    """
    Build the status context a job waiting on a multi-page question adds.

    ``pass_answer`` is the answer already claimed for the latest question; a
    claim in ``facts`` is authoritative for the job it names, as with the flip.
    ``pass_prompt`` and ``pass_copy`` are built only for a viewer who may answer,
    so nobody else's context holds a prompt number, a button or the scanner's
    error text.  That error text is a job detail, so it follows ``owns_detail``
    rather than the answering rule, and is scrubbed of host paths and addresses
    even for the owner.

    Returns:
        ``pass_answer``, ``pass_prompt`` and ``pass_copy``, all None unless the
        job is in a multi-page waiting state.

    """
    answer: PassAnswer | None = None
    prompt: PassPrompt | None = None
    copy: PassPromptCopy | None = None
    if job is not None and job.state in PASS_WAIT_STATES:
        if facts.claimed_pass is not None and facts.claimed_pass[0] == job.id:
            answer = facts.claimed_pass[1]
        else:
            answer = worker.pass_answer(job.id)
        if answer is None and is_owner:
            prompt = worker.pass_prompt(job.id)
    if prompt is not None and job is not None:
        error = (
            scrub_for_owner(prompt.error, facts.settings)
            if prompt.error and owns_detail(facts.owner_token, job.owner_token)
            else None
        )
        # Replaced on the prompt itself rather than passed beside it: given no
        # error, the copy falls back to the prompt's own, unscrubbed text.
        copy = pass_prompt_copy(replace(prompt, error=error), deadline=deadline)
    return {"pass_answer": answer, "pass_prompt": prompt, "pass_copy": copy}
