"""HTTP route handlers for the saneless web UI."""

from __future__ import annotations

import html
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Final, assert_never

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BeforeValidator
from pydantic_core import PydanticCustomError

# A runtime import although only annotations use it, because FastAPI resolves
# each route's return annotation when the decorator runs, and a Response it
# cannot resolve becomes a response model that makes app.openapi() raise.
# runtime-evaluated-decorators in pyproject.toml tells ruff the same.
from starlette.responses import HTMLResponse, Response

from saneless.checks import (
    POLL_PROBE_ATTEMPT_CAP,
)
from saneless.config import PaperlessId, resolve_job_title
from saneless.scan_metadata import resolve_scan_metadata
from saneless.text_safety import has_control_characters
from saneless.vocabulary import (
    NO_SCRIPT_LINE,
    QUEUE_FULL_JOB_ERROR,
    TAG_FILTER_LABEL,
    TITLE_MAX_LENGTH,
    WORKER_DEGRADED_JOB_ERROR,
    WORKER_DOWN_JOB_ERROR,
    ErrorCategory,
    FlipOutcome,
    JobState,
    PassAnswer,
    RequestRejection,
    SubmitResult,
    WorkerHealth,
    progress_label,
    scan_hold_reason,
    worker_health_detail,
)
from saneless.web import (
    metadata_view,
    owner,
    profile_view,
    scan_block,
    status_view,
    strip_view,
)
from saneless.web.errors import (
    RETRY_AFTER_SECONDS,
    TITLE_CONTROL_TYPE,
    BrowserNavigationRefused,
    RequestRejected,
)
from saneless.web.job_view import (
    JobView,
)
from saneless.web.refresher import ManualProbe
from saneless.web.services import PaperlessTestAnswer, services
from saneless.worker import ScanOptions

if TYPE_CHECKING:
    from saneless.job import JobStore
    from saneless.worker import ScanWorker

__all__ = ["router"]

logger = logging.getLogger(__name__)

# Every handler is a plain ``def``, so FastAPI runs it on its threadpool: each
# one calls blocking code (httpx2, sqlite, the worker), and a slow paperless-ngx
# call must not stall ``/health`` or the status poll.  The shared state they
# touch is locked.
router = APIRouter()

TAGS_MAX_COUNT: Final = 100
"""
The cap on how many tag ids one request may carry, applied at the boundary.

The ids are stored as JSON on the job row, so an unbounded list would let one
request, refused or not, write as much as it liked.  A longer list is a 422
before the handler body runs; ``PaperlessId`` bounds each id.
"""

_TAGS_FORM_DEFAULT = Form(default=[], max_length=TAGS_MAX_COUNT)

# htmx sends an ``hx-include``'s values in the query string of a GET and in the
# body of a POST, so the GET that renders the tag list needs its own default.
_TAGS_QUERY_DEFAULT = Query(default=[], max_length=TAGS_MAX_COUNT)

TAG_FILTER_MAX_LENGTH: Final = 100
"""
The cap on the tag filter, applied at the boundary before any work.

The filter is a substring test against tag names, so anything longer is a
mistake or an attempt to make the server work for nothing.
"""

LISTS_LOADING_MARKER: Final = "(lists loading)"
"""
What the page's profile markers say while its lists are still loading.

A status poll can release Scan before the lazy list load lands, and a marker
naming the opening profile would then file the empty controls as that
profile's answer.  This value names no profile, so ``start_scan`` applies the
submitted profile's defaults, which is what the untouched form files anyway.

It is not empty, because an empty field arrives as no marker, which means the
values are taken as given.  A bare TOML key and ``auto-profiles`` cannot spell
it, though a quoted TOML key can.
"""

CHECK_AGAIN_WAIT_SECONDS: Final = 2.5
"""
The most seconds Check again waits for the probe it handed to the refresher.

A slower probe returns the strip with its settling poll, which collects the
answer later.  The wait stays far inside the ten seconds an idle server is
given to stop, and only a click the manual-refresh floor admits waits at all.
Read at call time, not bound where it is used.
"""


