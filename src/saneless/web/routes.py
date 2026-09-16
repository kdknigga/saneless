"""HTTP route handlers for the saneless web UI."""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Final, Literal, assert_never

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from saneless.checks import (
    CHECKING_GLYPH,
    CHECKING_MESSAGE,
    CHECKING_STATE_CLASS,
    CHECKING_STATE_LABEL,
    CheckKey,
    run_checks,
)
from saneless.config import is_placeholder_token, resolve_job_title
from saneless.scanner.base import SourceKind, classify_source
from saneless.vocabulary import (
    QUEUE_FULL_JOB_ERROR,
    SCAN_BLOCKED_REASON,
    TITLE_MAX_LENGTH,
    TOKEN_UNSET_JOB_ERROR,
    WORKER_DEGRADED_JOB_ERROR,
    WORKER_DOWN_JOB_ERROR,
    ErrorCategory,
    FlipOutcome,
    JobState,
    RequestRejection,
    SubmitResult,
    WorkerHealth,
    busy_line,
    local_time,
    worker_health_detail,
)
from saneless.web.errors import RequestRejected

if TYPE_CHECKING:
    from starlette.datastructures import State
    from starlette.responses import Response

    from saneless.job import Job, JobStore
    from saneless.paperless import PaperlessClient
    from saneless.web.cache import MetadataCache
    from saneless.web.checks_cache import CachedChecks
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

# The cookie naming the browser that started a scan (D-23).  Every attribute it
# is set with is deliberate: ``HttpOnly`` so no script can read it -- there is
# no script file in this application at all; ``SameSite=Lax`` so the browser
# withholds it on any cross-site POST; no lifetime attribute, so it is a
# session cookie that dies when the browser closes; and deliberately no
# ``Secure``, because the appliance is served over plain HTTP on a LAN and that
# flag would silently stop the cookie being sent rather than harden it.
#
# REQUIREMENTS' position, recorded here so a later reader does not mistake this
# for something it is not: the token is a footgun guard for the flip prompt,
# not an authentication mechanism.  ``CrossOriginGuard`` allows a POST that
# carries neither ``Sec-Fetch-Site`` nor ``Origin``, so a scripted client that
# sends a guessed cookie of its own can answer a flip.  That is accepted on a
# trusted LAN, not overlooked.  What the token stops is the household member
# standing at the same appliance pressing Continue on a stack they did not
# load, which is the failure this phase exists to close.
OWNER_COOKIE: Final = "saneless_owner"


def _presented_owner(request: Request) -> str | None:
    """
    Return the owner token this request carries, or None when it carries none.

    A blank or whitespace-only value counts as none.  A browser holding one has
    no usable identity, and treating it as a token would make every such
    browser the same owner as every other.

    Args:
        request: The incoming request.

    Returns:
        The token, or None.

    """
    presented = request.cookies.get(OWNER_COOKIE, "").strip()
    return presented or None


def _is_owner(presented: str | None, recorded: str | None) -> bool:
    """
    Report whether a presented token speaks for the job that recorded one.

    A NULL recorded token means the job is unowned and everyone may answer it
    (UI-SPEC S5).  Every row written before this phase has one, including a
    manual-duplex job that was in flight across an upgrade, and a strict rule
    would leave such a job un-continuable until the Phase 25 flip timeout fired
    it away.  Nothing can create a NULL-token job after this phase, so the
    exception has a closed lifetime.

    The comparison goes through ``secrets.compare_digest`` so no timing
    difference can be read off it (T-30-57).  Both sides are encoded first:
    the presented value arrives as text out of a header and ``compare_digest``
    refuses a non-ASCII ``str``, while it compares bytes of any two lengths
    safely.

    Args:
        presented: The token this request carries, or None.
        recorded: The token stored on the job row, or None when unowned.

    Returns:
        Whether this request may answer for the job.

    """
    if recorded is None:
        return True
    if presented is None:
        return False
    return secrets.compare_digest(presented.encode(), recorded.encode())


