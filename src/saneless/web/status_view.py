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

    The worker clears its current job id in ``_process_job``'s ``finally``, so a
    request landing as a job ends -- a flip Continue or Abort in particular --
    finds no current job.  Falling back to the most recent job makes that
    request report the job that just ended instead of the idle "Ready to scan."
    copy, which would claim nothing happened.  Every route that renders the
    status area uses this one lookup, so none of them can drift.

    The fallback skips rows rejected at submit.  A refused submit --
    queue full, worker down or degraded -- writes a REJECTED row that is newer
    than the job running at the time, yet it never ran, so it cannot be "the
    job that just ended".  ``JobStore.latest_run_job`` leaves those rows out;
    the lookup's contract is otherwise unchanged, and history still lists them.

    The fallback also skips a refused submit whose REJECTED write the request
    could not make and owed to the worker.  Until the worker writes it,
    that row is PENDING with no marker and would render as "Starting scan..."
    with the Scan button disabled, for a scan that never ran.  The
    accepted residual window is a poll landing during the request's own write
    attempt, between ``create_job`` and that write or the ``owe_rejection``
    call after it failed.

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
        job = job_store.latest_run_job(exclude_ids=worker.owed_rejection_ids())
    return job


def owned_active_job_id(
    worker: ScanWorker, job_store: JobStore, presented: str | None
) -> str | None:
    """
    Find the newest active job the presenting browser owns, for the page to follow.

    A full page load has no poll URL to carry a followed id, so without this
    a reload while someone else's scan runs would report that scan to a
    person whose own job is still queued behind it.  The candidates are the
    worker's current job, when it is still active, and every queued job;
    the newest ``created_at`` among the ones this browser owns wins.

    Ownership is ``owns_detail``'s rule, not the flip prompt's: a job that
    recorded no owner is nobody's, so it is never followed, and a browser
    presenting no token owns nothing.  A refused submit whose REJECTED write
    is still owed to the worker is PENDING with the submitter's token, yet it
    will never run, so it is left out as ``_current_or_recent_job`` leaves it
    out.

    The token is compared here, in Python, and never put into a SQL
    ``WHERE``: ``owns_detail`` compares in constant time, which an index
    lookup does not, and the set being filtered is bounded by the queue cap,
    so reading it whole costs nothing.  ``JobStore.latest_run_job`` filters
    its excluded ids in Python for the same kind of reason.

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

    A PENDING job the worker is not running is queued only while the worker
    runs another: with nothing running it is about to start.  The busy line
    and the tab title both read this, from the one call ``status_context``
    makes, so a tab titled "Queued" never sits over a "Starting scan..." line.

    Args:
        worker: The scan worker, for the id of the job in flight.
        job: The job being rendered, or None when nothing has ever run.

    Returns:
        True for a PENDING job while the worker runs a different one.

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

    Built here rather than composed in the template, because templates own no
    vocabulary: a page that assembled its own sentence would be a second place
    for the copy to drift from what ``vocabulary.busy_line`` says.

    Four situations, in the precedence ``busy_line`` itself documents.  A
    queued job -- PENDING while the worker runs a *different* one -- is told
    what it is waiting for and how many jobs are ahead.  Whether it is queued
    is decided once, by ``_is_queued`` in ``status_context``, and passed in,
    so the busy line and the tab title read one answer.
    The job the worker is actually running, on its second
    manual-duplex pass, leads with the pages counted on the first; on a later
    pass of a multi-page document, it leads with the pages already kept.
    Everything else is the plain progress prose, unchanged.

    Both counts are the worker's, and so only ever passed for the job the
    worker is running.  ``busy_line`` reads each only in the state it belongs
    to, so passing both here cannot put a count on the wrong line.

    The zero case reads as being next in line; a count of none ahead is never
    spelled out as a number, because it is technically true and reads like a
    bug.  That rule lives in ``vocabulary.busy_line``, so this
    function passes the count through and does not restate it.

    The queued line names a job other than the one being rendered, so it is
    gated on its own: the running job's title reaches only the browser that
    started the running job, by the same rule ``build_job_view`` applies, and
    everyone else waits for the generic title.

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

    Bundled rather than passed one by one because passing them separately
    would take ``status_context`` past ``PLR0913``'s five-parameter ceiling.
    A suppression is forbidden in this project, and ``create_job`` already
    had to take the same route, so the frozen dataclass is the established
    answer here.

    Every field is read off the request in ``status_facts`` and nowhere else,
    which is what stops a route added later from acquiring or losing a fact by
    forgetting about it -- the same discipline ``refresh_checks`` and
    ``is_owner`` already follow.

    ``settings`` is the running configuration, carried so ``status_context``
    can build the job's view: which host paths the owner's text names by
    setting is read from it.

    ``claimed`` and ``claimed_pass`` are the flip answer and the multi-page
    answer this request itself claimed, each with the job it names.  They are
    two fields rather than one because the two waits have two answer types,
    and a flip answer must never be read as a multi-page one.
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

    ``owner_token`` and ``scan_blocked`` are both derived here rather than at
    each call site, so a new status-rendering route cannot silently lose the
    owner's flip prompt or hand back an enabled Scan button on a blocked
    appliance.  Only the two facts a route genuinely knows
    about itself -- the answer it just claimed, and the job the browser is
    following -- are passed in.

    ``followed_job_id`` has no default, so every caller has to decide which
    job its status area follows, and the type checkers refuse a call that did
    not.  A default of None once let the flip and multi-page answers silently
    hand the area to the current-job URL, so the next poll reported whatever
    the worker ran instead of the job the operator had just answered.  A route
    that genuinely follows nothing in particular passes None and says so.

    ``scan_blocked`` is derived from ``Settings``, which is loaded once at
    process start, so it cannot change while the process runs: there is no live
    flip to orchestrate and ``POST /api/checks/refresh`` deliberately carries
    neither the button nor the reason line, because re-running the checks
    cannot change a verdict that was never read from them.

    This unwraps the configured token, and the value goes to the predicate and
    nowhere else: it is never logged, rendered or echoed, and the flag that
    reaches the template is a bool (ASVS 4.0.3 V7.1.1).

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

    The job comes from ``_current_or_recent_job``, and the store's recorded
    state still selects the branch the partial renders, so no route asserts a
    state the store has not recorded.  It reaches the partial as a
    ``JobView``, never the stored row: whether this browser sees the title,
    the preview and the error and warning text, and in what form, is decided
    in ``build_job_view``, once, from the token the request presents.  Every
    status response -- the page, both polls, both flip answers and the scan
    submit -- goes through here, so none can render more than the view holds.

    What this adds is ``flip_answer``: for a
    job the store still reads as ``AWAITING_FLIP``, whether its flip wait has
    already been answered, and with what.  That is a fact the worker genuinely
    holds, and it lets the partial acknowledge the answer instead of
    re-rendering the Continue and Abort buttons as though the click did
    nothing.

    ``claimed`` exists because the worker may already have cleared its flip
    coordinator by the time the route reads it: a route that just claimed an
    answer is authoritative for its own job.  It is used only when it names
    the job being rendered, so a posted foreign job id cannot acknowledge a
    job nobody answered.

    ``followed_job_id`` is the second such extra fact: the job this browser
    submitted, baked into its poll URL by ``start_scan``.  It is used only to
    select which job is rendered, and when it names no existing row the
    function falls back to ``_current_or_recent_job``, so a browser whose job
    has been pruned degrades to today's behaviour instead of meeting a 404.
    The context key echoes back only an id that was actually found,
    which is what keeps that fallback rendering byte-identical to the one
    ``GET /api/jobs/current/status`` produces.

    Args:
        worker: The scan worker, for the job in flight and its flip answer.
        job_store: The job store to read the job from.
        facts: The per-request bundle ``status_facts`` built.

    ``refresh_checks`` is False here for every caller, and that is the whole of
    the flag's policy: ``start_scan`` sets it True on its own, so a status poll
    or a flip answer cannot carry an out-of-band strip.  Defaulting it in this
    one builder rather than at each call site is what stops a route added later
    from acquiring the behaviour by forgetting to say no.

    ``is_owner`` is decided here, once, rather than at each call site, for the
    same reason ``refresh_checks`` is: a route added later must not be able to
    acquire or lose the gate by forgetting about it.  The partial reads the
    flag and never the token, so the value itself has no path into the
    markup.  It is computed from the stored row with ``owner.is_owner``, not from
    the view's rule: a job that recorded no owner may be answered by anyone,
    while its title and preview are nobody's.

    ``scan_blocked`` rides along for the same reason again, and it is why every
    out-of-band ``#scan-btn`` -- the scan submit's own response, both status
    polls and both flip answers -- carries the blocked state: they all render
    ``partials/scan_button.html`` from this one context, so no status response
    can hand back an enabled button on an appliance that cannot upload.  On a
    blocked appliance ``POST /api/scan`` is refused before it reaches its
    success branch, so that one is unreachable today; it is included anyway,
    because the property being defended is that the flag lives in one partial
    fed from one builder, not that each caller remembered.

    A job waiting on a multi-page question gets the same treatment through
    ``_pass_wait_context``: its claimed answer, and, for a viewer who may
    answer, the open question and its wording.  ``pass_heading``, the
    prompt's first line naming the document, is built beside it for an owner
    from the view's title, so the owner gate decides that title here too.

    A job waiting for a person -- the flip, or a multi-page question -- also
    gets its deadline, read by ``_wait_deadline`` for every viewer, not only
    the owner: a deadline is a time and carries no job detail, which is what
    lets ``non_owner_line`` tell anyone else when the wait gives up.  The
    title still reaches the prompt only through the view, so ``flip_heading``
    names the owner's real title, and a job that recorded no owner -- which
    anyone may answer -- by the generic title.  ``flip_note`` says when the
    flip wait ends and what then happens to the pages, or how long it lasts
    until the worker has recorded when it began.

    ``idle_line`` is what the area says with no job to report: the blocked
    reason on an appliance that cannot upload, so the area never invites a
    scan the button under it refuses, and ``IDLE_LINE`` otherwise.
    ``page_title`` is the tab's title for this rendering, from the job's state
    and never its title, which the tab list and the browser history would
    show to anyone at the tablet.  Every status response carries it, so the
    tab follows each change the area shows and keeps its title across a 204.

    Returns:
        This viewer's view of the job, its flip answer, the flip prompt's job
        line and deadline note, the line a viewer who cannot answer a waiting
        job sees, its multi-page answer, prompt, wording and job line, the
        followed job's id, the one busy line, whether
        the job is queued, the idle line, the tab title, whether this viewer
        may answer the job's prompt, whether the Scan button is blocked and a
        false strip-refresh flag.  ``flip_answer`` is None unless the rendered
        job is ``AWAITING_FLIP`` and has been answered.

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

    A page loaded after a job ended would otherwise render that job's outcome
    exactly as a live poll does -- red, in an alert, with the history reload --
    and present an old result as news to whoever opens the page next, however
    long ago it was.  So the full page, and only the full page, reports a job
    that is not active as the past: the idle line, then one muted line naming
    the outcome, the title and when it started, and the detail line a warning
    or a failure carries.  Every status response keeps the live rendering, so
    a poll that watches a job end still shows the outcome, alert included.

    The lines are built from the job's view, so the owner gate still decides
    the title and the warning and error text.  The time is ``created_at``,
    worded "started", because a job records no finish time.

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

A token is 24 hex characters.  A longer value cannot match, so it is ignored
and the poll is answered in full -- never refused with a 422.  A refused poll
would reach the page's error handling, and a poll's failure must never land in
``#status-message``, where it would read as the operator's own mistake.
"""

