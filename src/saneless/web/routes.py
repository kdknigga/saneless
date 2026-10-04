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

# Every handler below is a plain ``def`` on purpose.  Each one calls blocking
# code -- sync httpx2 to Paperless, sqlite through the job store, the worker --
# and FastAPI runs ``def`` handlers on its threadpool, so a slow Paperless call
# cannot stall ``/health`` or the status poll.  The shared state
# they touch is locked: the JobStore's RLock, the worker's profile lock and the
# metadata cache's per-key locks.
router = APIRouter()

TAGS_MAX_COUNT: Final = 100
"""
The cap on how many tag ids one request may carry, applied at the boundary.

A hundred is far more than anyone ticks for one document, and it bounds
what a single row can hold: the ids are stored as JSON on the job row, so
an unbounded list would let one request, refused or not, write as much as
it liked.  ``Form(max_length=...)`` and ``Query(max_length=...)`` turn a
longer list into a 422 before the handler body runs, on the scan form, the
tag list and the tag refresh alike.  Each id is bounded too, by
``PaperlessId``, to what a paperless-ngx key can be.
"""

_TAGS_FORM_DEFAULT = Form(default=[], max_length=TAGS_MAX_COUNT)

# The same "no tags ticked" default, for the GET that renders the list.  htmx
# sends an ``hx-include``'s values in the query string of a GET and in the body
# of a POST, so the two methods need one of these each.  Both are module-level
# constants so the parameter defaults themselves stay call-free (B008).
_TAGS_QUERY_DEFAULT = Query(default=[], max_length=TAGS_MAX_COUNT)

TAG_FILTER_MAX_LENGTH: Final = 100
"""
The cap on the tag filter, applied at the boundary before any work.

A hundred characters is far more than a tag name and far less than a payload:
the value is a substring test against names the operator chose, so anything
longer cannot be a filter and is either a mistake or an attempt to make the
server do work for nothing.  ``Query(max_length=...)`` turns it into a 422
before the handler body runs, which is the same "validate at the boundary"
shape ``metadata_view.MetadataResource`` uses for ``resource``.
"""

LISTS_LOADING_MARKER: Final = "(lists loading)"
"""
What the page's profile markers say while its lists are still loading.

The full page renders the tag list and the correspondent select empty, and
the lazy list load brings the rows, the ticks and the choice.  Scan is held
until then, but a status poll renders the Scan button from the job alone, so a
job ending during the load releases it early.  A marker naming the opening
profile would then present the empty controls as that profile's answer, and
the scan would be filed with no tags and no correspondent.  Marked with this
value instead, neither control answers for the submitted profile, so
``start_scan`` applies that profile's defaults by the rule it already has for
a control still showing another profile's: exactly what the untouched form
files once the lists have landed.  The lazy load's first answer replaces both
markers with the profile it ticked the defaults of.

It is not empty, because an empty field arrives as no marker at all, which is
how a script says its values are to be taken as given.  It holds a space and
brackets, which a bare TOML key cannot hold and ``auto-profiles`` never
writes, so no generated profile and no profile named without quotes is called
this.  A quoted TOML key can spell any string, so the guarantee stops there:
a profile named exactly this in quotes would have its page-marked submits
taken as given, as before.
"""

CHECK_AGAIN_WAIT_SECONDS: Final = 2.5
"""
The most seconds Check again waits for the probe it handed to the refresher.

A probe that finishes inside it is rendered in the same response, which is
the usual case on a healthy appliance.  A slower one returns the strip with
its settling poll, which collects the answer when the probe stores it.  The
wait is far inside the ten seconds an idle server is given to stop, so a
request waiting here never holds a shutdown open.  Only a click the
manual-refresh floor admits waits at all, so a loop of clicks holds at most
one waiting worker thread per floor interval.  Read at call time, not bound
where it is used.
"""