def _owner_answers(presented: str | None, job: Job | None) -> bool:
    """
    Report whether this request may answer the named job's flip prompt (D-24).

    An unknown job id answers False: there is nothing to own, and the worker
    would have dropped the answer anyway.  The outcome is logged as a match or
    a mismatch and never as a value -- the token is not allowed into a log
    line any more than into the markup (T-30-59).

    Args:
        presented: The token this request carries, or None.
        job: The job the answer names, or None when no such row exists.

    Returns:
        Whether the answer should be passed to the worker.

    """
    if job is None:
        return False
    matched = _is_owner(presented, job.owner_token)
    logger.debug(
        "Flip answer for job %s: owner %s",
        job.id,
        "matched" if matched else "did not match",
    )
    return matched


# The freshness line's four UI-SPEC variants, composed here rather than in the
# template: the strip's templates own no vocabulary, and a page that assembled
# its own prose would be a second place for the copy to drift from D-08's
# specimen.  The dash is U+2014 with spaces on both sides, as that specimen
# writes it.
_PAUSED_PREFIX: Final = "Paused during scan — "
_COLD_PAUSED_LINE: Final = f"{_PAUSED_PREFIX}not checked yet."


@dataclass(frozen=True, slots=True)
class _CheckingRow:
    """
    One cold-start placeholder row, before any probe has happened (D-06).

    It is not a :class:`~saneless.checks.CheckResult` because "we have not
    looked yet" is not one of the three ``CheckState`` members, and inventing a
    fourth would owe an exit-code rule to ``saneless doctor`` for a state the
    CLI cannot ever be in -- it probes synchronously and always has an answer.
    So the row carries the class, glyph and screen-reader word directly, and
    every one of them is a constant imported from ``saneless.checks``: the
    template still authors none of them.

    Only ``key`` varies, so the five rows are built once at import.

    Attributes:
        key: Which check this row is standing in for.
        state_class: The muted colour class the glyph is drawn in.
        glyph: The neutral cold-start marker.
        state_label: The word a screen reader hears in the glyph's place.
        message: The cold-start copy.

    """

    key: CheckKey
    state_class: str = CHECKING_STATE_CLASS
    glyph: str = CHECKING_GLYPH
    state_label: str = CHECKING_STATE_LABEL
    message: str = CHECKING_MESSAGE


# One placeholder per CheckKey, in member order, so a cold strip still renders
# five *named* rows rather than an empty list that reads as "nothing to report".
_CHECKING_ROWS: Final = tuple(_CheckingRow(key=key) for key in CheckKey)


def _freshness_line(cached: CachedChecks, *, scan_active: bool) -> str:
    """
    Compose the one sentence under the rows, for the four situations.

    The four variants are UI-SPEC S1's, verbatim.  Two axes produce them:
    whether any results exist yet, and whether a scan is holding the scanner.
    The paused wording is D-08's whole point -- a strip that silently showed a
    half-hour-old Scanner row during a scan would be lying by omission, and one
    that blanked would throw away the four rows that are still true.

    The timestamp goes through the shared ``local_time`` filter, which is the
    same object ``saneless doctor``'s table uses, so the two surfaces cannot
    disagree about the zone or the format (APPL-12, D-35).

    Args:
        cached: The cache snapshot this render is showing.
        scan_active: Whether the worker currently has a job in flight.

    Returns:
        The exact sentence for this situation.

    """
    if cached.results is None or cached.checked_at is None:
        return _COLD_PAUSED_LINE if scan_active else CHECKING_MESSAGE
    stamp = local_time(cached.checked_at)
    if scan_active:
        return f"{_PAUSED_PREFIX}last checked {stamp}."
    return f"Last checked {stamp}."