@router.get("/")
def index(request: Request) -> Response:
    """
    Render the main page: scan form, status area, status strip and job history.

    The page opens on ``default``, or on the profile standing in for a hidden
    ``default``.  It makes no paperless-ngx call: the tag list and the
    correspondent select render as loading, and a hidden loader asks
    ``/api/metadata`` for both, ticking the profile's defaults so an untouched
    submit files what ``saneless scan`` would.  Scan is held, with a line
    saying why, until that answer lands, whether it brings the lists or says
    they could not be loaded.
    """
    svc = services(request)
    # The refresher probes only while a page says someone is looking, so every
    # route that renders the strip stamps this.  Without it the strip never
    # leaves its cold-start rows.
    svc.refresher.note_watcher()
    choices = profile_view.profile_options(svc.worker)
    # Found by the name the choices chose, so the select, the sentence beneath
    # it and the Multiple pages field all read the same profile.
    opening_option = next(
        (option for option in choices.options if option.name == choices.opening),
        None,
    )
    # The lists are never fetched here; the lazy list load brings the rows.
    show_tags = svc.settings.web.show_tags
    show_correspondent = svc.settings.web.show_correspondent
    lists_loading = show_tags or show_correspondent

    # The page follows the newest active job this browser owns, else the
    # current job.  Its poll URL carries the token of this rendering, so the
    # first poll is a 204 and the page's own rendering stays in place.
    live = status_view.status_context(
        svc.worker,
        svc.job_store,
        status_view.status_facts(
            request,
            followed_job_id=status_view.owned_active_job_id(
                svc.worker, svc.job_store, owner.presented_owner(request)
            ),
        ),
    )
    _, status = status_view.with_poll(request, live)
    # A job no longer active is reported as the last scan.  The poll token
    # stays the live rendering's: an area with no active job does not poll.
    shown = live["job"]
    last_scan = status_view.last_scan_context(
        shown if isinstance(shown, JobView) else None
    )
    block = scan_block.block_for(svc.settings)

    jobs = profile_view.history_views(request)

    return svc.templates.TemplateResponse(
        request,
        "index.html",
        {
            "profiles": choices.options,
            # Named here because a browser selects the first option when none
            # is marked, and the first is seldom ``default``: the select and the
            # description beneath it must agree on first paint.
            "selected": choices.opening,
            "selected_description": (
                opening_option.description if opening_option is not None else ""
            ),
            # Never ticked: the choice is made per scan.
            **profile_view.multi_page_field(
                manual_duplex=opening_option is not None
                and opening_option.manual_duplex,
                ticked=False,
            ),
            **metadata_view.no_tag_list(),
            **metadata_view.no_correspondent_options(shown=show_correspondent),
            "tags_loading": show_tags,
            # Until the lazy list load replaces them, the markers answer for no
            # profile.
            "lists_loading_marker": LISTS_LOADING_MARKER,
            "correspondents_loading": show_correspondent,
            # A shown list not yet arrived holds Scan, beside an active job and
            # a blocked appliance.
            "lists_loading": lists_loading,
            # A blocked appliance's own reason outlasts the lists, so the page
            # renders that one alone.
            "scan_hold_reason": (
                None
                if block is not None
                else scan_hold_reason(tags=show_tags, correspondents=show_correspondent)
            ),
            **status,
            **last_scan,
            **strip_view.checks_context(svc),
            "jobs": jobs,
            # Templates own no vocabulary.
            "title_max_length": TITLE_MAX_LENGTH,
            "no_script_line": NO_SCRIPT_LINE,
            # The same bound the filter routes refuse a longer filter with.
            "tag_filter_label": TAG_FILTER_LABEL,
            "tag_filter_max_length": TAG_FILTER_MAX_LENGTH,
            # Only the full page renders the reason line, so no status response
            # needs it.  Whether to render it comes from the status context.
            "scan_blocked_reason": block.reason if block is not None else "",
            # A configured key, not a per-browser toggle: a hidden control is
            # left out of the markup, and ``start_scan`` applies the profile's
            # default for exactly the control not on the page.
            "show_tags": show_tags,
            "show_correspondent": show_correspondent,
            # A page load moves no focus, even while a prompt is open.  The poll
            # token never sees this, so the first poll after the page is a 204.
            "page_render": True,
        },
    )


@router.get("/health", response_model=None)
def health(request: Request) -> dict[str, str] | JSONResponse:
    """
    Health check endpoint for container orchestration.

    Returns 200 with ``{"status": "ok"}`` when the worker is healthy.
    Otherwise 503, whose detail distinguishes a failing job store ("job store
    failing") from a dead worker thread ("worker thread is down").
    Docker's HEALTHCHECK marks the container unhealthy on a 503 but does not
    restart it, as ``docs/reference/docker.md`` explains.
    """
    worker_health = services(request).worker.health
    if worker_health is WorkerHealth.HEALTHY:
        return {"status": "ok"}
    return JSONResponse(
        status_code=503,
        content={"status": "error", "detail": worker_health_detail(worker_health)},
    )


def _paperless_test_error(exc: BaseException) -> PaperlessTestAnswer:
    """
    Build the connection test's 500 answer, logging the class name only.

    A failure while running the test is a failure inside saneless, so it is a
    server error rather than a bad gateway; an answer paperless-ngx gave is a
    200.  Class name only, by the rule above
    ``metadata_view.cached_list_or_none``.
    """
    detail = type(exc).__name__
    logger.warning("Paperless connection test failed: %s", detail)
    return PaperlessTestAnswer(
        status_code=500, body={"status": "error", "detail": detail}
    )


@router.get("/api/paperless/test")
def paperless_test(request: Request) -> JSONResponse:
    """
    Answer the paperless-ngx connection status.

    Returns 200 with one ``ConnectionStatus`` value as its status.  A
    redirect's target is never in the body: it is upstream text, and this
    endpoint needs no login.  A failure inside saneless is a 500 naming the
    exception class.

    The answer is shared and reused for ``MIN_MANUAL_REFRESH_SECONDS``, error
    included, so a loop against this unauthenticated endpoint costs one
    token-bearing request to paperless-ngx per window, and concurrent callers
    share one probe.  The probe runs on ``metadata_view.REQUEST_FETCH_TIMEOUT``,
    so it ends within seconds against an unreachable paperless-ngx.

    Only callers before any answer exists wait, two at a time, for at most
    ``PAPERLESS_TEST_WAIT_SECONDS``: longer than the probe budget and shorter
    than the shutdown budget.  A caller that outwaits that bound, or gets no
    place to wait, is a 503 naming ``TimeoutError`` with ``Retry-After``, and
    that answer is not shared.
    """
    svc = services(request)

    def probe() -> PaperlessTestAnswer:
        try:
            status = svc.paperless.test_connection(
                timeout=metadata_view.REQUEST_FETCH_TIMEOUT
            )
        except Exception as exc:
            return _paperless_test_error(exc)
        return PaperlessTestAnswer(status_code=200, body={"status": str(status)})

    try:
        answer = svc.paperless_test_result.get(probe)
    except TimeoutError as exc:
        detail = type(exc).__name__
        logger.warning("Paperless connection test got no turn: %s", detail)
        return JSONResponse(
            {"status": "error", "detail": detail},
            status_code=503,
            headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
        )
    return JSONResponse(answer.body, status_code=answer.status_code)