@router.get("/")
def index(request: Request) -> Response:
    """
    Render the main page with scan form, status, and job history.

    Populates profile selector from the worker's profile set (read under its
    profile lock) and loads recent job history from the database.  The page
    opens on ``default``, or on the profile standing in for a hidden
    ``default``.

    The page makes no paperless-ngx call, so a slow or unreachable
    paperless-ngx never holds it up.  Where the tag list and the
    correspondent select go it says each is loading, and a hidden loader
    asks ``/api/metadata`` for both once the page has rendered, naming the
    profile the select shows.  That answer ticks the profile's default tags
    and selects its correspondent, so an untouched submit still scans with
    exactly what ``saneless scan`` with no ``--profile`` would.  Until it
    lands, Scan is held, with a line that says why: the lists it would file
    the scan with have not arrived.  An answer that says a list could not be
    loaded releases Scan just as one that brings the list does.
    """
    svc = services(request)
    # The refresher only probes while a page says someone is looking, so
    # every route that renders the strip has to stamp this.  Without it the
    # lazy thread returns at its first guard for ever and the strip never
    # leaves its cold-start rows.
    svc.refresher.note_watcher()
    choices = profile_view.profile_options(svc.worker)
    # The option the page opens on, found by the name the choices chose, so the
    # select, the sentence beneath it and the Multiple pages field all read the
    # same profile.  None only when there are no options at all.
    opening_option = next(
        (option for option in choices.options if option.name == choices.opening),
        None,
    )
    # Which lists the form shows, and so which ones the page waits for.  The
    # lists themselves are never fetched here: the page carries the emptied
    # contexts, the key sets the templates read, and the lazy list load
    # brings the rows.
    show_tags = svc.settings.web.show_tags
    show_correspondent = svc.settings.web.show_correspondent
    lists_loading = show_tags or show_correspondent

    # The page follows the newest active job this browser owns, so a reload
    # while someone else's scan runs still reports the scan this person
    # queued; with none of its own active, it reports the current job.  Its
    # poll URL carries the token of what the first poll would render, so that
    # poll is answered 204 and the page's own rendering stays in place.
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
    # A job that is no longer active is reported as the last scan, not as
    # news.  The poll token above is still the live rendering's, which no poll
    # asks for: an area with no active job does not poll.
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
            # Which option the page opens on, and the sentence that goes with
            # it: the profile ``saneless scan`` uses with no ``--profile``, or
            # the profile standing in for it, wherever it sits in the list.  A
            # browser selects the first option when none is marked, and the
            # first option is seldom ``default``, so the option is named here
            # and the highlighted option and the description beneath it cannot
            # disagree on first paint.
            "selected": choices.opening,
            "selected_description": (
                opening_option.description if opening_option is not None else ""
            ),
            # The Multiple pages field for the profile the page opens on, and
            # never ticked: the choice is made per scan, so a page load starts
            # it afresh.
            **profile_view.multi_page_field(
                manual_duplex=opening_option is not None
                and opening_option.manual_duplex,
                ticked=False,
            ),
            **metadata_view.no_tag_list(),
            **metadata_view.no_correspondent_options(shown=show_correspondent),
            # The loading lines where the lists go, one per shown list.
            "tags_loading": show_tags,
            # What both profile markers say until the lazy list load replaces
            # them: the lists have not answered for any profile yet.
            "lists_loading_marker": LISTS_LOADING_MARKER,
            "correspondents_loading": show_correspondent,
            # The third source of a disabled Scan button, beside an active job
            # and a blocked appliance: a shown list has not arrived yet.  The
            # lazy list load re-renders the button without it, and empties the
            # hold line, whether the lists arrived or could not be loaded.
            "lists_loading": lists_loading,
            # Why Scan is held, naming only the lists the page shows.  A blocked
            # appliance has its own reason, which still holds after the lists
            # arrive, so the page renders that one alone.
            "scan_hold_reason": (
                None
                if block is not None
                else scan_hold_reason(tags=show_tags, correspondents=show_correspondent)
            ),
            **status,
            **last_scan,
            **strip_view.checks_context(svc),
            "jobs": jobs,
            # The title input's maxlength; templates own no vocabulary.
            "title_max_length": TITLE_MAX_LENGTH,
            # What a browser with JavaScript off reads under the Scan heading:
            # Scan cannot work there, and the server refuses its post.
            "no_script_line": NO_SCRIPT_LINE,
            # The tag filter's visually hidden label, and its maxlength: the
            # same bound the filter routes refuse a longer filter with.
            "tag_filter_label": TAG_FILTER_LABEL,
            "tag_filter_max_length": TAG_FILTER_MAX_LENGTH,
            # The reason line's copy, which only the full page renders: it is
            # never an out-of-band swap target, so no status response needs it
            # and it never has to exist as an empty placeholder.
            # The template reads the string and decides nothing; the flag that
            # says whether to render it comes from the status context.
            "scan_blocked_reason": block.reason if block is not None else "",
            # Which optional controls this appliance's form carries.  A
            # configured key and not a per-browser toggle: one appliance, one
            # form shape, and the template renders the controls or leaves them
            # out of the markup entirely rather than hiding them with CSS.
            # Turning one off changes the form and never the scan: a shown
            # control opens on the profile's defaults, and ``start_scan``
            # applies the same defaults for exactly the control that is not
            # on the page.
            "show_tags": show_tags,
            "show_correspondent": show_correspondent,
            # A page load moves no focus, even while a prompt is open: the
            # prompts leave their autofocus off when this is set.  No status
            # response sets it and the poll token never sees it, so the page's
            # token still names the poll's rendering, autofocus included, and
            # the first poll after the page is a 204.
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
    server error rather than a bad gateway: an answer paperless-ngx gave is
    reported as a 200 status instead.  Class name only, by the
    client-exception rule above ``metadata_view.cached_list_or_none``.

    Args:
        exc: What stopped the test from producing a status.

    Returns:
        The 500 answer naming the exception class.

    """
    detail = type(exc).__name__
    logger.warning("Paperless connection test failed: %s", detail)
    return PaperlessTestAnswer(
        status_code=500, body={"status": "error", "detail": detail}
    )


@router.get("/api/paperless/test")
def paperless_test(request: Request) -> JSONResponse:
    """
    Test paperless-ngx connection status.

    Returns 200 with one ``ConnectionStatus`` value as its status:
    connected, token_rejected, not_found, server_error, unreachable,
    incompatible_version, redirected or misconfigured.  A redirect's target
    is never in the body: it is upstream text, and this endpoint needs no
    login.  An unexpected failure inside saneless is a 500
    whose status is error, with the exception's class name as its detail.

    The answer is shared and reused for ``MIN_MANUAL_REFRESH_SECONDS``, error
    included, so a loop against this unauthenticated endpoint costs one
    token-bearing request to paperless-ngx per window rather than one per
    call.  Concurrent callers do not each probe either, and while a probe is
    in flight a caller with a previous answer gets that answer at once rather
    than holding a worker thread behind it.

    The probe runs on the status strip's budget, ``metadata_view.REQUEST_FETCH_TIMEOUT``
    (2 s to connect, 5 s to read), not the client's own 30 s default, so
    against an unreachable paperless-ngx it ends within seconds.  Only the
    very first callers, before any answer exists, wait for the probe, two at
    most at a time, and for at most ``PAPERLESS_TEST_WAIT_SECONDS``: longer
    than the probe budget, so a follower shares the answer, and shorter than
    the time an idle server is given to stop, so no request waiting here
    holds a shutdown open.  A wait that outlasts its bound, and a caller
    refused a place to wait, answers 503 naming ``TimeoutError``, with a
    ``Retry-After`` header saying when to ask again.  That answer is not
    shared: the caller never got a turn, so there is no result to reuse.
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

    Grouped into one dependency because ``start_scan`` already takes five
    parameters, and one more would take it past ``PLR0913``'s ceiling.  A
    suppression is not allowed, and ``status_view.StatusFacts`` settled the answer to the
    same limit the same way.  Both fields are what decides how the job runs,
    which is why they, and not the title or the tags, travel together.

    The two markers ride here for the same reason: they say whose defaults
    the tag and correspondent controls were showing, which is a fact about
    the profile, not about the metadata itself.

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

        A marker naming another profile means the control was still showing
        that profile's defaults: a profile change whose swap had not landed,
        or had failed.  The page's own markers say ``LISTS_LOADING_MARKER``
        until its lists land, which names no profile, so a submit sent before
        then is read the same way.  No marker means the submit did not come
        from the page -- a script posting its own values -- and those are
        taken as given.

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

    Keyword-only so the boolean is never a positional flag.  An unticked
    checkbox sends nothing at all, so its absence is False.

    Args:
        profile: The scan profile name.
        multi_page: Whether Multiple pages was ticked.
        tags_profile: The tag list's profile marker, if the page sent one.
        correspondent_profile: The correspondent select's profile marker, if
            the page sent one.

    Returns:
        The four, together.

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

    This departs from the older rule of creating a row only after enqueue,
    because a refused attempt should be visible in history.  The row is written
    already ``ERROR`` with ``ErrorCategory.REJECTED``, the marker that keeps it
    out of the status area.

    ``create_rejected_job`` is a single ``INSERT``.  Create-then-finish would be
    two transactions, and a failure between them would leave a PENDING row with
    no marker that nothing ever reconciles, disabling the Scan button for good.
    With one statement a failure leaves no row at all.

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
    Mark a job row the worker then refused as a REJECTED error.

    The row had to exist before ``put_nowait``, or the worker could dequeue an
    id with no row, so this refusal is recorded by finishing that row.  The
    ``ErrorCategory.REJECTED`` marker is what keeps it out of the status area.

    The row already exists, so a failed write cannot simply be dropped: the
    row would stay PENDING with no marker, disabling the Scan button until a
    restart.  It is owed to the worker instead, which records it on its next
    idle tick.

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


