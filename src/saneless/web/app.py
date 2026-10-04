"""FastAPI application factory with lifespan management."""

from __future__ import annotations

import contextlib
import logging
import secrets
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from saneless.checks import (
    CheckContext,
    check_name,
    check_row_class,
    check_row_glyph,
    check_row_label,
)
from saneless.config import validate_settings_dirs
from saneless.job import JobStore
from saneless.paperless import PaperlessClient
from saneless.private_dirs import ensure_private_dir, make_private_dir
from saneless.vocabulary import (
    CORRESPONDENTS_LOADING,
    CORRESPONDENTS_UNAVAILABLE,
    PASS_WAIT_STATES,
    TAGS_LOADING,
    TAGS_UNAVAILABLE,
    CheckSurface,
    JobState,
    error_message,
    error_next_step,
    flip_answer_label,
    is_amber_category,
    job_label,
    job_status_class,
    local_time,
    outcome_line,
    page_counts,
    pass_answer_label,
    progress_label,
    removed_pages,
    render_check_step,
    scan_button_label,
)
from saneless.worker import PRESERVATION_JOIN_SECONDS, STOP_JOIN_SECONDS, ScanWorker
from saneless.workspace import sweep_orphans

from .cache import CachedMetadataLookup, MetadataCache
from .checks_cache import CheckCache
from .cross_origin import CrossOriginGuard
from .errors import install_error_handlers
from .host_guard import HostGuard
from .refresher import CheckRefresher
from .routes import router, scan_is_blocked
from .security_headers import STATIC_PATH, SecurityHeaders
from .services import AppLifecycle, Services
from .throttle import (
    MIN_MANUAL_REFRESH_SECONDS,
    PAPERLESS_TEST_WAIT_SECONDS,
    MinimumInterval,
    SingleFlightResult,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from saneless.config import Settings
    from saneless.scanner.base import ScannerBackend

__all__ = ["create_app"]

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def _build_templates() -> Jinja2Templates:
    """
    Build the template environment and hand it every word it may render.

    Filters are registered before any template is loaded, which is the only
    requirement Jinja places on mutating ``filters`` and ``globals`` on a live
    Environment.  The templates own no vocabulary of their own: labels come
    from these filters and state comparisons go through the ``JobState``
    global.

    Each name is bound to the shared function itself and never to a wrapper,
    because ``local_time`` here is the same object ``cli.py`` imports: the web
    table and the ``saneless doctor`` table cannot disagree about the zone or
    the format, and could not be made to without editing the one implementation
    both read.

    Returns:
        The template environment every renderer in the app uses.

    """
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    # A warned upload's label and headline are chosen from the state plus its
    # warning, so they cannot be a lookup on the state alone: these two take
    # the warning (and the headline the title) as filter arguments.
    # ``state_label`` is deliberately NOT registered: it reads the state alone
    # and so calls a warned upload "Complete", and a template that reached for
    # it would bring that label back.
    templates.env.filters["job_label"] = job_label
    # A row's colour is chosen by the same kind of function as its label, from
    # the state, the warning and the category, so no template compares states
    # to pick a class.  The status area asks whether an ERROR's category is
    # amber on its own, to drop the alert role as well as the red.
    templates.env.filters["job_status_class"] = job_status_class
    templates.env.filters["is_amber_category"] = is_amber_category
    templates.env.filters["outcome_line"] = outcome_line
    templates.env.filters["progress_label"] = progress_label
    templates.env.filters["flip_answer_label"] = flip_answer_label
    templates.env.filters["pass_answer_label"] = pass_answer_label
    # The Scan button's label, one function picking by state, so the button
    # cannot call a queued job "Scanning" while the status area says it waits.
    # The template hands it None for a finished job, as for no job at all:
    # the button offers a scan again once the job has an outcome.
    templates.env.filters["scan_button_label"] = scan_button_label
    templates.env.filters["check_name"] = check_name
    # The strip draws its rows with these three and not with the state lookups
    # they delegate to: a row the registry skipped carries `CheckState.OK` so
    # that `saneless doctor` keeps exiting 0, and a marker derived from the
    # state alone therefore ticked a row nothing had looked at.
    # `check_state_class`, `check_state_glyph` and `check_state_label` are
    # deliberately NOT registered as filters: no template uses them, the
    # delegation is in Python and needs no filter name, and a name in the
    # template namespace that draws a skipped row green is a trap for the next
    # row's author.
    templates.env.filters["check_row_class"] = check_row_class
    templates.env.filters["check_row_glyph"] = check_row_glyph
    templates.env.filters["check_row_label"] = check_row_label
    # One check row, two endings: a next step that says to try again holds a
    # placeholder, and the strip renders it as pressing the Check again button
    # beside it, where ``saneless doctor`` renders the same row as running the
    # command again.  The surface reaches the template as the enum member, by
    # the same route as ``JobState``, so the call site names no string.
    templates.env.filters["check_step"] = render_check_step
    templates.env.filters["error_message"] = error_message
    templates.env.filters["error_next_step"] = error_next_step
    templates.env.filters["local_time"] = local_time
    templates.env.filters["page_counts"] = page_counts
    # Beside the counts it explains.  An informational note, so the templates
    # render it in the muted page-counts style and never through job.warning,
    # which would turn a plain DONE amber.
    templates.env.filters["removed_pages"] = removed_pages
    # Jinja2 3.1.6 builds `Environment.globals` from the unannotated
    # `DEFAULT_NAMESPACE` dict, so a checker infers its value type as the union of
    # the six built-in helpers instead of the `MutableMapping[str, Any]` namespace
    # Jinja documents everywhere else. Handing the enum over through an explicitly
    # `Any`-typed name states that widening in code rather than suppressing the
    # resulting false positive with a comment.
    job_state_global: Any = JobState
    templates.env.globals["JobState"] = job_state_global
    check_surface_global: Any = CheckSurface
    templates.env.globals["CheckSurface"] = check_surface_global
    # The multi-page waiting states, by the same route and for the same reason.
    # The status area and the Scan button test membership of this one set, so
    # a template never lists the states by hand and cannot drift from it.
    pass_wait_states_global: Any = PASS_WAIT_STATES
    templates.env.globals["pass_wait_states"] = pass_wait_states_global
    # What the tag and correspondent lists say while they load and when they
    # could not be loaded.  Vocabulary owns the copy and the templates read it
    # by name, so no template spells a sentence out, and every rendering of a
    # list, whichever route sends it, says the same thing.
    list_copy: dict[str, Any] = {
        "TAGS_LOADING": TAGS_LOADING,
        "TAGS_UNAVAILABLE": TAGS_UNAVAILABLE,
        "CORRESPONDENTS_LOADING": CORRESPONDENTS_LOADING,
        "CORRESPONDENTS_UNAVAILABLE": CORRESPONDENTS_UNAVAILABLE,
    }
    templates.env.globals.update(list_copy)
    return templates


def _build_check_machinery(
    settings: Settings,
    scanner: ScannerBackend,
    paperless: PaperlessClient,
    worker: ScanWorker,
) -> tuple[CheckCache, CheckRefresher]:
    """
    Build the status strip's cache and the thread that fills it.

    Neither reaches into the app's services: everything the refresher needs arrives
    through the context factory and the gate accessor built here, which is what
    keeps it unit-testable with no FastAPI application at all.

    Nothing is started and nothing is probed.  The cache is returned cold, so
    the first render says ``Checking…`` and the refresher's first tick with a
    watcher does the work.

    Args:
        settings: The loaded configuration every check reads.
        scanner: The backend the scanner check probes through the gate.
        paperless: The long-lived client the Paperless check reuses.
        worker: The source of the scanner gate and the profile-write outcome.

    Returns:
        The cold cache and the unstarted refresher that fills it.  The
        refresher also hands the context factory back out through
        :meth:`~saneless.web.refresher.CheckRefresher.build_context`, so
        every probe, a tick's or one ``POST /api/checks/refresh`` asked for,
        runs on the same assembled dependencies rather than a second set.

    """
    # No ttl is passed: unlike the Paperless metadata cache there is no config
    # key for this one, so the answer is the class default, bound when
    # CheckCache is defined.  A test that wants another ttl passes its own.
    checks_cache = CheckCache()

    def build_check_context() -> CheckContext:
        """
        Assemble the context for one refresh, reading the worker each time.

        ``profile_storage`` is read here rather than captured because the
        worker records it when it writes the generated profiles, which happens
        after ``create_app`` has already returned.
        """
        return CheckContext(
            settings=settings,
            scanner=scanner,
            paperless=paperless,
            profile_storage=worker.profile_storage,
        )

    refresher = CheckRefresher(
        cache=checks_cache,
        context_factory=build_check_context,
        # The worker creates its scanner gate once, so this returns the same
        # lock on every call; it is a callable only to match scan_active.
        scanner_gate=lambda: worker.scanner_gate,
        # The same fact _checks_context renders as scan_active, read from the
        # same place, so the strip's words and its colour cannot disagree.
        scan_active=lambda: worker.current_job_id is not None,
    )
    return checks_cache, refresher


def _stop_threads(worker: ScanWorker, refresher: CheckRefresher) -> tuple[bool, bool]:
    """
    Bring both background threads down against one shared deadline.

    The deadline is taken before either join and the two joins spend it
    between them, so whatever the worker's join used is gone from the
    refresher's share and the worst case stays one ``STOP_JOIN_SECONDS``
    rather than one per thread (the joins are sequential, so signalling both
    events first does not make them overlap).  The one exception is the
    worker keeping a stopped scan's pages: its join is then extended by up to
    ``PRESERVATION_JOIN_SECONDS``, so the worst case is the sum of the two,
    and the refresher, whose share is long spent by then, gets a poll.
    The total is what matters because a refresher parked in an unbounded
    ``getaddrinfo`` or inside ``sane_get_devices`` is exactly the case the
    bound exists for, and exactly the case a per-thread bound would double --
    and a container's stop grace period is sized on the documented number.

    ``request_stop()`` before the worker's join is still worth doing: a
    refresher merely between ticks wakes on the event during that join and
    exits for free, so its own join is skipped entirely.  The worker goes
    first because it is the thread that must be confirmed stopped before any
    resource closes, and its own stop blocks the event loop for at most
    ``STOP_JOIN_SECONDS`` -- plus ``PRESERVATION_JOIN_SECONDS`` while pages
    are being kept -- during lifespan shutdown, after uvicorn has stopped
    serving.

    This join is the largest part of the server's stop budget.  An idle
    server stops within 10 seconds, Docker's default grace period: the
    request drain, these joins, the closes and SANE's shutdown all fit inside
    it.  A stop that lands while a stopped scan's pages are being preserved
    may take up to 90 seconds, which the shipped ``stop_grace_period`` covers.

    Args:
        worker: The scan worker to stop first.
        refresher: The check refresher, joined with what is left of the bound.

    Returns:
        Whether the worker stopped, and whether the refresher stopped.

    """
    refresher.request_stop()
    deadline = time.monotonic() + STOP_JOIN_SECONDS
    worker_stopped = worker.stop()
    refresher_stopped = refresher.stop(timeout=max(0.0, deadline - time.monotonic()))
    return worker_stopped, refresher_stopped


def _close_logged(name: str, close: Callable[[], object]) -> None:
    """
    Run one resource's close, logging a failure instead of raising it.

    The server is on its way down when this runs, and one close that raises
    must not leave the resources after it open.

    Args:
        name: What is being closed, as the log line names it.
        close: The resource's close method.

    """
    try:
        close()
    except Exception:
        logger.exception("Closing the %s failed; the rest are still closed", name)


def _release_after_failed_start(
    worker: ScanWorker,
    refresher: CheckRefresher,
    job_store: JobStore,
    paperless: PaperlessClient,
) -> bool:
    """
    Undo a lifespan start-up that raised part-way.

    Whichever thread had started is stopped first, against the same deadline
    as a shutdown.  Stopping a thread that never started is safe, so both are
    asked.  The Paperless client and the job store are then closed, each on
    its own, as at shutdown.  The scanner is not closed here: ``serve`` holds
    it until the lifespan has taken ownership, and closes it itself.

    A thread that does not stop may still be using all three, so then nothing
    is closed, for the reason the shutdown gives.

    Args:
        worker: The scan worker, started or not.
        refresher: The check refresher, started or not.
        job_store: The job store to close.
        paperless: The Paperless client to close.

    Returns:
        Whether both threads stopped and the store and client were closed.

    """
    worker_stopped, refresher_stopped = _stop_threads(worker, refresher)
    if not (worker_stopped and refresher_stopped):
        logger.warning(
            "A background thread did not stop after the failed start-up; "
            "leaving the job store, Paperless client and scanner open for "
            "process exit"
        )
        return False
    with contextlib.ExitStack() as closing:
        # Last in, first out: the client closes first, then the store.
        closing.callback(_close_logged, "job store", job_store.close)
        closing.callback(_close_logged, "Paperless client", paperless.close)
    return True


def _open_job_store(settings: Settings) -> JobStore:
    """
    Open the server's job store, creating ``data_dir`` and converting old files.

    ``sqlite3.connect`` does not create parent directories, so ``data_dir``
    must exist first.  It is created 0700; one that already exists keeps its
    mode.

    A database an earlier release created is converted to incremental
    auto-vacuum here, once, on the server path only: a CLI command opening the
    same file would race the running server's connection.  The conversion is
    best effort -- its ``VACUUM`` needs free space about the size of the
    database, and a database that needs shrinking may sit on a full disk -- so
    a failure is a WARNING and the server starts with the file as it is.

    Args:
        settings: Application settings naming ``data_dir`` and the database.

    Returns:
        The open job store.

    """
    make_private_dir(settings.output.data_dir)
    job_store = JobStore(db_path=settings.output.db_path)
    try:
        job_store.enable_incremental_auto_vacuum()
    except sqlite3.Error:
        logger.warning(
            "Could not convert the job database to incremental auto-vacuum; "
            "it will not shrink after deletes",
            exc_info=True,
        )
    return job_store


def _recover_orphaned_workspaces(settings: Settings) -> dict[str, str]:
    """
    Keep the pages a killed process left in ``tmp_dir``, and say where they went.

    Runs the workspace sweep and returns, for each recovered workspace that
    kept something, the sentence naming the preserved file.  The job store
    composes each row's error from it, by the state the row was left in: the
    restart text for that state, then this sentence.  The sentence keeps
    "preserved at " before every path, which is what lets the job view show
    the path to the job's owner, relative to ``data_dir``, and hide it from
    anyone else.

    A sweep failure never stops the app starting: it is logged with its
    traceback and there are then no sentences, so every row left active gets
    only its restart text.

    Args:
        settings: Application settings; ``tmp_dir``, ``failed_dir`` and
            ``min_free_space_mb`` are read.

    Returns:
        The kept-file sentence for each recovered job, by job id.

    """
    output = settings.output
    try:
        recovered = sweep_orphans(
            output.tmp_dir, output.failed_dir, output.min_free_space_mb
        )
    except Exception:
        logger.warning("Startup workspace recovery failed", exc_info=True)
        return {}
    return {
        entry.job_id: entry.sentence
        for entry in recovered
        if entry.job_id and entry.sentence
    }


def _recover_interrupted_jobs(
    settings: Settings, job_store: JobStore, worker: ScanWorker
) -> None:
    """
    Recover what a previous process left behind: its workspaces, then its rows.

    The orphaned workspaces are recovered first, so each job whose pages were
    kept is failed with a text naming the kept file; every other row still
    active is then failed with only its restart text.  The store words each
    row by the state it was left in, so an uploading row reads as possibly in
    paperless-ngx rather than unfinished.  When the job store refuses either
    write, the worker is marked recovery-pending with the same sentences, so
    it starts degraded and writes them once the store accepts writes.

    Args:
        settings: Application settings, for the workspace sweep.
        job_store: The job store the previous process's rows are in.
        worker: The scan worker, not started yet.

    """
    kept = _recover_orphaned_workspaces(settings)
    try:
        failed = 0
        if kept:
            failed = job_store.fail_recovered_jobs(kept)
        failed += job_store.fail_active_jobs()
    except Exception:
        # logger.exception is an ERROR record with the traceback attached.
        logger.exception(
            "Crash recovery could not update the job store; "
            "starting degraded until it accepts writes"
        )
        worker.mark_recovery_pending(kept)
    else:
        if failed:
            logger.warning("Marked %d interrupted job(s) as failed at startup", failed)


def _install_middleware(app: FastAPI, settings: Settings) -> None:
    """
    Add the application's own middleware, in the order that decides who runs first.

    ``add_middleware`` is LIFO: the last one added is outermost and runs first
    on the way in and last on the way out.

    Args:
        app: The application being built.
        settings: Application settings; ``[web] allowed_hosts`` is read.

    """
    # App-wide, on every method except GET, HEAD and OPTIONS, so a POST route
    # added later cannot forget the cross-site check.  web_host still
    # defaults to 0.0.0.0; the LAN exposure that implies is documented.
    app.add_middleware(CrossOriginGuard)
    # Added after the cross-site check, so the Host check wraps it and runs
    # before it.  It applies to every method, GET and /health included,
    # because a DNS-rebinding page can read as easily as it can start a scan.
    app.add_middleware(HostGuard, allowed_hosts=settings.web.allowed_hosts)
    # Added last, so it is the outermost of the app's own middleware: every
    # route and static file, the router's 404 and 405, and the Host check's
    # 421 and 400 and the cross-site check's 403 all pass through it on the
    # way out.  Only the 500 for an unhandled exception is sent outside it,
    # and render_error gives that one the same headers.
    app.add_middleware(SecurityHeaders)


def create_app(settings: Settings, scanner: ScannerBackend) -> FastAPI:
    """
    Create and configure the FastAPI application.

    Sets up the paperless client, job store, metadata cache, and scan
    worker. Mounts static files and registers all route handlers.

    The configured directories are validated first, so a refused one leaves
    nothing created and nothing open.  If anything after that raises, the job
    store and the Paperless client already opened are closed before the error
    propagates.  Once the app is built they belong to it, and its lifespan
    closes them.  The scanner is never closed here: it belongs to the caller
    until the lifespan has started.

    Args:
        settings: Application settings instance.
        scanner: Scanner backend to use for scan jobs.

    Returns:
        Configured FastAPI application ready to serve.

    Raises:
        ConfigError: A configured directory is unusable or unsafe.

    """
    validate_settings_dirs(settings)
    with contextlib.ExitStack() as acquired:
        # Scratch space is created 0700 and refused if another user got to
        # the name first.
        ensure_private_dir(settings.output.tmp_dir, key="output.tmp_dir")
        job_store = _open_job_store(settings)
        acquired.callback(job_store.close)
        # Reading the TLS trust store can fail here, with the store open.
        paperless = PaperlessClient(
            url=settings.paperless.url,
            token=settings.paperless.token.get_secret_value(),
            consume_dir=settings.paperless.consume_dir,
        )
        acquired.callback(paperless.close)
        app = _assemble_app(settings, scanner, job_store, paperless)
        # Built: from here the app's lifespan owns the store and the client.
        acquired.pop_all()
    return app


def _assemble_app(
    settings: Settings,
    scanner: ScannerBackend,
    job_store: JobStore,
    paperless: PaperlessClient,
) -> FastAPI:
    """
    Build the app around a job store and a Paperless client already open.

    Args:
        settings: Application settings instance.
        scanner: Scanner backend to use for scan jobs.
        job_store: The server's open job store.
        paperless: The server's Paperless client.

    Returns:
        Configured FastAPI application ready to serve.

    """
    cache = MetadataCache(ttl=settings.output.paperless_cache_ttl_seconds)
    # Each job checks its ids against the lists the pickers were served from,
    # asking paperless-ngx again only for an id they do not hold.
    worker = ScanWorker(
        scanner,
        paperless,
        settings,
        job_store,
        metadata_lookup=CachedMetadataLookup(cache, paperless),
    )
    checks_cache, refresher = _build_check_machinery(
        settings, scanner, paperless, worker
    )
    lifecycle = AppLifecycle()

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        """
        Recover, prune and start the worker at startup; stop it at shutdown.

        The directories were validated when the app was built.  Startup then
        runs in a fixed order: recover
        the workspaces a killed process left in ``tmp_dir``, fail every job
        a previous process left active, prune history, and only then start
        the worker.  Recovery comes before the worker so the worker never
        sees an orphan as live work, and so a queued job that was lost from
        memory in a restart can never be picked up and scan whatever paper
        happens to be in the feeder now.  The workspaces are recovered first
        so that a job whose pages were kept is failed with a text naming the
        kept file, before the restart text alone reaches every other row.
        The store words each row by the state it was left in.

        A workspace recovery that fails is only logged, and the rows then get
        their restart text alone.  A recovery that cannot write does not
        refuse to start the app.  The worker starts degraded with recovery
        pending instead, holding the recovered rows' sentences, so ``/health``
        answers a truthful 503 and the worker's first successful store probe
        runs the recovery.  A prune failure is only logged: history that
        outlives its retention is harmless, and a service that will not come
        up over it is not.

        The check refresher starts last, after the worker, and never probes on
        the way up: the cache is cold, the first render says ``Checking…`` and
        a self-stopping poll fills the strip in.  That is a deliberate
        continuation of the worker's non-blocking startup -- an unplugged scanner
        host is a TCP connect that hangs until the OS gives up, and a server
        that would not finish starting because of one is a worse appliance than
        one that starts and says so.

        A startup that raises stops whichever thread it had started, closes
        the Paperless client and the job store, and re-raises, so the server
        refuses to start with nothing left open.  The scanner stays with
        ``serve``, which closes it when the lifespan never took it over.
        The services' ``lifecycle.started`` is what tells it: true once startup
        finished, and also when a failed startup left a thread running, since
        that thread may still be inside SANE.

        Shutdown stops both threads before closing anything, and closes the
        Paperless client, the job store and the scanner only when both confirm
        they stopped.  Each close runs on its own: one that raises is logged
        with its traceback and the others still run, in the same order.  It
        is not raised again, because the process is stopping either way and
        every other resource has been released.

        The whole stop fits Docker's default 10-second grace period when the
        server is idle.  A stop that lands while a stopped scan's pages are
        being preserved may take up to 90 seconds, which the shipped
        ``stop_grace_period`` covers.
        """
        try:
            _recover_interrupted_jobs(settings, job_store, worker)
            try:
                job_store.prune(
                    settings.output.history_retention_days,
                    settings.output.history_max_rows,
                )
            except Exception:
                logger.warning("Startup history prune failed", exc_info=True)
            worker.start()
            # After the worker, because the refresher's gate accessor reads
            # worker.scanner_gate at probe time and its context reads
            # worker.profile_storage.  Starting it costs one thread and no
            # probe.
            refresher.start()
        except BaseException:
            released = _release_after_failed_start(
                worker, refresher, job_store, paperless
            )
            # A thread still running may be inside SANE, so the scanner must
            # not be shut down under it: the lifespan keeps it, as it would
            # at shutdown.
            lifecycle.started = not released
            raise
        lifecycle.started = True
        logger.info("App started")
        yield
        worker_stopped, refresher_stopped = _stop_threads(worker, refresher)
        if not (worker_stopped and refresher_stopped):
            # The store closes only after a confirmed stop, so a stuck thread
            # never hits "Cannot operate on a closed database".  The abandoned
            # job is not written here -- that would race its own final write;
            # the next startup's recovery records it.
            #
            # The same guarantee extends to the refresher, which holds this
            # same Paperless client and may be inside sane_get_devices:
            # paperless.close() would raise inside a live probe, and
            # scanner.close() would run sane_exit() with a SANE call
            # outstanding, which sane_backend.shutdown() documents as a
            # segfault risk.
            stuck = " and ".join(
                name
                for name, stopped in (
                    ("Scan worker", worker_stopped),
                    ("check refresher", refresher_stopped),
                )
                if not stopped
            )
            logger.warning(
                "%s did not stop within %s s (the scan worker's join is "
                "extended by up to %g s while a stopped scan's pages are being "
                "preserved); leaving the job store, Paperless client and scanner "
                "open for process exit (current job: %s)",
                stuck,
                STOP_JOIN_SECONDS,
                PRESERVATION_JOIN_SECONDS,
                worker.current_job_id,
            )
            return
        with contextlib.ExitStack() as closing:
            # Registered in reverse, because the stack runs its callbacks last
            # in, first out: the Paperless client closes first, then the job
            # store, then the scanner.  Each is guarded on its own, and the
            # stack still runs the rest if one raises something the guard
            # does not catch.
            #
            # The scanner is last, and closed only here: closing it shuts SANE
            # down for the whole process, and sane_exit() closes every open
            # handle while holding the GIL.  A worker that did not stop may
            # still be inside a read, which is why the branch above returns
            # instead.
            closing.callback(_close_logged, "scanner", scanner.close)
            closing.callback(_close_logged, "job store", job_store.close)
            closing.callback(_close_logged, "Paperless client", paperless.close)
        logger.info("App shutdown complete")

    # No generated schema and no interactive documentation: an unauthenticated
    # LAN appliance serving an HTMX UI has no stable API contract to advertise;
    # its endpoints are the UI's own.  All three are named, so switching
    # openapi_url back on later cannot bring /docs and /redoc back with it.
    app = FastAPI(lifespan=lifespan, openapi_url=None, docs_url=None, redoc_url=None)
    # Every error response the app sends is rendered there.
    install_error_handlers(app)
    _install_middleware(app, settings)

    services = Services(
        worker=worker,
        job_store=job_store,
        settings=settings,
        paperless=paperless,
        cache=cache,
        checks=checks_cache,
        refresher=refresher,
        templates=_build_templates(),
        # The two routes that send a token-bearing request to paperless-ngx on
        # every call get a rate floor, decided in web/throttle.py: one refetch
        # per resource per floor window, and one shared connection-test answer.
        invalidate_floors={
            "tags": MinimumInterval(),
            "correspondents": MinimumInterval(),
        },
        paperless_test_result=SingleFlightResult(
            ttl=MIN_MANUAL_REFRESH_SECONDS, wait_bound=PAPERLESS_TEST_WAIT_SECONDS
        ),
        # The key the status polls' "seen" tokens are hashed with.  Those
        # tokens travel in URLs, and so into access logs; a key minted per
        # process makes one unlinkable to the content it stands for and
        # impossible to compute ahead of time.  A restart mints a new key,
        # which costs each open page one full render on its next poll and
        # nothing else.
        status_token_key=secrets.token_bytes(32),
        # Whether paperless-ngx is configured well enough for any scan to
        # start.  The settings are fixed for the life of the process, so it is
        # decided once; the error rendering reads it so a refusal on a blocked
        # appliance never re-renders Scan enabled.
        scan_blocked=scan_is_blocked(settings),
        # Set by the lifespan once it owns the scanner; serve closes the
        # scanner itself while this is still false.
        lifecycle=lifecycle,
    )
    app.state.services = services

    app.mount(STATIC_PATH, StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)

    return app