@dataclass(frozen=True, slots=True)
class _ScanChoice:
    """
    Which profile a scan submission names, and whether it is multi-page.

    These decide how the job runs, and the two markers say whose defaults the
    tag and correspondent controls were showing: facts about the profile, not
    the metadata, which is why the title and tags do not travel with them.

    Attributes:
        profile: The scan profile name.
        multi_page: Whether Multiple pages was ticked.
        tags_profile: The profile whose defaults the tag list showed, or None
            when the submit carried no marker.
        correspondent_profile: The profile whose default the correspondent
            select showed, or None when the submit carried no marker.

    """

    profile: str
    multi_page: bool
    tags_profile: str | None = None
    correspondent_profile: str | None = None

    def shows_own_defaults(self, marker: str | None) -> bool:
        """
        Say whether a control's values belong to the submitted profile.

        A marker naming another profile, or ``LISTS_LOADING_MARKER``, means
        the control was still showing other defaults: a profile-change swap
        had not landed, or the lists had not loaded.  No marker means a script
        posted its own values, and those are taken as given.

        Args:
            marker: The control's profile marker as submitted, or None.

        Returns:
            False only when the marker names a different profile.

        """
        return marker is None or marker == self.profile


def _scan_choice(
    *,
    profile: Annotated[str, Form()],
    multi_page: Annotated[bool, Form()] = False,
    tags_profile: Annotated[str | None, Form()] = None,
    correspondent_profile: Annotated[str | None, Form()] = None,
) -> _ScanChoice:
    """
    Read the profile, the Multiple pages choice and the profile markers.

    An unticked checkbox sends nothing at all, so its absence is False.
    """
    return _ScanChoice(
        profile=profile,
        multi_page=multi_page,
        tags_profile=tags_profile,
        correspondent_profile=correspondent_profile,
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
        and the job-row error text that records it.

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

    The row is written already ``ERROR`` with ``ErrorCategory.REJECTED``, which
    keeps it out of the status area while Job History shows the attempt.  It is
    a single ``INSERT``: create-then-finish would be two transactions, and a
    failure between them would leave a PENDING row disabling Scan for good.

    Returns:
        The row's id, or None when the store refused the write, in which case
        the rendered error must not reload Job History or name the row.

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
    Mark a job row the worker then refused as a REJECTED error.

    The row had to exist before ``put_nowait``, or the worker could dequeue an
    id with no row.  A failed write cannot be dropped, because the row would
    stay PENDING and disable Scan until a restart, so it is owed to the worker,
    which records it on its next idle tick.

    Returns:
        The row's id, or None when the write is owed to the worker, in which
        case the rendered error must not reload Job History or name the row.

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


# Nothing reads or renders this message: the error handler maps
# ``TITLE_CONTROL_TYPE`` to TITLE_HAS_CONTROL, whose fixed sentence the page shows.
_TITLE_CONTROL_MESSAGE: Final = "the title contains a control character"


def _refuse_control_characters(value: str) -> str:
    """
    Refuse a title that holds any control character, tab included.

    Refused, never repaired: stripping characters would store a title the
    person did not type, and a stored control character could later rewrite a
    terminal or split a log line.  The error never carries the value.

    Raises:
        PydanticCustomError: The title holds a control character.

    """
    if has_control_characters(value):
        raise PydanticCustomError(TITLE_CONTROL_TYPE, _TITLE_CONTROL_MESSAGE)
    return value


def _queued_status_fallback(job_id: str) -> str:
    """
    Build the status area for a queued job without rendering a template.

    ``start_scan`` answers this when its normal response fails to render after
    the worker accepted the job.  It carries the poll attributes
    ``partials/status.html`` gives a followed active job and the out-of-band
    clear of ``#status-message``, and the first poll renders the full status.
    No exception text reaches it.
    """
    poll_url = html.escape(status_view.poll_url(job_id, seen=""))
    line = html.escape(progress_label(JobState.PENDING))
    return (
        f'<div id="status-area" tabindex="-1" autofocus hx-get="{poll_url}" '
        f'hx-trigger="every {status_view.POLL_INTERVAL_SECONDS}s" hx-swap="outerHTML">\n'
        f'  <p class="busy-line">{line}</p>\n'
        "</div>\n"
        '<div id="status-message" hx-swap-oob="innerHTML"></div>\n'
    )


def _refuse_browser_navigation(request: Request) -> None:
    """
    Refuse a scan form a browser posted by itself, with JavaScript off.

    A navigation sends no ``HX-Request`` and an ``Accept`` naming
    ``text/html``; curl and scripts send ``*/*`` or none.  ``Sec-Fetch-Mode``
    would name a navigation outright, but a browser sends it only to a secure
    origin, and this appliance is usually reached over plain http.

    Raises:
        BrowserNavigationRefused: The request is a browser navigation.

    """
    if request.headers.get("HX-Request") != "true" and "text/html" in (
        request.headers.get("accept", "")
    ):
        raise BrowserNavigationRefused


@router.post("/api/scan", dependencies=[Depends(_refuse_browser_navigation)])
def start_scan(
    request: Request,
    choice: Annotated[_ScanChoice, Depends(_scan_choice)],
    title: Annotated[
        str,
        Form(max_length=TITLE_MAX_LENGTH),
        AfterValidator(_refuse_control_characters),
    ] = "",
    tags: list[PaperlessId] = _TAGS_FORM_DEFAULT,
    correspondent: Annotated[PaperlessId | None, Form()] = None,
) -> Response:
    """
    Start a scan job from the form and answer the status area.

    Input is validated before any job row exists: an overlong title, a title
    holding a control character, too many tags, an unknown profile, or
    Multiple pages on a manual-duplex profile is a 422 and writes nothing.  A
    refused submit records a REJECTED error row and raises: 429 with
    ``Retry-After`` for a full queue, 503 for a down or degraded worker, and
    503 with its own message for an unset paperless-ngx token or URL, which
    would scan pages with nowhere to go.

    An accepted submit's response re-renders the Scan button, clears
    ``#status-message`` and moves focus to the status area.  The worker
    accepting the job is the commit point, so anything that can fail and is
    not about the job is built before the row exists.  If the response cannot
    be rendered after it, a minimal status area still answers 200 and follows
    the job, because "try again" would queue a second scan; either response
    sets the owner cookie, so this browser can answer the job's prompts.

    Args:
        request: The incoming HTTP request.
        choice: The scan profile name and whether Multiple pages was ticked.
        title: Document title; when blank, the profile's title, else
            'Scan <time>'.
        tags: List of paperless-ngx tag IDs, each within ``PaperlessId``'s
            bounds, so an id no paperless-ngx can have is a 422 before any
            job row exists.
        correspondent: Optional paperless-ngx correspondent ID, bounded the
            same way.

    Raises:
        RequestRejected: The profile is unknown, Multiple pages was asked for
            on a manual-duplex profile, or the submit was refused.

    """
    svc = services(request)
    # One locked lookup both validates the profile and yields its title, so
    # there is no check-then-read gap for a profile rewrite to fall into.
    found = svc.worker.get_profile(choice.profile)
    if found is None:
        raise RequestRejected(RequestRejection.UNKNOWN_PROFILE)
    # Refused ahead of every refusal that writes a row: the form would not have
    # made this request, so Job History has no attempt to show.
    if choice.multi_page and profile_view.is_manual_duplex(found):
        raise RequestRejected(RequestRejection.MULTI_PAGE_MANUAL_DUPLEX)
    title = resolve_job_title(title, found, now=datetime.now(tz=UTC))
    # A shown control is the operator's whole answer; a control ``[web]`` hides
    # was never answered, so the profile's default applies, by the policy
    # ``saneless scan`` uses.  The gate is the config key, never the submitted
    # value: an empty list arrives the same whether hidden or cleared.
    #
    # A control whose marker names another profile, or no profile, still held
    # other defaults, so the submitted profile's apply instead.
    tags_answered = svc.settings.web.show_tags and choice.shows_own_defaults(
        choice.tags_profile
    )
    correspondent_answered = (
        svc.settings.web.show_correspondent
        and choice.shows_own_defaults(choice.correspondent_profile)
    )
    metadata = resolve_scan_metadata(
        found,
        tags=tags if tags_answered else None,
        correspondent=correspondent,
        correspondent_given=correspondent_answered,
    )
    form = _ScanForm(
        profile=choice.profile,
        title=title,
        tags=list(metadata.tags),
        correspondent=metadata.correspondent,
    )

    # The route guard is the enforcement; the disabled Scan button is only a
    # courtesy.  It sits ahead of ``create_job``, even with a consume directory
    # configured, so a scan that could never upload leaves one REJECTED row.
    # Its own rejection, not WORKER_DEGRADED, because the service is fine.
    block = scan_block.block_for(svc.settings)
    if block is not None:
        written = _record_refused_submit(svc.job_store, form, error=block.job_error)
        raise RequestRejected(block.rejection, job_id=written)

    unhealthy = _unhealthy_rejection(svc.worker.health)
    if unhealthy is not None:
        rejection, error = unhealthy
        written = _record_refused_submit(svc.job_store, form, error=error)
        raise RequestRejected(rejection, job_id=written)

    owner_token = owner.token_for(owner.presented_owner(request))
    # Ahead of the row: a failure between the two would leave a PENDING row no
    # worker runs.  Built as a scan in progress, because read from the worker
    # before the job exists it would say idle.
    checks = strip_view.checks_context(svc, scan_active=True)
    job = svc.job_store.create_job(
        profile=form.profile,
        title=form.title,
        tags=form.tags,
        correspondent=form.correspondent,
        owner_token=owner_token,
    )
    result = svc.worker.submit(job, ScanOptions(multi_page=choice.multi_page))
    match result:
        case SubmitResult.ACCEPTED:
            # Only an accepted scan carries the strip out-of-band: the scanner
            # has just become busy, so the strip's paused note is due now.
            #
            # The job is queued, so a failure below must not reach the error
            # handler, whose answer says to try again.  Starlette renders the
            # template inside the TemplateResponse constructor.
            try:
                # The poll token renders a template, so it is built inside the
                # guard too.
                _, status = status_view.with_poll(
                    request,
                    status_view.status_context(
                        svc.worker,
                        svc.job_store,
                        replace(
                            status_view.status_facts(request, followed_job_id=job.id),
                            # The minted token when the browser presented none:
                            # the cookie carrying it has not reached the
                            # browser yet.
                            owner_token=owner_token,
                        ),
                    ),
                )
                response = svc.templates.TemplateResponse(
                    request,
                    "partials/status_response.html",
                    {
                        **status,
                        "clear_message": True,
                        "refresh_checks": True,
                        "terminal_reload": True,
                        # Focus follows the press to the scan it started.
                        "focus_status_area": True,
                        **checks,
                    },
                )
            except Exception:
                logger.exception(
                    "Rendering the response for accepted job %s failed; "
                    "answering the minimal queued status instead",
                    job.id,
                )
                fallback = HTMLResponse(
                    status_code=200, content=_queued_status_fallback(job.id)
                )
                owner.set_owner_cookie(fallback, owner_token)
                return fallback
            owner.set_owner_cookie(response, owner_token)
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
    written = _reject_created_job(svc.worker, svc.job_store, job.id, error=error)
    raise RequestRejected(rejection, job_id=written)


@router.get("/api/jobs/current/status")
def current_job_status(
    request: Request,
    seen: Annotated[str, Query()] = "",
    attempt: Annotated[int, Query()] = 0,
    focus: Annotated[str, Query()] = "",
) -> Response:
    """
    Poll the current or most recent job's status.

    The response re-renders the Scan button out-of-band and never clears
    ``#status-message``, which would erase a rejection shown mid-scan.  A poll
    whose ``seen`` is the token of what it would render now is a 204, which
    htmx leaves unswapped, so focus, a pressed key and the live region survive
    it; htmx does not re-process an unswapped element, so later polls present
    the same token until something changes.

    A poll that cannot build its status gets status_view's lost-contact
    fallback, which backs off and never stops, rather than ``render_error``.
    Retargeted into ``#status-message`` an error would rewrite the page's alert
    every tick, and swapped over ``#status-area`` it would delete the element
    the scan form targets.

    Args:
        request: The incoming HTTP request.
        seen: The token of the rendering the polling element shows.
        attempt: How many polls in a row have failed, as the previous fallback
            named it.  Clamped into
            ``0..len(status_view.STATUS_BACKOFF_SECONDS)``, never refused, and
            reset by the first poll that succeeds, whose URL carries none.
        focus: ``status_view.FOCUS_SCAN`` when a claimed Abort asked for focus
            on the Scan button; any other value is ignored and never echoed.

    Returns:
        The status partial, the lost-contact fallback, or an empty 204 when
        nothing has changed.

    """
    return status_view.answer_status_poll(
        request,
        None,
        seen=seen,
        attempt=attempt,
        focus_scan=focus == status_view.FOCUS_SCAN,
    )


@router.get("/api/jobs/{job_id}/status")
def followed_job_status(
    request: Request,
    job_id: str,
    seen: Annotated[str, Query()] = "",
    attempt: Annotated[int, Query()] = 0,
    focus: Annotated[str, Query()] = "",
) -> Response:
    """
    Poll the status of the job this browser submitted.

    Declared after ``/api/jobs/current/status`` so that literal path wins.
    ``job_id`` is an opaque store key that never builds a path, a URL or a
    template name.  An id naming no row falls back to the inferred rendering
    rather than a 404, which would confirm to a caller which ids exist.

    Otherwise it answers as ``current_job_status`` does.  Its fallback cannot
    confirm the id against the store, so it echoes the id only after it parses
    as a UUID, and otherwise polls the current route.

    Args:
        request: The incoming HTTP request.
        job_id: The job this browser is following.
        seen: The token of the rendering the polling element shows.
        attempt: How many polls in a row have failed, clamped as
            ``current_job_status`` clamps it.
        focus: The pending request for focus on the Scan button, read as
            ``current_job_status`` reads it.

    Returns:
        The status partial rendered for that job, the lost-contact fallback,
        or an empty 204 when nothing has changed.

    """
    return status_view.answer_status_poll(
        request,
        job_id,
        seen=seen,
        attempt=attempt,
        focus_scan=focus == status_view.FOCUS_SCAN,
    )


@router.get("/api/checks")
def get_checks(
    request: Request,
    attempt: Annotated[int, Query()] = 0,
) -> Response:
    """
    Render the status strip from the cache.

    The cold-start poll's target, and only a cache read, so no number of open
    tabs can raise the probe rate.  Once results exist the body carries no
    ``hx-trigger``, which ends the poll.

    The attempt count lives in the URL, not on the server, and is clamped
    rather than refused: only the clamped number is used or rendered, and past
    the cap it lands on the give-up body.  A failure in the watcher stamp or
    the context build renders ``strip_view.checks_fallback_context`` at 200,
    because the polling element cannot use an error response.

    Args:
        request: The incoming request.
        attempt: Which attempt this is, counted from zero.  Clamped into
            ``0..POLL_PROBE_ATTEMPT_CAP``, the larger of the two caps, so a
            chain waiting on a live probe is not cut short at
            ``POLL_ATTEMPT_CAP``.

    Returns:
        The strip body, for an ``outerHTML`` swap.

    """
    svc = services(request)
    counted = min(max(attempt, 0), POLL_PROBE_ATTEMPT_CAP)
    try:
        svc.refresher.note_watcher()
        context = strip_view.checks_context(svc, attempt=counted)
    except Exception:
        logger.exception("Failed to render the status strip")
        context = strip_view.checks_fallback_context()
    return svc.templates.TemplateResponse(
        request,
        "partials/checks.html",
        context,
    )


@router.post("/api/checks/refresh")
def refresh_checks(request: Request) -> Response:
    """
    Re-probe every check now, bypassing the TTL, and render the strip.

    The manual refresh probes in one place, through the refresher: the click
    asks ``request_probe``, the refresher's own thread runs the probe without
    the TTL, and this thread waits at most ``CHECK_AGAIN_WAIT_SECONDS``.  A
    probe still pending leaves ``probe_in_flight`` true, so the one render
    below carries the settling poll that collects it.

    A click landing while a probe is in flight collapses into it and gives its
    manual-refresh claim back, because it caused no traffic.
    ``release_manual_claim`` clears only the stamp this request wrote, and the
    claim is tested with ``is not None`` because a ``time.monotonic()`` stamp
    can be 0.0 just after boot.

    The claim's floor bounds a serial loop against this unauthenticated
    endpoint, which would otherwise mean unbounded paperless-ngx requests,
    saned dials and filesystem writes, and could park a scan behind the
    scanner gate, which is not a fair lock.  A too-soon click re-renders the
    current strip, because it is early, not wrong.  During a scan only the
    checks that do not touch the scanner re-run, decided by the worker's
    record of a job in flight.

    Only the render is guarded: a failure there renders
    ``strip_view.checks_fallback_context`` at 200, so the strip and its button
    stay.  A failure before it is a failed click, reported in the message slot.
    ``CrossOriginGuard`` middleware covers this POST; a per-route dependency
    is something a later route could forget.
    """
    svc = services(request)
    svc.refresher.note_watcher()
    claim = svc.checks.claim_manual_refresh()
    if (
        claim is not None
        and svc.refresher.request_probe(wait=CHECK_AGAIN_WAIT_SECONDS)
        is ManualProbe.COLLAPSED
    ):
        svc.checks.release_manual_claim(claim)
    try:
        context = strip_view.checks_context(svc)
    except Exception:
        logger.exception("Failed to render the status strip")
        context = strip_view.checks_fallback_context()
    return svc.templates.TemplateResponse(
        request,
        "partials/checks.html",
        context,
    )


@router.get("/api/tags")
def get_tags(
    request: Request,
    q: Annotated[str, Query(max_length=TAG_FILTER_MAX_LENGTH)] = "",
    tags: list[PaperlessId] = _TAGS_QUERY_DEFAULT,
) -> Response:
    """
    Render the tag checkbox list, optionally narrowed by a filter.

    Reads the cache, else fetches within the short request budget.  On an API
    error it renders the last list fetched, or a line saying the tags could
    not be loaded with the ticked ids still ticked; it never claims
    paperless-ngx has no tags when it could not ask.  The response is the
    whole ``outerHTML`` swap target; ``metadata_view.tag_list_context`` says
    why the selection rides along and ``q`` reaches neither paperless-ngx nor
    the body.

    Args:
        request: The incoming HTTP request.
        q: Filter text; a value over ``TAG_FILTER_MAX_LENGTH`` is a 422 before
            any work happens.
        tags: The tag ids the browser reports as currently ticked.

    """
    svc = services(request)
    return svc.templates.TemplateResponse(
        request,
        "partials/tags.html",
        metadata_view.tag_list_context(
            svc, q=q, selected=tags, timeout=metadata_view.REQUEST_FETCH_TIMEOUT
        ),
    )


def _blank_as_none(value: object) -> object:
    """
    Read an empty query value as none, before it is validated.

    "No correspondent" has an empty value, which htmx still sends in a GET as
    ``correspondent=``.  FastAPI turns an empty value into the default for a
    form field but not for a query parameter, so without this the page's own
    request would fail the integer check.
    """
    return None if value == "" else value


@router.get("/api/correspondents")
def get_correspondents(
    request: Request,
    correspondent: Annotated[
        PaperlessId | None, BeforeValidator(_blank_as_none), Query()
    ] = None,
) -> Response:
    """
    Render the correspondent options for the select.

    Reads the cache, else fetches within the short request budget.  On an API
    error it returns the last list fetched, or only the option that means
    none, with the help line out of band saying the list could not be loaded.
    The current choice rides along and is rendered selected again, so filling
    in the list never changes what is chosen.  A hidden control fetches
    nothing.

    Args:
        request: The incoming HTTP request.
        correspondent: The correspondent currently chosen, if any; an empty
            value, which "No correspondent" sends, is none.

    """
    svc = services(request)
    return svc.templates.TemplateResponse(
        request,
        "partials/correspondents.html",
        metadata_view.correspondent_options_context(
            svc, correspondent, timeout=metadata_view.REQUEST_FETCH_TIMEOUT
        ),
    )


@router.get("/api/profiles/description")
def get_profile_description(request: Request, profile: str) -> Response:
    """
    Return the sentence that explains one profile, for the select's help slot.

    The text is read from the loaded ``ProfileConfig``, so an operator who
    took a generated profile over sees their own wording.  ``profile`` never
    builds a path, a URL or a template name, and one locked lookup both
    validates it and yields the profile, leaving no check-then-read gap.  An
    unknown name is a 422.

    Args:
        request: The incoming HTTP request.
        profile: The profile name whose description is wanted.

    Returns:
        The description text alone, with no wrapper element.

    Raises:
        RequestRejected: The profile is not configured.

    """
    svc = services(request)
    found = svc.worker.get_profile(profile)
    if found is None:
        raise RequestRejected(RequestRejection.UNKNOWN_PROFILE)
    return svc.templates.TemplateResponse(
        request,
        "partials/profile_description.html",
        {"description": found.description},
    )


@router.get("/api/profiles/multi-page")
def get_multi_page_field(
    request: Request, profile: str, *, multi_page: bool = False
) -> Response:
    """
    Re-render the Multiple pages field for a newly chosen profile.

    A tick survives a change to a profile that allows it, so a profile switch
    never silently drops a choice; a manual-duplex profile renders the box
    disabled and unticked, with the reason in place of the help line.  An
    unknown profile is a 422, as in ``get_profile_description``.

    Args:
        request: The incoming HTTP request.
        profile: The profile name now chosen.
        multi_page: Whether the box was ticked before the change.

    Returns:
        The field, wrapper and all.

    Raises:
        RequestRejected: The profile is not configured.

    """
    svc = services(request)
    found = svc.worker.get_profile(profile)
    if found is None:
        raise RequestRejected(RequestRejection.UNKNOWN_PROFILE)
    return svc.templates.TemplateResponse(
        request,
        "partials/multi_page_field.html",
        profile_view.multi_page_field(
            manual_duplex=profile_view.is_manual_duplex(found), ticked=multi_page
        ),
    )


@router.get("/api/profiles/tags")
def get_profile_tags(request: Request, profile: str) -> Response:
    """
    Re-render the tag list ticked with a newly chosen profile's default tags.

    The earlier ticks are replaced, unlike the Multiple pages tick: a
    profile's defaults are what an untouched form means, so the form must not
    show a mix of two profiles.  An unknown profile is a 422 before any fetch.

    Args:
        request: The incoming HTTP request.
        profile: The profile name now chosen.

    Returns:
        The tag list, wrapper and all.

    Raises:
        RequestRejected: The profile is not configured.

    """
    svc = services(request)
    found = svc.worker.get_profile(profile)
    if found is None:
        raise RequestRejected(RequestRejection.UNKNOWN_PROFILE)
    return svc.templates.TemplateResponse(
        request,
        "partials/tags.html",
        {
            **metadata_view.tag_list_context(
                svc,
                q="",
                selected=list(found.default_tags),
                timeout=metadata_view.REQUEST_FETCH_TIMEOUT,
            ),
            # The list's profile marker rides along out-of-band, so the scan
            # can tell whose defaults the ticks are (see ``start_scan``).
            "follows_profile": profile,
        },
    )


@router.get("/api/profiles/correspondent")
def get_profile_correspondent(request: Request, profile: str) -> Response:
    """
    Re-render the correspondent select with a newly chosen profile's default.

    The correspondent twin of ``get_profile_tags``, replacing the earlier
    choice for the same reason.

    Args:
        request: The incoming HTTP request.
        profile: The profile name now chosen.

    Returns:
        The correspondent select, options and all.

    Raises:
        RequestRejected: The profile is not configured.

    """
    svc = services(request)
    found = svc.worker.get_profile(profile)
    if found is None:
        raise RequestRejected(RequestRejection.UNKNOWN_PROFILE)
    return svc.templates.TemplateResponse(
        request,
        "partials/correspondent_select.html",
        {
            **metadata_view.correspondent_options_context(
                svc,
                found.default_correspondent,
                timeout=metadata_view.REQUEST_FETCH_TIMEOUT,
            ),
            "follows_profile": profile,
        },
    )


@router.get("/api/metadata")
def get_metadata(request: Request, profile: str | None = None) -> Response:
    """
    Render both lists, their help, the Scan button and the hold line, at once.

    The page's loader asks here once.  One combined request, because Scan is
    held until both lists are done and a page with no script of its own
    cannot count two responses; every part that depends on them comes out of
    band (see ``partials/metadata_response.html``).  A list that could not be
    loaded carries its own retry, which asks ``probe_metadata``.

    The answer shows the named profile's defaults with both profile markers,
    so whichever of this and a profile-change swap lands last, its list and
    its marker agree.  The Scan button comes from the status context of the
    job this browser follows, so an active job or a blocked appliance still
    holds it.

    The loader polls until answered, so nothing is refused: an error would
    land in the alert slot every tick and hold Scan for good.  A missing or
    unknown profile answers the lists with no ticks, no choice and no markers,
    so a submit gets the submitted profile's own defaults.

    Args:
        request: The incoming HTTP request.
        profile: The profile whose defaults to show.

    Returns:
        Nothing in the loader's place, with the out-of-band parts.

    """
    svc = services(request)
    found = None if profile is None else svc.worker.get_profile(profile)
    if found is not None:
        ticked = list(found.default_tags)
        chosen = found.default_correspondent
        follows = profile
    else:
        logger.warning(
            "The lazy list load named no configured profile; answering the"
            " lists with no profile's defaults"
        )
        ticked, chosen, follows = [], None, None
    tag_list = metadata_view.tag_list_context(
        svc, q="", selected=ticked, timeout=metadata_view.REQUEST_FETCH_TIMEOUT
    )
    options = metadata_view.correspondent_options_context(
        svc, chosen, timeout=metadata_view.REQUEST_FETCH_TIMEOUT
    )
    job, scan_blocked = metadata_view.metadata_scan_state(request)
    return svc.templates.TemplateResponse(
        request,
        "partials/metadata_response.html",
        {
            **tag_list,
            **options,
            "follows_profile": follows,
            "show_tags": svc.settings.web.show_tags,
            "show_correspondent": svc.settings.web.show_correspondent,
            "job": job,
            "scan_blocked": scan_blocked,
        },
    )


@router.get("/api/metadata/probe")
def probe_metadata(
    request: Request, resource: metadata_view.MetadataResource
) -> Response:
    """
    Say whether a list that could not be loaded can be loaded now.

    A list rendered unavailable carries a hidden retry that asks here.  The
    answer is a fresh copy of the retry, so it asks again after
    ``metadata_view.metadata_retry_seconds``; when the list can be loaded the
    answer also fires ``<resource>-recovered``, and the page's own recovery
    element asks for the list with what the form shows at that moment.

    The retry carries nothing of the form and renders only itself, so it can
    never put back a tick or a choice changed while it was in flight.  The
    recovery request yields to the list's profile-change request (see
    ``index.html``), and the fresh retry is sent on recovery too, so a dropped
    recovery is asked again.

    The fetch goes through the cache, whose memory of a failure starts when
    the failed fetch ended, so a retry timed from this answer reaches
    paperless-ngx.  A hidden list has no retry and answers 204.  Nothing is
    refused: the retry polls, and an error would land in the alert slot every
    tick.

    Args:
        request: The incoming HTTP request.
        resource: The list to ask about, ``tags`` or ``correspondents``.

    Returns:
        A 200 with a fresh retry, firing the list's recovery event when it
        can be loaded, or a 204 for a hidden list.

    """
    svc = services(request)
    shown = (
        svc.settings.web.show_tags
        if resource == "tags"
        else svc.settings.web.show_correspondent
    )
    if not shown:
        return Response(status_code=204)
    loaded = (
        metadata_view.cached_list_or_none(
            svc.cache,
            svc.paperless,
            resource,
            timeout=metadata_view.REQUEST_FETCH_TIMEOUT,
        )
        is not None
    )
    return svc.templates.TemplateResponse(
        request,
        "partials/list_retry.html",
        {
            "retry_resource": resource,
            "retry_seconds": metadata_view.metadata_retry_seconds(
                svc.settings.output.paperless_cache_ttl_seconds
            ),
        },
        headers={"HX-Trigger": f"{resource}-recovered"} if loaded else None,
    )


@router.post("/api/cache/invalidate")
def invalidate_cache(
    request: Request,
    resource: metadata_view.MetadataResource,
    q: Annotated[str, Form(max_length=TAG_FILTER_MAX_LENGTH)] = "",
    tags: list[PaperlessId] = _TAGS_FORM_DEFAULT,
    correspondent: Annotated[PaperlessId | None, Form()] = None,
) -> Response:
    """
    Refetch one list and render it.

    Invalidating means "fetch again", not "forget": a failed refetch renders
    the last list fetched, or says the list could not be loaded.  It also
    clears the cache's memory of a failed fetch, so a press retries at once.
    The refresh carries the filter, the ticks and the choice, so it changes
    the list and never what the form shows.

    Each resource has its own floor of one refetch per
    ``MIN_MANUAL_REFRESH_SECONDS``, because the endpoint is unauthenticated
    and every refetch is a token-bearing request to paperless-ngx.  A
    too-soon call renders the cached list instead of erroring: it is early,
    not wrong.

    Args:
        request: The incoming HTTP request.
        resource: Resource name to invalidate ('tags' or 'correspondents').
        q: The tag filter currently in the box, if any.
        tags: The tag ids currently ticked, if any.
        correspondent: The correspondent currently chosen, if any, bounded
            the way the scan's is.

    """
    svc = services(request)
    if svc.invalidate_floors[resource].claim() is not None:
        svc.cache.invalidate(resource)

    if resource == "tags":
        return svc.templates.TemplateResponse(
            request,
            "partials/tags.html",
            metadata_view.tag_list_context(
                svc, q=q, selected=tags, timeout=metadata_view.REQUEST_FETCH_TIMEOUT
            ),
        )

    return svc.templates.TemplateResponse(
        request,
        "partials/correspondents.html",
        metadata_view.correspondent_options_context(
            svc, correspondent, timeout=metadata_view.REQUEST_FETCH_TIMEOUT
        ),
    )


@router.get("/api/jobs/history")
def job_history(request: Request) -> Response:
    """
    Fetch the job history table body.

    Returns the history partial with the most recent jobs for
    HTMX swap into the history table, each one as this browser may see it.
    """
    svc = services(request)
    return svc.templates.TemplateResponse(
        request,
        "partials/history.html",
        {"jobs": profile_view.history_views(request)},
    )


@router.post("/api/flip/continue")
def continue_flip(request: Request, job_id: Annotated[str, Form()]) -> Response:
    """
    Answer the named job's flip prompt with Continue, starting its pass B.

    The posted ``job_id`` scopes the answer.  A Continue for another job, one
    sent before or after the prompt, or one from a browser not holding the
    job's owner token is dropped, and the route still returns the current
    status rather than an error.  A job whose ``owner_token`` is NULL is
    unowned and anyone may answer it.

    A claimed answer is acknowledged in place of the buttons, so a repeated
    click never re-renders a prompt that looks unanswered.  The response
    re-renders the Scan button out-of-band, leaves ``#status-message`` alone,
    and moves focus to the status area, because the pressed button is gone.
    """
    svc = services(request)
    presented = owner.presented_owner(request)
    claimed = False
    if owner.owner_answers(presented, svc.job_store.get_job(job_id)):
        claimed = svc.worker.continue_flip(job_id)
    _, context = status_view.with_poll(
        request,
        status_view.status_context(
            svc.worker,
            svc.job_store,
            status_view.status_facts(
                request,
                followed_job_id=job_id,
                claimed=(job_id, FlipOutcome.CONTINUED) if claimed else None,
            ),
        ),
    )
    return svc.templates.TemplateResponse(
        request,
        "partials/status_response.html",
        {**context, "terminal_reload": True, "focus_status_area": True},
    )


@router.post("/api/flip/abort")
def abort_flip(request: Request, job_id: Annotated[str, Form()]) -> Response:
    """
    Answer the named job's flip prompt with Abort, failing it before pass B.

    The posted ``job_id`` scopes the answer, so a double-clicked Abort cannot
    land on the next queued job.  An Abort dropped as ``continue_flip`` drops
    a Continue still returns the current status rather than an error.

    A claimed Abort is acknowledged in place of the buttons and asks for focus
    on the Scan button.  Its own rendering usually still shows that button
    disabled, so the status area takes focus meanwhile and the poll URL
    carries ``focus=scan`` until a rendering enables it.
    """
    svc = services(request)
    presented = owner.presented_owner(request)
    claimed = False
    if owner.owner_answers(presented, svc.job_store.get_job(job_id)):
        claimed = svc.worker.abort_flip(job_id)
    _, context = status_view.with_poll(
        request,
        status_view.status_context(
            svc.worker,
            svc.job_store,
            status_view.status_facts(
                request,
                followed_job_id=job_id,
                claimed=(job_id, FlipOutcome.ABORTED) if claimed else None,
            ),
        ),
        focus_scan=claimed,
    )
    return svc.templates.TemplateResponse(
        request,
        "partials/status_response.html",
        {**context, "terminal_reload": True, "focus_status_area": True},
    )


@router.post("/api/multi-page/answer")
def answer_multi_page(
    request: Request,
    job_id: Annotated[str, Form()],
    prompt: Annotated[int, Form(ge=1)],
    answer: Annotated[PassAnswer, Form()],
) -> Response:
    """
    Answer the named job's open multi-page question.

    The answer is claimed only for that job, while that numbered question is
    open, and when the question offers ``answer``; anything else, including an
    answer from a browser not holding the job's owner token, is dropped and
    the route still returns the current status.  The clock's and the
    shutdown's answers pass validation but are never offered, so the worker
    drops them too.

    A claimed answer is acknowledged in place of the buttons.  Focus moves to
    the status area, or to the Scan button for a claimed Abort, as the flip
    prompt's Abort does.

    Args:
        request: The incoming request.
        job_id: The job the answer is for.
        prompt: The number of the question the answer is for.
        answer: What the operator chose.

    Returns:
        The status partial, as this browser may see it.

    """
    svc = services(request)
    presented = owner.presented_owner(request)
    claimed = False
    if owner.owner_answers(presented, svc.job_store.get_job(job_id)):
        claimed = svc.worker.answer_pass(job_id, prompt, answer)
    _, context = status_view.with_poll(
        request,
        status_view.status_context(
            svc.worker,
            svc.job_store,
            status_view.status_facts(
                request,
                followed_job_id=job_id,
                claimed_pass=(job_id, answer) if claimed else None,
            ),
        ),
        focus_scan=claimed and answer is PassAnswer.ABORT,
    )
    return svc.templates.TemplateResponse(
        request,
        "partials/status_response.html",
        {**context, "terminal_reload": True, "focus_status_area": True},
    )