def _checks_context(state: State) -> dict[str, object]:
    """
    Build the context ``partials/checks.html`` renders from, without probing.

    D-04: this reads the cache and never calls ``run_checks``.  Probing inside
    a request handler is what an unplugged scanner host would make hang -- a
    sane-net connect that Linux retries six times costs roughly two minutes
    inside a blocking C call, and a page that waited for it would be a page
    that never arrives.  The background refresher is what fills the cache; this
    only reads what is already there.

    ``checks`` is ``None`` on a cold cache, and that is the single fact the
    template branches on: no results means the poll trigger is emitted and the
    placeholder rows are drawn, results means neither.  Nothing else in the
    strip is conditional.

    Args:
        state: The application state holding the cache and the worker.

    Returns:
        ``checks``, ``checking_rows``, ``freshness_line`` and ``scan_active``.

    """
    cached: CachedChecks = state.checks.current()
    # The worker's own record of a job in flight, not the scanner gate: reading
    # the gate would mean acquiring it, and a render is not allowed to contend
    # for the lock a live scan holds.
    scan_active = state.worker.current_job_id is not None
    return {
        "checks": cached.results,
        "checking_rows": _CHECKING_ROWS,
        "freshness_line": _freshness_line(cached, scan_active=scan_active),
        "scan_active": scan_active,
    }


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

    The fallback also skips a refused submit whose REJECTED write the request
    could not make and owed to the worker (WR-01).  Until the worker writes it,
    that row is PENDING with no marker and would render as "Starting scan..."
    with the Scan button disabled, for a scan that never ran (IN-08).  The
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


def _busy_line(worker: ScanWorker, job_store: JobStore, job: Job | None) -> str | None:
    """
    Compose the one line the status area shows while the rendered job works.

    Built here rather than composed in the template, because templates own no
    vocabulary: a page that assembled its own sentence would be a second place
    for the copy to drift from what ``vocabulary.busy_line`` says.

    Three situations, in the precedence ``busy_line`` itself documents.  A
    PENDING job while the worker runs a *different* one is waiting in the
    queue, and is told what it is waiting for and how many jobs are ahead
    (APPL-08).  The job the worker is actually running, on its second
    manual-duplex pass, leads with the pages counted on the first.  Everything
    else is the plain progress prose, unchanged.

    The zero case reads as being next in line; a count of none ahead is never
    spelled out as a number, because it is technically true and reads like a
    bug (UI-SPEC S5).  That rule lives in ``vocabulary.busy_line``, so this
    function passes the count through and does not restate it.

    Args:
        worker: The scan worker, for the job in flight and its front count.
        job_store: The job store, for the running job's title and the position.
        job: The job being rendered, or None when nothing has ever run.

    Returns:
        One line of plain text, or None when there is no job to describe.

    """
    if job is None:
        return None
    running_id = worker.current_job_id
    if job.state is JobState.PENDING and running_id not in (None, job.id):
        running = job_store.get_job(running_id) if running_id else None
        ahead = job_store.queue_position(job.id)
        if running is not None and ahead is not None:
            return busy_line(job.state, queue_title=running.title, queue_ahead=ahead)
    if job.id == running_id and job.state is JobState.SCANNING_REVERSE:
        return busy_line(job.state, front_pages=worker.front_pages)
    return busy_line(job.state)


@dataclass(frozen=True, slots=True)
class _StatusFacts:
    """
    The per-request facts a status render needs beyond the worker and store.

    Bundled rather than passed one by one because ``_status_context`` had
    reached ``PLR0913``'s five-parameter ceiling and this plan adds a sixth
    fact.  A suppression is forbidden (CLAUDE.md), and ``create_job`` already
    had to take the same route, so the frozen dataclass is the established
    answer here.

    Every field is read off the request in ``_status_facts`` and nowhere else,
    which is what stops a route added later from acquiring or losing a fact by
    forgetting about it -- the same discipline ``refresh_checks`` and
    ``is_owner`` already follow.
    """

    claimed: tuple[str, FlipOutcome] | None = None
    followed_job_id: str | None = None
    owner_token: str | None = None
    scan_blocked: bool = False