# The title validator's error message.  It goes with ``TITLE_CONTROL_TYPE``,
# which the validation error handler maps to TITLE_HAS_CONTROL.  The handler
# never reads this message and nothing renders it; the page shows the
# rejection's fixed sentence instead.
_TITLE_CONTROL_MESSAGE: Final = "the title contains a control character"


def _refuse_control_characters(value: str) -> str:
    """
    Refuse a title that holds any control character, tab included.

    The title is refused, never repaired: silently stripping characters would
    store a title the person did not type.  A browser's text input already
    drops newlines, so what reaches here is a tab, a pasted escape sequence or
    a request that did not come from the page, and a stored control character
    could later rewrite a terminal or split a log line.  The error carries a
    fixed message and never the value; the error handler reads only its
    location and type.

    Args:
        value: The submitted title, already within ``TITLE_MAX_LENGTH``.

    Returns:
        The title, unchanged.

    Raises:
        PydanticCustomError: The title holds a control character.

    """
    if has_control_characters(value):
        raise PydanticCustomError(TITLE_CONTROL_TYPE, _TITLE_CONTROL_MESSAGE)
    return value


def _queued_status_fallback(job_id: str) -> str:
    """
    Build the status area for a queued job without rendering a template.

    This is what a scan submit answers when rendering its normal response
    failed after the worker had already accepted the job.  It is fixed markup:
    the status area with the same poll attributes ``partials/status.html``
    gives a followed active job, holding the existing starting prose and
    taking focus as every accepted submit's status area does, plus the
    out-of-band clear of ``#status-message`` that every accepted submit sends.
    The first poll then renders the full status, the Scan button included.
    No exception text reaches it; that goes to the server log only.

    Args:
        job_id: The accepted job, which the status area follows.

    Returns:
        The response body.

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

    The page submits the form through htmx, which sends ``HX-Request: true``.
    A browser with JavaScript off, or one whose htmx failed to load, posts the
    form itself as a navigation and shows the answer as the page, so it is
    answered with a page saying why nothing happened, and nothing is started.

    A navigation is told apart by two headers together: no ``HX-Request``, and
    an ``Accept`` naming ``text/html``, which a browser sends for the page it
    will show next.  Refusing every post without ``HX-Request`` would break
    the documented API: curl and scripts post here directly, and they send
    ``*/*`` or no ``Accept`` at all.  ``Sec-Fetch-Mode`` would name a
    navigation outright, but a browser sends it only to a secure origin, and
    this appliance is usually reached over plain http on the LAN.

    This runs as a route dependency, ahead of every parameter, so it answers
    before body validation does and before ``scan_block.block_for`` writes a refused
    attempt's row: a refused navigation started nothing and records nothing.

    Args:
        request: The incoming request.

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
    Start a new scan job from form submission.

    A browser that posted the form by itself, with JavaScript off, is refused
    before anything else, with a page that says why and starts nothing
    (``_refuse_browser_navigation``).

    Input is validated before any job row exists: a title over
    ``TITLE_MAX_LENGTH``, a title holding a tab or any other control
    character, more than ``TAGS_MAX_COUNT`` tags, or a profile that is not
    configured (checked under the worker's profile lock) is a 422 and writes
    nothing.  So is Multiple pages on a manual-duplex profile: the form
    disables the checkbox for one, and this is the refusal that holds for a
    submit that did not come through that form.

    An accepted job is handed to the worker with its ``ScanOptions``, which is
    how the Multiple pages choice reaches the scan without being stored on the
    job row.

    A valid submit creates the job row and offers it to the worker, returning
    the status partial once the job is queued.  That response re-renders the
    Scan button out-of-band from server state and clears the
    ``#status-message`` slot out-of-band, so an error left there by an earlier
    rejected submit disappears.  It also moves focus to the status area,
    where the scan just started is reported.  A refused submit records a
    REJECTED error row and raises: 429 with ``Retry-After`` when the queue is
    full, 503 when the worker is down or degraded, and
    503 with its own message when the paperless-ngx API token is a placeholder
    nobody replaced, which is the one failure certain to waste paper because
    the pages would be scanned and then have nowhere to go.
    The rendered error reloads Job History only when that row was written.

    The worker accepting the job is the commit point: from then on the scan
    runs whatever this response says, so nothing after it may fail.  Anything
    that can fail and is not about the job -- the status strip's context -- is
    built before the job row exists, so its failure writes nothing.  If the
    status response itself cannot be rendered after the commit point, the
    failure is logged and a minimal status area answers instead, still 200 and
    still following the job: an error telling the operator to try again would
    be false, and trying again would queue a second scan behind the one
    already running.  The owner cookie is set on
    whichever of the two responses goes out, so this browser can still answer
    the job's flip prompt.

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
    # Refused like an unknown profile, ahead of every refusal that writes a
    # row: this is a request the form would not have made, not an attempt to
    # scan that Job History should show.
    if choice.multi_page and profile_view.is_manual_duplex(found):
        raise RequestRejected(RequestRejection.MULTI_PAGE_MANUAL_DUPLEX)
    title = resolve_job_title(title, found, now=datetime.now(tz=UTC))
    # The form shows the profile's default tags and correspondent already
    # chosen, so a submit from a shown control is the operator's whole answer:
    # the defaults when nobody touched it, none when every box was unticked.
    # A control ``[web] show_tags`` or ``show_correspondent`` hides was never
    # answered, and the profile's default applies, exactly as a blank title
    # falls back to the profile's title on the line above.  One policy decides
    # both, the same one ``saneless scan`` uses, so the same profile files the
    # same metadata from either surface.
    #
    # The gate is the config key and never the submitted value, because the
    # value cannot carry the distinction: an empty tag list and an absent
    # correspondent arrive identically whether the control was never rendered
    # or was rendered and cleared.  Only the setting that decided which page
    # was served knows which happened, and with the control hidden the
    # submitted value for that field is ignored rather than merged.
    #
    # A shown control whose profile marker names another profile was not
    # answering for this one either: its swap to the submitted profile's
    # defaults had not landed, or had failed, so it still held the previous
    # profile's.  Taking those values would file this scan with the other
    # profile's metadata, so the submitted profile's defaults apply instead.
    # The page's markers say the lists have not answered at all until its lazy
    # list load lands, which names no profile either, so a Scan a status poll
    # released early files the defaults, not the empty controls.
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

    # The route guard is the enforcement and the disabled Scan button is
    # only a courtesy, so this refusal holds for curl, for a script, and for a
    # browser whose ``disabled`` attribute was removed in devtools.  It is
    # unconditional -- a configured consume directory does not buy an exception
    # -- and it sits ahead of ``create_job`` so a scan that could never upload
    # leaves exactly one row: the REJECTED one, which Job History shows so the
    # attempt is visible rather than silently swallowed.
    # ``RequestRejection.WORKER_DEGRADED`` is deliberately not reused here: "the
    # scan service was unavailable" is untrue when the service is fine and
    # nobody set the token, and it would send a household member looking for a
    # broken server.  The
    # status is 503, matching the two existing refuse-to-start rejections, so
    # htmx response handling and the history reload behave identically; a 4xx
    # would imply the request was at fault, which it was not.
    #
    # An empty ``paperless.url`` is refused the same way: the upload would
    # fail for certain as a configuration error, and no consume-folder copy is
    # made for it, so the stack would be fed for a PDF that ends in failed/.
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
    # Ahead of the row, not merely ahead of the submit: a failure between
    # the two would leave a PENDING row that no worker will ever run.  Built
    # as a scan in progress, because it is rendered only for an accepted
    # submit, and by then the scanner is this job's or a queued-ahead one's:
    # read from the worker here, before the job exists, it would say idle.
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
            # A job created by this request cannot have a flip answer yet.
            # Only a successful scan clears the status-message slot, and only a
            # successful scan carries the strip out-of-band: the scanner has
            # just become busy, so the strip's paused note is due now rather than
            # at the end of the cache's TTL.  The strip's own context rides
            # along because the partial is rendered inside this response.
            #
            # The job is queued from here on, so a failure below must not
            # escape into the error handler: Starlette renders the template
            # inside the TemplateResponse constructor, and the generic error it
            # would answer tells the operator to try again.
            try:
                # The created job is the followed job, so the poll URL this
                # browser is handed names it and the status area keeps
                # reporting the scan this person started.  The token is built
                # inside the guard too: it renders a template, and its
                # failure must answer the fallback like any other.
                _, status = status_view.with_poll(
                    request,
                    status_view.status_context(
                        svc.worker,
                        svc.job_store,
                        replace(
                            status_view.status_facts(request, followed_job_id=job.id),
                            # The token this submit is owned by, which is the
                            # minted one when the browser presented none: the
                            # cookie carrying it has not reached the browser
                            # yet, so the request cannot present it.
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
    Poll the current or most recent job status.

    Returns the status partial template for HTMX polling swap.  While an
    answered job is still recorded ``AWAITING_FLIP``, the partial shows the
    acknowledgment rather than the flip buttons.

    The response re-renders the Scan button out-of-band from server state.
    It never clears ``#status-message``: a poll carrying that clear would erase
    a rejection shown mid-scan within a second.

    A poll whose ``seen`` is the token of what it would render now is answered
    204 with no body, which htmx leaves unswapped: the focused button, a
    pressed key and the live region's text all survive it.  The element keeps
    its URL, because htmx does not re-process an element it did not swap, so
    every later poll presents the same token until something changes.

    A poll that cannot build or render its status never reaches
    ``render_error``, for the reason ``get_checks`` gives: an error response is
    the one thing a polling element cannot usefully receive.  Retargeted into
    ``#status-message`` it would re-write the page's one alert on every tick
    and leave it standing above "Done" once the store healed.  The failure is
    logged and answered with status_view's lost-contact fallback instead: a
    fixed line inside the status area that polls again on the
    ``status_view.STATUS_BACKOFF_SECONDS`` schedule and never stops.  The status area is deliberately not added to
    ``render_error``'s own-target exemption, as the strip is: swapping
    ``error.html`` over ``#status-area`` would delete the element the scan
    form targets.

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

    Declared after ``/api/jobs/current/status`` so that literal path keeps
    winning: a browser that submitted nothing still gets today's route and
    today's current-or-most-recent inference.

    The path parameter is an opaque store lookup key and nothing else.  It
    never builds a filesystem path, a URL or a template name, so there is no
    traversal surface to guard, and an id naming no row is a
    fallback rather than a 404 by design: a browser whose job has been pruned
    degrades to the inferred rendering, and a 404 would additionally confirm to
    a caller which ids exist.

    Everything else matches ``current_job_status``: the Scan button rides along
    out-of-band, ``#status-message`` is left alone, and a poll presenting the
    token of what it would render now is answered 204 with no body.  A failure
    to build or render the status is answered with the same backing-off
    fallback, never through ``render_error``.  The fallback cannot confirm the
    id against the store, so it echoes it only after it parses as a UUID, and
    otherwise polls the current route.

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

    This is the target of the cold-start poll, and it is a cache read: it never
    probes, so no number of open browser tabs can raise the probe rate above
    the cache's TTL.  The poll ends itself -- the body this
    returns once results exist carries no ``hx-trigger``, so the swap that
    installs it is the last one.

    ``attempt`` is how the poll ends when results never arrive.  Each
    body names the number the next request should carry, so the count lives in
    the URL rather than on the server: the strip is one shared cache read and
    there is nothing per-tab to keep, and a browser that goes away takes its
    count with it.

    This route no longer hands its own ordinary failures to ``render_error``.
    An error response is the one thing the polling element cannot
    usefully receive, so both of the failures this handler can see end as a
    trigger-free strip at 200 instead.  An out-of-range counter is clamped
    rather than refused: the ``Query`` bound that used to make it a 422 is
    gone, and the clamp into ``0..POLL_PROBE_ATTEMPT_CAP`` runs before anything
    else reads the value.  The property that bound was defending is unchanged
    -- a crafted number still never reaches ``strip_view.checks_context`` and is still
    never rendered into the visible body, because only the clamped number is
    used and only the clamped number goes into the next request's URL.  What
    changed is the failure mode: past the cap the clamp lands on the cap, which
    is the give-up body, so an out-of-range counter ends the chain quietly,
    which is what the strip wanted all along.  A failure inside the watcher
    stamp or the context build is caught and rendered as
    ``strip_view.checks_fallback_context``.  The ``TemplateResponse`` call stays outside
    the guard, so there is exactly one render path and one status code.

    Args:
        request: The incoming request.
        attempt: Which attempt this is, counted from the zero a page render
            starts at.  Clamped into ``0..POLL_PROBE_ATTEMPT_CAP`` here, so a
            value below zero is read as the start of a fresh chain and a value
            above the larger cap is read as that cap, where the response
            carries no request attribute at all and the strip stops asking.
            The bound is the *larger* of the two caps on purpose:
            clamping at ``POLL_ATTEMPT_CAP`` would cut a legitimate chain that
            is waiting on a live probe down to an ending it never reached.

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
    Re-probe every check now, bypassing the TTL, and render the result.

    The bypass is the whole point of the button.  Routing the click through the
    refresher's *policy* would do nothing for the first thirty seconds after a
    page load -- ``_tick`` returns early on a fresh cache -- which is precisely
    when somebody who has just plugged the scanner back in presses it.  So the
    click asks the refresher for a probe with ``request_probe``, and the
    refresher's own thread runs it without the two guards.

    This thread never probes.  It signals and then waits at most
    ``CHECK_AGAIN_WAIT_SECONDS`` for the answer: a probe that finishes inside
    that is rendered here, and a slower one leaves ``probe_in_flight`` true, so
    the strip rendered below carries the settling poll that collects it.  A
    request thread still inside a probe would hold the server open past its
    shutdown budget, and would be a second caller into a scanner library that
    is not reentrant.

    Concurrent clicks collapse.  The refresher admits one probe at a time and
    a click landing while a probe is in flight re-renders the current strip
    rather than asking for another, at once and without waiting: the answer is
    seconds away, and a second probe would cost a Paperless request and two
    filesystem writes to produce it twice.

    The collapse used to cost the clicker three things.
    The strip rendered was the cache *as it stood* -- the pre-probe entry,
    because the in-flight probe had not stored yet.  Because results already
    existed the body carried no trigger, so nothing on the page was ever going
    to pick up the result that probe landed a second later; the button could
    visibly do nothing.  And the claim was stamped before the collapse was
    discovered, so the next click inside two seconds was refused too: nothing,
    twice in a row.  All three close here.  ``request_probe`` reports a
    collapse, so a collapse is a fact rather than a guess; on a collapse the
    claim goes back, because a collapse issued no Paperless request, no saned
    dial and no filesystem write and so bought none of the traffic the floor
    exists to bound; and ``strip_view.checks_context`` sees the same lock still held and
    emits a trigger, so the page collects the answer on its own.

    The grant given back is identified rather than assumed.  The claim reports
    the stamp it wrote, this handler holds that stamp for the length of the
    request, and ``release_manual_claim`` compares before it clears -- so a
    release can only ever undo this request's own grant and never one a later
    caller made.  The grant is tested with ``is not None`` and not
    for truth: the stamp is a ``time.monotonic()`` reading, which counts from
    boot, so the first click on a freshly booted appliance can hold a perfectly
    valid grant of 0.0.

    There is one render path and it is the last statement.  The context decides
    the trigger from ``probe_in_flight``, which is still ``True`` on the
    collapse branch -- the other checker has not released yet -- and on a
    probe still pending at the end of the wait, so the one render covers every
    outcome, and there is no second response and no "poll once" flag for a
    later reader to keep in step with the template.  A click the probe
    answered in time and a pending one both keep their claim: a probe ran, or
    is running, for each.

    That render is guarded the way ``get_checks``'s is, and only the render.
    ``strip_view.checks_context`` raising here used to be a 500, and because the button
    aims at ``#checks-body`` that 500 arrived carrying the strip's own
    ``HX-Target`` -- which, until the exemption was narrowed to a GET,
    meant the error body was written over the strip, taking every row and
    the only button that could bring them back.  Now a failure inside the
    strip's rendering ends as ``strip_view.checks_fallback_context`` at 200 on
    this route too, so the strip and the button stay on the page.  Everything before the
    render -- the watcher stamp, the claim, the probe request -- is the click's
    action rather than the strip's drawing, and stays outside the guard on
    purpose: a request that raises is a failed click, and a failed click is
    reported like
    every other one, as an error in the message slot with the strip left as it
    was.

    During a scan it re-runs only the checks that do not touch the scanner, and
    that decision comes from the worker's own record of a job in flight rather
    than from a failed attempt on a lock.  An explicit click does not get to
    defeat the exclusive-scanner rule, because nothing in the SANE backend
    mutually excludes two callers and a status probe landing mid-scan is a
    second caller into the same C library.  Deriving it from the job state is
    also what stops two checkers contending from being rendered to a household
    member as a running scan.

    ``CrossOriginGuard`` is app-wide middleware on every non-safe method
    (``web/app.py``), so this POST inherits the cross-site check and must
    **not** add a per-route dependency: a dependency is something a route added
    later can forget, and the middleware is not.

    A registry that raised is logged by the probe and stores nothing, leaving
    the previous entry in place -- and a strip that blanked would be worse than
    one still showing what was true a moment ago.

    The bypass has a floor under it.  This is the one handler that can ask for
    a probe and it is unauthenticated by design on a LAN -- ``CrossOriginGuard``
    allows a request carrying neither ``Sec-Fetch-Site`` nor ``Origin``, which
    is what a non-browser client sends -- so without a minimum interval a loop
    turns one click into unbounded Paperless requests, saned dials and
    filesystem writes.  Single flight does not cover it: that collapses
    *concurrent* callers, and a serial loop is not concurrent.  The cost is
    not only traffic; ``ScanWorker._scan_job`` blocks on a scanner gate that is
    not a fair lock, so an unbounded loop can park a submitted job whose row
    already reads ``SCANNING``.  A too-soon click re-renders the
    current strip instead of erroring, because the click is not wrong, only
    early: there is nothing to tell the person at the appliance, and the
    response is the same partial from the same cache read either way.
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

    Uses cached data when available, falling back to a fresh fetch from
    paperless-ngx within the short request budget; an API error renders the
    last list fetched successfully, or, if there has never been one, a line
    saying the tags could not be loaded, with any ticked ids still ticked,
    rather than an error.  It never claims paperless-ngx has no tags when it
    could not ask.

    The response is the whole swap target, wrapper included, because the filter
    swaps it ``outerHTML``.  Both parameters arrive from the same
    ``hx-include`` and are what makes the swap lossless: see
    ``metadata_view.tag_list_context`` for why the selection has to ride along, and why
    ``q`` reaches neither paperless-ngx nor the response body.

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

    A select whose chosen option has an empty value, "No correspondent",
    still sends its name: htmx includes it in a GET as ``correspondent=``.
    FastAPI turns an empty value into the default for a form field but not
    for a query parameter, so without this the empty string would fail the
    integer check and refuse a request the page itself sends.

    Args:
        value: The raw query value.

    Returns:
        None for an empty string, otherwise ``value`` unchanged.

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
    Fetch correspondent options for the dropdown selector.

    Uses cached data when available, falling back to a fresh fetch from
    paperless-ngx within the short request budget.  On an API error it returns
    the last list fetched successfully, or, if there has never been one, only
    the option that means none.  Either way the help line under the select
    rides along out of band, saying the list could not be loaded when there
    was no list to show.  With the control hidden nothing is fetched.

    The choice the select holds can ride along and is rendered selected
    again, as the refresh does, so filling in a list that could not be
    loaded never changes what is chosen.

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

    The text is read from the loaded ``ProfileConfig`` rather than re-derived
    from the source name, so an operator who took a profile over -- by removing
    ``auto_generated`` and writing their own wording -- sees their sentence and
    not the generated one.

    ``profile`` is a query parameter and not a path segment, precisely so there
    is no traversal surface to reason about: the name never builds a path, a
    URL or a template name.  It is validated against the known profile set
    before any work, by one locked lookup that both checks the name and yields
    the profile, leaving no check-then-read gap -- the same shape ``start_scan``
    uses.  An unknown name is a 422.

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

    The select's change carries the profile and, when it is ticked, the
    checkbox.  A tick survives a change to a profile that allows it, so a
    profile switch never silently drops a choice; a manual-duplex profile
    renders the box disabled and unticked, with the reason in place of the help
    line.

    The profile is validated exactly as ``get_profile_description`` validates
    it: one locked lookup, and an unknown name is a 422.  The field is its own
    swap target, rather than an out-of-band swap riding the description
    response, because that response is the description text and nothing else.

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

    The profile select's change carries the profile alone, and the list comes
    back ticked with that profile's defaults and nothing else.  Replacing the
    earlier ticks is deliberate, and the opposite of the Multiple pages field,
    which keeps its tick: a profile's defaults are what an untouched form has
    to mean, so after a change the form must show the new profile's answer
    rather than a mix of two.  A default paperless-ngx no longer has is
    ticked with its note (see ``metadata_view.tag_list_context``).

    The profile is validated exactly as ``get_multi_page_field`` validates
    it: one locked lookup, and an unknown name is a 422 before any fetch.
    The list is its own swap target, wrapper and all, because the swap is
    outerHTML.

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

    The correspondent twin of ``get_profile_tags``, and it replaces the
    earlier choice for the same reason: the new profile's default, or none
    when it has none, is what the untouched form now means.  The select is
    its own swap target, so the response is the whole element.

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

    The page renders at once with a loader where the lists go, and the loader
    asks here after load.  One combined request, not one per list, because
    only the server knows when both are done: Scan is held until they are,
    and a page with no script of its own cannot count two responses.  So this
    fetches both, within the short request budget, and answers with every
    part that depends on them out of band (see
    ``partials/metadata_response.html``).  The loader itself is removed: a
    list that could not be loaded carries its own retry, which asks
    ``probe_metadata`` and changes nothing on the page but the retry.

    The request names the profile the page's select shows now, and the
    answer shows that profile's default ticks and correspondent with both
    profile markers out of band.  A person who changes Profile before this
    answer lands starts a profile-change swap of each control, and the loader
    asks here again for the new profile, abandoning the request in flight, so
    an answer for the profile chosen before never lands over the new one.
    The markers are sent again all the same, so whichever of this answer and
    a profile-change swap lands last, its list and its marker agree, and the
    next submit never files one profile's scan with another profile's
    defaults.

    The Scan button comes from the same status context the page builds, for
    the same job this browser follows, so an active job and a blocked
    appliance still disable it; a list that could not be loaded releases it
    just as a loaded one does.  A status poll can release it early too,
    because a poll knows nothing of the lists: that window is accepted, since
    it is bounded by the short budget and the profile-marker rule still
    protects what the scan files.

    The loader polls until it is answered, so nothing it sends is refused:
    an error would be written into the alert slot on every tick and hold Scan
    for good.  A request whose profile is missing, or was rewritten away
    since the page rendered, answers the lists with no ticks, no choice and
    no markers, so the markers keep saying the lists have not answered and a
    submit gets the submitted profile's own defaults.  A job store that
    cannot be read costs the lists nothing either (see
    ``metadata_view.metadata_scan_state``).

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

    A list rendered unavailable carries a hidden retry that asks here, for
    as long as that rendering is on the page.  The answer is a 200 whose
    body is a fresh copy of the retry, which replaces the one that asked and
    nothing else, so the retry asks again ``metadata_view.metadata_retry_seconds`` after
    this answer lands (see ``partials/list_retry.html``).  When the list can
    be loaded, the answer also fires ``<resource>-recovered``, and the
    page's own recovery element for that list asks for it again, carrying
    what the form shows at that moment: the ticks and filter, or the
    choice.

    The retry carries nothing of the form, and its answer renders only the
    retry, so it can never put back a tick or a choice changed while it was
    in flight, nor a profile's defaults changed away from.  The list it
    brings back is asked for only after it lands, and that request is
    synced with the list's own profile-change request, which always wins: a
    recovery asked while the profile change is in flight is dropped, and
    one in flight when the profile change starts is abandoned (see
    ``index.html``).  The fresh copy is sent on recovery too, so a recovery
    request that is dropped or fails is asked again; the recovered list
    replaces it.

    The recovery request does not keep a tick or a choice made while it is
    itself in flight.  It normally reads the list this fetched from the
    cache and lands at once.  With the cache disabled
    (``paperless_cache_ttl_seconds = 0``) nothing is read from the cache, so
    it fetches the list again, and can be in flight as long as any list
    fetch.

    The fetch goes through the cache, as the lists' own do, so a list the
    cache still remembers failing is answered at once.  The cache remembers
    a failure from when the failed fetch ended, so a retry timed from this
    answer is past the memory its own fetch left, and asks paperless-ngx
    within the short budget unless another request asked meanwhile.  A
    hidden list is never fetched, and no page renders a retry for it, so
    its answer is a 204, which htmx swaps nowhere.  Nothing here is
    refused: the retry polls, and an error would be written into the alert
    slot on every tick.

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
    Invalidate a specific cache entry and return fresh data.

    After clearing the cached entry, fetches and returns the updated
    partial for the specified resource.  Any other resource name is a 422
    before the cache is touched.

    Invalidating means "fetch again", not "forget": when the refetch fails,
    the partial is rendered from the last list fetched successfully and the
    failure is logged.  If there has never been one, the partial says the list
    could not be loaded: in the tag list itself, and for correspondents in the
    help line under the select, out of band.  The invalidate also clears the
    cache's short memory of a failed fetch, so a press retries at once.  The
    response does not say a served list is stale; the checks strip is what
    reports Paperless unreachable.

    The tag refresh renders the same partial the filter does, from the same
    context, so a refresh mid-filter comes back filtered and still ticked.  The
    correspondent refresh carries the select's current value the same way and
    renders it selected again, so a refresh changes the list and never the
    choice.  The extra values arrive in the body rather than the query string
    only because htmx sends an ``hx-include``'s values that way on a POST;
    each resource ignores the other's.

    Each resource has a floor under it: at most one refetch every
    ``MIN_MANUAL_REFRESH_SECONDS``.  The endpoint is unauthenticated on a LAN
    and every refetch is a token-bearing request to paperless-ngx, so without
    one a loop is unbounded upstream traffic.  A too-soon call skips the
    invalidation and renders the cached list instead of erroring, because the
    click is not wrong, only early: the list it would fetch is at most two
    seconds newer than the one it gets.  The floors are per resource, so a
    tag refresh never spends the correspondent list's.

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

    The posted ``job_id`` scopes the answer.  A Continue for any other
    job, one sent before that job reached the flip prompt, or one arriving
    after the prompt was already answered is dropped -- and the route still
    returns the current status rather than an error.  Nothing is
    waited for: the route renders whatever the store has recorded and the
    one-second poll picks up pass B from there.

    While the store still reads ``AWAITING_FLIP`` for an answered job, the
    partial acknowledges the answer in place of the Continue and Abort
    buttons, so a claimed or repeated click never re-renders a prompt that
    looks unanswered.

    A Continue from a browser that does not hold the job's owner token is
    dropped in exactly the same way, and for the same reason: the
    household member who did not load the paper must not be able to start
    pass B, and must not be shown a failure for trying either.  A job whose
    ``owner_token`` is NULL is unowned and anyone may answer it.

    The response re-renders the Scan button out-of-band from server state
    and leaves ``#status-message`` alone.  It moves focus to the status area:
    the pressed button is gone from the rendering, and the area is where the
    scan's progress is read from next.
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

    The posted ``job_id`` scopes the answer: a double-clicked Abort
    cannot land on the next queued job.  An Abort for any other job, one sent
    before that job reached the flip prompt, or one arriving after Continue
    already answered is dropped rather than reported as an error: the
    route returns the current status either way.

    While the store still reads ``AWAITING_FLIP`` for an answered job, the
    partial shows "Aborting scan..." (or the answer that won) in place of the
    buttons, so the response never invites a second click.

    An Abort from a browser that does not hold the job's owner token is
    dropped the same way: someone else's scan is not theirs to stop.
    A job whose ``owner_token`` is NULL is unowned and anyone may answer it.

    The response re-renders the Scan button out-of-band from server state
    and leaves ``#status-message`` alone.

    Focus goes to the Scan button: the operator is done with this scan.  A
    claimed Abort's own rendering usually still shows the button disabled,
    so the response focuses the status area for the interim and bakes
    ``focus=scan`` into its poll URL; the first rendering whose button is
    enabled focuses it.  A dropped Abort asks for nothing.
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

    The posted ``job_id`` and ``prompt`` scope the answer: it is claimed only
    for that job, only while that numbered question is the one open, and only
    when the question offers ``answer``.  Anything else is dropped -- an
    answer for another job, a click on a question that has since been
    replaced, a second click, or an answer the page never offered, such as
    Finish on a document with no pages -- and the route still returns the
    current status rather than an error.  Nothing is waited for: the route
    renders whatever the store has recorded, and the one-second poll picks up
    the next pass from there.

    ``answer`` and ``prompt`` are validated before the handler runs: a value
    that is not one of the answers, or a prompt number below 1, is a 422.  The
    clock's and the shutdown's answers pass that check but are never in a
    question's offered set, so the worker drops them like any other answer
    the page did not offer.

    Once this request has claimed the answer, the partial acknowledges it in
    place of the buttons even though the store still reads the job as
    waiting, so a claimed or repeated click never re-renders a question that
    looks unanswered.

    An answer from a browser that does not hold the job's owner token is
    dropped in exactly the same way: somebody else's document is not theirs to
    finish or abort, and they are not shown a failure for trying either.  A
    job whose ``owner_token`` is NULL is unowned and anyone may answer it.

    The response re-renders the Scan button out-of-band from server state
    and leaves ``#status-message`` alone.  It moves focus to the status area,
    where the next question's own primary button takes it when that question
    appears.  A claimed Abort asks for the Scan button instead, as the flip
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