POLL_INTERVAL_SECONDS: Final = 1
"""How often an active status area polls, in seconds."""

_STATUS_TOKEN_BYTES: Final = 12
"""The token's digest size: 24 hex characters in the poll URL."""

FOCUS_SCAN: Final = "scan"
"""
The value of a status poll's ``focus`` parameter that asks for the Scan button.

A claimed Abort sends focus to Scan, but the button its own response renders
is still disabled while the scan winds down, and a disabled button cannot take
focus.  So the response bakes this into its poll URL, and the first rendering
whose Scan button is enabled carries ``autofocus`` on it.

The parameter is compared against this constant and never echoed: any other
value is ignored, and a poll URL only ever carries this constant, so nothing a
client sends reaches the markup.
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

    Templates compose no poll URL of their own: the page, both polls, the three
    answer routes, the scan submit and the template-free fallback all render
    the one this function returns, so none of them can drift on the path or
    the query.

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

    The token is a hash of the rendered bytes rather than of a hand-picked set
    of facts, because what the viewer sees changes in ways such a set misses:
    a preview stored mid-scan while the busy line stays the same, the owner
    gate, the question number a multi-page prompt carries, the queue
    position.  The rendering covers every one of them, and any later change to
    the templates, by construction.

    It hashes the canonical *poll* rendering, never the calling route's own:
    the empty ``seen``, the one-second interval, no focus attribute, no
    message clear, no strip refresh, and the terminal reload every poll
    carries.  A scan submit or an answer carries extras a poll never does, so
    a token computed from its own rendering would never match the first poll
    after it, and every action would be followed by one needless swap -- one
    that replaces the focused button and speaks the area again.

    The one focus fact the canonical rendering keeps is ``focus_scan``: unlike
    the status area's action-only ``autofocus``, it is poll-visible, because
    it rides in the poll URL and is what a poll carrying it renders.

    The hash is keyed with the process's ``status_token_key``.  The token
    travels in a URL, and so into access logs; keyed, it is unlinkable to the
    content and cannot be computed by anyone else.

    Args:
        request: The incoming request, for the app's templates and key.
        context: The status context ``status_context`` built for this viewer.

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

    This is the one definition of the token that both the real status
    rendering and the lost-contact fallback carry, so the two cannot drift on
    the key or the digest size.

    Args:
        request: The incoming request, for the app's ``status_token_key``.
        text: The rendering to hash.

    Returns:
        The token, as hex.

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

    Every status rendering goes through here, so every one of them hands the
    browser a poll URL whose ``seen`` names what it is showing.  htmx captures
    an element's URL when it processes the element and never re-processes an
    element a 204 left in place, so the URL a rendering bakes is the one every
    poll from it presents until something changes.

    ``focus_scan`` is the Scan button's pending request for focus, set by a
    claimed Abort and then by each poll that carries it on.  It rides in the
    poll URL and in the token, and the rendering puts ``autofocus`` on the
    Scan button once that button is enabled.  The status area polls only while
    its job is active, and the button is disabled for exactly that long, so the
    first rendering that can honour the request is also the last one that
    polls: focus is moved once.

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

    A value longer than any token is ignored rather than compared, and the
    comparison is constant-time, so neither its length nor its content can be
    probed.  An empty ``seen`` never matches.

    Args:
        seen: The token the poll presented.
        token: The token of what the poll would render now.

    Returns:
        True when the poll can be answered with no content.

    """
    if not seen or len(seen) > _SEEN_MAX_LENGTH:
        return False
    return secrets.compare_digest(seen.encode(), token.encode())