def _status_facts(
    request: Request,
    *,
    claimed: tuple[str, FlipOutcome] | None = None,
    followed_job_id: str | None = None,
) -> _StatusFacts:
    """
    Read every per-request status fact off the request, in one place.

    ``owner_token`` and ``scan_blocked`` are both derived here rather than at
    each call site, so a new status-rendering route cannot silently lose the
    owner's flip prompt (D-24) or hand back an enabled Scan button on a
    blocked appliance (D-15).  Only the two facts a route genuinely knows
    about itself -- the answer it just claimed, and the job the browser is
    following -- are passed in.

    ``scan_blocked`` is derived from ``Settings``, which is loaded once at
    process start, so it cannot change while the process runs: there is no live
    flip to orchestrate and ``POST /api/checks/refresh`` deliberately carries
    neither the button nor the reason line, because re-running the checks
    cannot change a verdict that was never read from them (UI-SPEC S8).

    This unwraps the configured token, and the value goes to the predicate and
    nowhere else: it is never logged, rendered or echoed, and the flag that
    reaches the template is a bool (ASVS V7, CFG-05).

    Args:
        request: The incoming request, for its cookies and the app's settings.
        claimed: The job id and answer this request itself claimed, if any.
        followed_job_id: The job this browser submitted, if it submitted one.

    Returns:
        The bundle ``_status_context`` reads.

    """
    settings = request.app.state.settings
    return _StatusFacts(
        claimed=claimed,
        followed_job_id=followed_job_id,
        owner_token=_presented_owner(request),
        scan_blocked=is_placeholder_token(settings.paperless.token.get_secret_value()),
    )


def _status_context(
    worker: ScanWorker,
    job_store: JobStore,
    facts: _StatusFacts,
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

    ``followed_job_id`` is the second such extra fact: the job this browser
    submitted, baked into its poll URL by ``start_scan``.  It is used only to
    select which job is rendered, and when it names no existing row the
    function falls back to ``_current_or_recent_job``, so a browser whose job
    has been pruned degrades to today's behaviour instead of meeting a 404
    (D-25).  The context key echoes back only an id that was actually found,
    which is what keeps that fallback rendering byte-identical to the one
    ``GET /api/jobs/current/status`` produces.

    Args:
        worker: The scan worker, for the job in flight and its flip answer.
        job_store: The job store to read the job from.
        facts: The per-request bundle ``_status_facts`` built.

    ``refresh_checks`` is False here for every caller, and that is the whole of
    the flag's policy: ``start_scan`` sets it True on its own, so a status poll
    or a flip answer cannot carry an out-of-band strip.  Defaulting it in this
    one builder rather than at each call site is what stops a route added later
    from acquiring the behaviour by forgetting to say no.

    ``is_owner`` is decided here, once, rather than at each call site, for the
    same reason ``refresh_checks`` is: a route added later must not be able to
    acquire or lose the gate by forgetting about it.  The partial reads the
    flag and never the token, so the value itself has no path into the markup
    (T-30-59).

    ``scan_blocked`` rides along for the same reason again, and it is why every
    out-of-band ``#scan-btn`` -- the scan submit's own response, both status
    polls and both flip answers -- carries the blocked state: they all render
    ``partials/scan_button.html`` from this one context, so no status response
    can hand back an enabled button on an appliance that cannot upload
    (UI-SPEC S8, C-10).  On a blocked appliance ``POST /api/scan`` is refused
    before it reaches its success branch, so that one is unreachable today; it
    is included anyway, because the property being defended is that the flag
    lives in one partial fed from one builder, not that each caller remembered.

    Returns:
        The job, its flip answer, the followed job's id, the one busy line,
        whether this viewer owns the job, whether the Scan button is blocked
        and a false strip-refresh flag.  ``flip_answer`` is None unless the
        rendered job is ``AWAITING_FLIP`` and has been answered.

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
    return {
        "job": job,
        "flip_answer": answer,
        "refresh_checks": False,
        "followed_job_id": followed.id if followed is not None else None,
        "busy_line": _busy_line(worker, job_store, job),
        "is_owner": job is not None and _is_owner(facts.owner_token, job.owner_token),
        "scan_blocked": facts.scan_blocked,
    }


@dataclass(frozen=True, slots=True)
class _ProfileOption:
    """
    One entry of the Profile select: what it submits and what it reads as.

    Attributes:
        name: The profile name, which is the ``value`` the option submits.
            It is the wire contract ``POST /api/scan`` already takes, so only
            the text a household member reads is new here.
        label: The human name shown in the dropdown.
        description: The sentence shown beneath the select for this profile.

    """

    name: str
    label: str
    description: str


def _profile_options(worker: ScanWorker) -> tuple[_ProfileOption, ...]:
    """
    Build the ordered option list the Profile select renders (APPL-05, D-21).

    D-21 asks for feeder profiles first on a sheet-fed device, and this is
    where ``has_flatbed`` is read: sheet-fed means the device reports no
    flatbed source, and at render time the server's evidence for that is the
    generated profile set, which mirrors the device's sources.  So the answer
    is derived from the profiles already in hand -- no new device probe, no new
    config key, which is exactly what the decision asks for.

    The classification comes from ``classify_source`` and from nowhere else:
    its docstring states it is the only source-classification rule in the
    codebase and forbids re-deriving the answer from the string.  That matters
    most for the commonest real feeder name of all, whose first four letters
    are the whole of the automatic rule -- an exact match is what keeps it out
    of the single-page group, and this function inherits that answer rather
    than asking again.

    Args:
        worker: The worker whose profile set is being rendered.

    Returns:
        One option per configured profile, feeder-first when the device is
        sheet-fed and in configuration order otherwise.

    """
    entries: list[tuple[_ProfileOption, SourceKind]] = []
    for name in worker.profile_names():
        profile = worker.get_profile(name)
        if profile is None:
            # Listing and looking up are two locked calls, so a profile
            # rewritten between them can be gone by the time it is read.  One
            # option fewer for one render is honest; a placeholder would not
            # be, and there is nothing to show under a name that no longer
            # names anything.
            continue
        entries.append(
            (
                _ProfileOption(
                    name=name,
                    # Amendment A-3.  A deployed config whose profiles predate
                    # this phase carries an empty human name: startup
                    # generation only runs on a bare default config, so it is
                    # skipped there, and a blank option is worse than a raw
                    # profile name.  ``saneless auto-profiles --force`` is what
                    # backfills the text, and the how-to says so.
                    label=profile.label or name,
                    description=profile.description,
                ),
                classify_source(profile.source),
            )
        )
    sheet_fed = not any(kind is SourceKind.FLATBED for _, kind in entries)
    if sheet_fed:
        # A stable sort, so configuration order survives inside each group and
        # the only thing this changes is which group comes first.
        entries.sort(key=lambda entry: not entry[1].uses_feeder)
    return tuple(option for option, _ in entries)


@router.get("/")
def index(request: Request) -> Response:
    """
    Render the main page with scan form, status, and job history.

    Populates profile selector from the worker's profile set (read under its
    profile lock, D-19), fetches tags and correspondents from cache or
    paperless-ngx, and loads recent job history from the database.
    """
    state = request.app.state
    # D-05: the refresher only probes while a page says someone is looking, so
    # every route that renders the strip has to stamp this.  Without it the
    # lazy thread returns at its first guard for ever and the strip never
    # leaves its cold-start rows.
    state.refresher.note_watcher()
    profiles = _profile_options(state.worker)
    tags = _get_cached_or_fetch(state.cache, state.paperless, "tags")
    correspondents = _get_cached_or_fetch(
        state.cache, state.paperless, "correspondents"
    )

    status = _status_context(state.worker, state.job_store, _status_facts(request))

    jobs = state.job_store.list_recent(limit=50)

    return state.templates.TemplateResponse(
        request,
        "index.html",
        {
            "profiles": profiles,
            # Which option the page opens on, and the sentence that goes with
            # it.  A browser selects the first option when none is marked, so
            # naming the first one here is the only way the highlighted option
            # and the description beneath it cannot disagree on first paint --
            # and after the D-21 regrouping the first option is no longer
            # necessarily the first profile in the config file.
            "selected": profiles[0].name if profiles else "",
            "selected_description": profiles[0].description if profiles else "",
            "tags": tags,
            "correspondents": correspondents,
            **status,
            **_checks_context(state),
            "jobs": jobs,
            # The title input's maxlength; templates own no vocabulary (ROBU-08).
            "title_max_length": TITLE_MAX_LENGTH,
            # The reason line's copy, which only the full page renders: it is
            # never an out-of-band swap target, so no status response needs it
            # and it never has to exist as an empty placeholder (UI-SPEC S8).
            # The template reads the string and decides nothing; the flag that
            # says whether to render it comes from the status context.
            "scan_blocked_reason": SCAN_BLOCKED_REASON,
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


def _record_refused_submit(
    job_store: JobStore, form: _ScanForm, *, error: str
) -> str | None:
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
        The id of the row written, or None when the store refused the write --
        in which case the rendered error must neither reload Job History nor
        name a row the user will not find there.

    """
    try:
        job = job_store.create_rejected_job(
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
        return None
    return job.id


def _reject_created_job(
    worker: ScanWorker, job_store: JobStore, job_id: str, *, error: str
) -> str | None:
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
        The id of the row now recording the refusal, or None when the store
        refused the write.  None means the row is still PENDING and owed to the
        worker, so the rendered error must neither reload Job History yet nor
        name a row that does not yet say it was rejected.

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
        return None
    return job_id


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
    full, 503 when the worker is down or degraded (ROBU-02, D-05, D-11), and
    503 with its own message when the paperless-ngx API token is a placeholder
    nobody replaced, which is the one failure certain to waste paper because
    the pages would be scanned and then have nowhere to go (APPL-07, D-15).
    The rendered error reloads Job History only when that row was written.

    Args:
        request: The incoming HTTP request.
        profile: Scan profile name.
        title: Document title; when blank, the profile's title, else
            'Scan <time>' (D-16).
        tags: List of paperless-ngx tag IDs.
        correspondent: Optional paperless-ngx correspondent ID.

    Raises:
        RequestRejected: The profile is unknown, or the submit was refused.

    """
    state = request.app.state
    # One locked lookup both validates the profile and yields its title, so
    # there is no check-then-read gap for a profile rewrite to fall into.
    found = state.worker.get_profile(profile)
    if found is None:
        raise RequestRejected(RequestRejection.UNKNOWN_PROFILE)
    title = resolve_job_title(title, found, now=datetime.now(tz=UTC))
    form = _ScanForm(
        profile=profile, title=title, tags=tags, correspondent=correspondent
    )

    # D-15: the route guard is the enforcement and the disabled Scan button is
    # only a courtesy, so this refusal holds for curl, for a script, and for a
    # browser whose ``disabled`` attribute was removed in devtools.  It is
    # unconditional -- a configured consume directory does not buy an exception
    # -- and it sits ahead of ``create_job`` so a scan that could never upload
    # leaves exactly one row: the REJECTED one, which Job History shows so the
    # attempt is visible rather than silently swallowed (Phase 26 D-05).
    # The degraded-worker rejection is deliberately not reused here, and this
    # comment names it in prose rather than as the symbol so a grep for that
    # member still counts only the places that raise it: "the scan service was
    # unavailable" is untrue when the service is fine and nobody set the token,
    # and it would send a household member looking for a broken server.  The
    # status is 503, matching the two existing refuse-to-start rejections, so
    # htmx response handling and the history reload behave identically; a 4xx
    # would imply the request was at fault, which it was not.
    #
    # This is the web layer's third place that unwraps the configured token,
    # after the PaperlessClient build in ``web/app.py`` and the Paperless check
    # in ``checks.py``.  The value goes to the predicate and nowhere else: it is
    # never logged, rendered, echoed or put in the job row, whose text names the
    # problem and the file to edit and never the secret (ASVS V7, CFG-05).
    if is_placeholder_token(state.settings.paperless.token.get_secret_value()):
        written = _record_refused_submit(
            state.job_store, form, error=TOKEN_UNSET_JOB_ERROR
        )
        raise RequestRejected(RequestRejection.TOKEN_UNSET, job_id=written)

    unhealthy = _unhealthy_rejection(state.worker.health)
    if unhealthy is not None:
        rejection, error = unhealthy
        written = _record_refused_submit(state.job_store, form, error=error)
        raise RequestRejected(rejection, job_id=written)

    # D-23's mint rule: a token is minted on the first submit from a browser
    # and reused for every later job from it, so two tabs on one device do not
    # disown each other.  It is recorded on the row either way; only a mint
    # reaches the response as a cookie.
    presented = _presented_owner(request)
    owner = presented or secrets.token_urlsafe(32)
    job = state.job_store.create_job(
        profile=form.profile,
        title=form.title,
        tags=form.tags,
        correspondent=form.correspondent,
        owner_token=owner,
    )
    # Annotated because app.state is untyped; assert_never needs the real type.
    result: SubmitResult = state.worker.submit(job)
    match result:
        case SubmitResult.ACCEPTED:
            # A job created by this request cannot have a flip answer yet.
            # Only a successful scan clears the status-message slot (D-03), and
            # only a successful scan carries the strip out-of-band: the scanner
            # has just become busy, so D-08's paused note is due now rather than
            # at the end of the cache's TTL.  The strip's own context rides
            # along because the partial is rendered inside this response.
            response = state.templates.TemplateResponse(
                request,
                "partials/status_response.html",
                {
                    # The created job is the followed job, so the poll URL this
                    # browser is handed names it and the status area keeps
                    # reporting the scan this person started (D-25).
                    **_status_context(
                        state.worker,
                        state.job_store,
                        replace(
                            _status_facts(request, followed_job_id=job.id),
                            # The token this submit is owned by, which is the
                            # minted one when the browser presented none: the
                            # cookie carrying it has not reached the browser
                            # yet, so the request cannot present it.
                            owner_token=owner,
                        ),
                    ),
                    "clear_message": True,
                    "refresh_checks": True,
                    **_checks_context(state),
                },
            )
            if presented is None:
                response.set_cookie(
                    OWNER_COOKIE,
                    owner,
                    httponly=True,
                    samesite="lax",
                    path="/",
                )
            return response
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
    raise RequestRejected(rejection, job_id=written)


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
        _status_context(state.worker, state.job_store, _status_facts(request)),
    )


@router.get("/api/jobs/{job_id}/status")
def followed_job_status(request: Request, job_id: str) -> Response:
    """
    Poll the status of the job this browser submitted (D-25).

    Declared after ``/api/jobs/current/status`` so that literal path keeps
    winning: a browser that submitted nothing still gets today's route and
    today's current-or-most-recent inference.

    The path parameter is an opaque store lookup key and nothing else.  It
    never builds a filesystem path, a URL or a template name, so there is no
    traversal surface to guard (T-30-61), and an id naming no row is a
    fallback rather than a 404 by design: a browser whose job has been pruned
    degrades to the inferred rendering, and a 404 would additionally confirm to
    a caller which ids exist.

    Everything else matches ``current_job_status``: the Scan button rides along
    out-of-band (ROBU-04) and ``#status-message`` is left alone (D-03).

    Args:
        request: The incoming HTTP request.
        job_id: The job this browser is following.

    Returns:
        The status partial rendered for that job.

    """
    state = request.app.state
    return state.templates.TemplateResponse(
        request,
        "partials/status_response.html",
        _status_context(
            state.worker,
            state.job_store,
            _status_facts(request, followed_job_id=job_id),
        ),
    )


@router.get("/api/checks")
def get_checks(request: Request) -> Response:
    """
    Render the status strip from the cache.

    This is the target of the cold-start poll, and it is a cache read: it never
    probes, so no number of open browser tabs can raise the probe rate above
    the cache's TTL (D-04, T-30-26).  The poll ends itself -- the body this
    returns once results exist carries no ``hx-trigger``, so the swap that
    installs it is the last one.
    """
    state = request.app.state
    state.refresher.note_watcher()
    return state.templates.TemplateResponse(
        request,
        "partials/checks.html",
        _checks_context(state),
    )


@router.post("/api/checks/refresh")
def refresh_checks(request: Request) -> Response:
    """
    Re-probe every check now, bypassing the TTL, and render the result (D-09).

    This is the one handler in this module allowed to probe, and the bypass is
    the whole point of the button.  Routing the click through the refresher's
    policy instead would do nothing for the first thirty seconds after a page
    load -- ``_tick`` returns early on a fresh cache -- which is precisely when
    somebody who has just plugged the scanner back in presses it.

    During a scan it re-runs only the checks that do not touch the scanner: the
    gate is tried without blocking exactly as the refresher tries it, and a
    failed attempt sets ``skip_scanner``.  An explicit click does not get to
    defeat the exclusive-scanner rule, because nothing in the SANE backend
    mutually excludes two callers and a status probe landing mid-scan is a
    second caller into the same C library.

    ``CrossOriginGuard`` is app-wide middleware on every non-safe method
    (``web/app.py``), so this POST inherits the cross-site check and must
    **not** add a per-route dependency: a dependency is something a route added
    later can forget, and the middleware is not (T-30-46, D-23).

    A registry that raised is logged and stores nothing, leaving the previous
    entry in place.  ``run_checks`` catches its own per-check failures, so
    reaching that handler means the registry itself broke -- and a strip that
    blanked would be worse than one still showing what was true a moment ago.
    """
    state = request.app.state
    state.refresher.note_watcher()
    gate = state.worker.scanner_gate
    acquired = gate.acquire(blocking=False)
    try:
        context = replace(state.refresher.build_context(), skip_scanner=not acquired)
        results = run_checks(context)
    except Exception:
        # No exception text goes near the cache or the page (ASVS V7).
        logger.exception("Check refresh failed; keeping the previous results")
    else:
        state.checks.store(results)
    finally:
        if acquired:
            gate.release()
    return state.templates.TemplateResponse(
        request,
        "partials/checks.html",
        _checks_context(state),
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


@router.get("/api/profiles/description")
def get_profile_description(request: Request, profile: str) -> Response:
    """
    Return the sentence that explains one profile, for the select's help slot.

    The text is read from the loaded ``ProfileConfig`` rather than re-derived
    from the source name, so an operator who took a profile over -- by removing
    ``auto_generated`` and writing their own wording -- sees their sentence and
    not the generated one.

    ``profile`` is a query parameter and not a path segment, precisely so there
    is no traversal surface to reason about: the name never builds a path, a
    URL or a template name.  It is validated against the known profile set
    before any work, by one locked lookup that both checks the name and yields
    the profile, leaving no check-then-read gap -- the same shape ``start_scan``
    uses.  An unknown name is a 422 (T-30-69).

    Args:
        request: The incoming HTTP request.
        profile: The profile name whose description is wanted.

    Returns:
        The description text alone, with no wrapper element.

    Raises:
        RequestRejected: The profile is not configured.

    """
    state = request.app.state
    found = state.worker.get_profile(profile)
    if found is None:
        raise RequestRejected(RequestRejection.UNKNOWN_PROFILE)
    return state.templates.TemplateResponse(
        request,
        "partials/profile_description.html",
        {"description": found.description},
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

    A Continue from a browser that does not hold the job's owner token is
    dropped in exactly the same way, and for the same reason (D-24): the
    household member who did not load the paper must not be able to start
    pass B, and must not be shown a failure for trying either.  A job whose
    ``owner_token`` is NULL is unowned and anyone may answer it.

    The response re-renders the Scan button out-of-band from server state
    (ROBU-04) and leaves ``#status-message`` alone (D-03).
    """
    state = request.app.state
    presented = _presented_owner(request)
    claimed = False
    if _owner_answers(presented, state.job_store.get_job(job_id)):
        claimed = state.worker.continue_flip(job_id)
    return state.templates.TemplateResponse(
        request,
        "partials/status_response.html",
        _status_context(
            state.worker,
            state.job_store,
            _status_facts(
                request,
                claimed=(job_id, FlipOutcome.CONTINUED) if claimed else None,
            ),
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

    An Abort from a browser that does not hold the job's owner token is
    dropped the same way (D-24): someone else's scan is not theirs to stop.
    A job whose ``owner_token`` is NULL is unowned and anyone may answer it.

    The response re-renders the Scan button out-of-band from server state
    (ROBU-04) and leaves ``#status-message`` alone (D-03).
    """
    state = request.app.state
    presented = _presented_owner(request)
    claimed = False
    if _owner_answers(presented, state.job_store.get_job(job_id)):
        claimed = state.worker.abort_flip(job_id)
    return state.templates.TemplateResponse(
        request,
        "partials/status_response.html",
        _status_context(
            state.worker,
            state.job_store,
            _status_facts(
                request,
                claimed=(job_id, FlipOutcome.ABORTED) if claimed else None,
            ),
        ),
    )