STATUS_BACKOFF_SECONDS: Final = (2, 5, 15)
"""
The intervals a status poll that cannot read its job steps through, in seconds.

A failing poll's first retry comes after 2 s, its second after 5 s, and every
later one after 15 s.  The first step is short because the common failure is a
moment's lock contention on the job store, and the cap is long because a store
that stays broken gains nothing from a browser asking every second.

Polling never stops: the page has no script of its own to restart it, so a
poll that gave up would leave the area frozen on the fallback line after the
store healed.  At the cap the fallback is the same every time, so its poll is
answered 204 and the area is not re-swapped or re-announced.

Before the cap, each step's fallback carries a different poll URL, so it is
swapped in, and a screen reader may read the same line again: at most once
per step, three times in all.  That bounded repeat is accepted.  Skipping it
would mean keeping the step out of the swapped markup, which only starting at
the cap does, and that would turn a moment's lock contention into a 15 s wait.

Read at call time rather than bound into a default, so a test can shorten it.
"""


def _confirmed_job_id(job_id: str | None) -> str | None:
    """
    Return a path job id in canonical form if it is a UUID, else None.

    The lost-contact fallback cannot ask the store whether the id names a job,
    so it echoes back only something that is well-formed: job ids are
    ``str(uuid.uuid4())``, and anything else falls back to the current route.

    Args:
        job_id: The id from the poll's path, or None for the current route.

    Returns:
        The parsed id as its canonical string, or None.

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

    Built in Python without a template, so a template fault cannot reach the
    one path that has to survive it.  The body is the status area with a
    fixed line and no exception text; the text goes to the server log only.
    It carries no out-of-band Scan button, no ``#status-message`` clear and no
    ``<title>``: nothing the poll could not read is asserted, and the alert
    slot is left to the operator's own failed actions.

    The next poll URL carries the next backoff step as ``attempt`` and the
    fallback's own token as ``seen``.  The token hashes the markup built with
    an empty ``seen``, the same shape ``_status_token`` hashes, so at the cap,
    where the markup no longer changes, a poll presenting it gets a 204.  A
    pending request for focus on the Scan button is carried on as well, so an
    Abort whose wind-down the store could not report still focuses Scan once
    the store heals.

    Args:
        request: The incoming request, for the token key.
        job_id: The id from the poll's path, or None for the current route.
        attempt: The failing poll's clamped attempt count.
        seen: The token the poll presented.
        focus_scan: Whether the poll still asks for focus on the Scan button.

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

    The flip wait's deadline comes from ``ScanWorker.flip_deadline`` and a
    multi-page question's from ``ScanWorker.pass_deadline``; the job's stored
    state says which wait it is in.  It is read once per rendering, for every
    viewer and outside the owner gate: a deadline is a time and nothing else,
    with no title or other job detail, so telling a viewer who cannot answer
    when the wait ends discloses nothing the owner gate protects.

    Args:
        worker: The scan worker, which holds when each wait began.
        job: The job being rendered, or None.

    Returns:
        The aware deadline, or None when the job is not waiting for a person
        or the worker has not yet recorded when its wait began.

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

    Three keys, all None unless the rendered job's row is in one of the
    multi-page waiting states.

    ``pass_answer`` is the answer already claimed for the job's latest
    question, so the partial acknowledges it rather than re-rendering buttons
    that look as though the click did nothing.  As with the flip answer, a
    route that has just claimed one is authoritative for the job it named,
    because the worker may move on before the route reads it back; the claim
    counts only when it names the job being rendered.

    ``pass_prompt`` and ``pass_copy`` are the open question and every sentence
    it shows.  They are built only for a viewer who may answer, by the same
    rule as the flip prompt: the owner, or anyone when the job recorded no
    owner.  Nobody else gets a prompt number, a button or the scanner's error
    text, because none of it is put in their context at all.  The error text
    is a job detail rather than part of the question, so it follows the rule
    the rest of the job view uses (``owns_detail``) rather than the answering
    rule: a job that recorded no owner may be answered by anyone, but its
    error text is nobody's, just as ``build_job_view`` hides that job's stored
    error.  It is scrubbed of host paths and addresses before it goes into the
    copy, so even the owner never sees one.

    The copy's timeout note names ``deadline`` when there is one.  The
    deadline is read by the caller for every viewer, outside the owner gate,
    because it is a time and carries no job detail; the title that heads the
    prompt stays gated through the view, in the caller.

    Args:
        worker: The scan worker, for the open question and its answer.
        job: The job being rendered, or None.
        facts: The per-request bundle, for the claimed answer and the settings.
        is_owner: Whether this viewer may answer the job's questions.
        deadline: When the open question gives up, from ``_wait_deadline``.

    Returns:
        ``pass_answer``, ``pass_prompt`` and ``pass_copy``.

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
