"""FastAPI application factory with lifespan management."""

from __future__ import annotations

import contextlib
import logging
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
    RESTART_REASON,
    JobState,
    error_message,
    error_next_step,
    flip_answer_label,
    job_label,
    local_time,
    outcome_line,
    page_counts,
    progress_label,
)
from saneless.worker import STOP_JOIN_SECONDS, ScanWorker

from .cache import MetadataCache
from .checks_cache import CheckCache
from .cross_origin import CrossOriginGuard
from .errors import install_error_handlers
from .host_guard import HostGuard
from .refresher import CheckRefresher
from .routes import router
from .security_headers import STATIC_PATH, SecurityHeaders
from .throttle import (
    MIN_MANUAL_REFRESH_SECONDS,
    PAPERLESS_TEST_WAIT_SECONDS,
    MinimumInterval,
    SingleFlightResult,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

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
    templates.env.filters["outcome_line"] = outcome_line
    templates.env.filters["progress_label"] = progress_label
    templates.env.filters["flip_answer_label"] = flip_answer_label
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
    templates.env.filters["error_message"] = error_message
    templates.env.filters["error_next_step"] = error_next_step
    templates.env.filters["local_time"] = local_time
    templates.env.filters["page_counts"] = page_counts
    # Jinja2 3.1.6 builds `Environment.globals` from the unannotated
    # `DEFAULT_NAMESPACE` dict, so a checker infers its value type as the union of
    # the six built-in helpers instead of the `MutableMapping[str, Any]` namespace
    # Jinja documents everywhere else. Handing the enum over through an explicitly
    # `Any`-typed name states that widening in code rather than suppressing the
    # resulting false positive with a comment.
    job_state_global: Any = JobState
    templates.env.globals["JobState"] = job_state_global
    return templates


def _build_check_machinery(
    settings: Settings,
    scanner: ScannerBackend,
    paperless: PaperlessClient,
    worker: ScanWorker,
) -> tuple[CheckCache, CheckRefresher]:
    """
    Build the status strip's cache and the thread that fills it.

    Neither reaches into ``app.state``: everything the refresher needs arrives
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
        :meth:`~saneless.web.refresher.CheckRefresher.build_context`, which is
        how ``POST /api/checks/refresh`` probes from the same assembled
        dependencies rather than putting them together a second time.

    """
    # No ttl is passed: unlike the Paperless metadata cache there is no config
    # key for this one, so the answer is the class default.  One default, read
    # at call time, is what a test or a future setting overrides.
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
        # Late-bound on purpose: the gate is fetched at probe time, so the
        # refresher follows a worker that is rebuilt rather than holding a lock
        # nothing else uses any more.
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
    events first does not make them overlap).
    The total is what matters because a refresher parked in an unbounded
    ``getaddrinfo`` or inside ``sane_get_devices`` is exactly the case the
    bound exists for, and exactly the case a per-thread bound would double --
    and a container's stop grace period is sized on the documented number.

    ``request_stop()`` before the worker's join is still worth doing: a
    refresher merely between ticks wakes on the event during that join and
    exits for free, so its own join is skipped entirely.  The worker goes
    first because it is the thread that must be confirmed stopped before any
    resource closes, and its own stop blocks the event loop for at most
    ``STOP_JOIN_SECONDS``, during lifespan shutdown, after uvicorn has stopped
    serving.

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

    Args:
        settings: Application settings instance.
        scanner: Scanner backend to use for scan jobs.

    Returns:
        Configured FastAPI application ready to serve.

    """
    # Before anything that holds a resource, so a refused directory leaves
    # nothing open.  Scratch space is created 0700 and refused if another
    # user got to the name first; the lifespan's validate_settings_dirs then
    # sees the directory this made.
    ensure_private_dir(settings.output.tmp_dir, key="output.tmp_dir")
    job_store = _open_job_store(settings)
    paperless = PaperlessClient(
        url=settings.paperless.url,
        token=settings.paperless.token.get_secret_value(),
        consume_dir=settings.paperless.consume_dir,
    )
    cache = MetadataCache(ttl=settings.output.paperless_cache_ttl_seconds)
    worker = ScanWorker(scanner, paperless, settings, job_store)
    checks_cache, refresher = _build_check_machinery(
        settings, scanner, paperless, worker
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        """
        Recover, prune and start the worker at startup; stop it at shutdown.

        Startup runs in a fixed order: validate the directories, fail
        every job a previous process left active, prune history, and only
        then start the worker.  Recovery comes before the worker so the
        worker never sees an orphan as live work, and so a queued job that
        was lost from memory in a restart can never be picked up and scan
        whatever paper happens to be in the feeder now.

        A recovery that cannot write does not refuse to start the app.  The
        worker starts degraded with recovery pending instead, so ``/health``
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

        Shutdown stops both threads before closing anything, and closes the
        Paperless client, the job store and the scanner only when both confirm
        they stopped.
        """
        validate_settings_dirs(settings)
        try:
            failed = job_store.fail_active_jobs(RESTART_REASON)
        except Exception:
            # logger.exception is an ERROR record with the traceback attached.
            logger.exception(
                "Crash recovery could not update the job store; "
                "starting degraded until it accepts writes"
            )
            worker.mark_recovery_pending()
        else:
            if failed:
                logger.warning(
                    "Marked %d interrupted job(s) as failed at startup", failed
                )
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
        # worker.profile_storage.  Starting it costs one thread and no probe.
        refresher.start()
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
                "%s did not stop within %s s; leaving the job store, Paperless "
                "client and scanner open for process exit (current job: %s)",
                stuck,
                STOP_JOIN_SECONDS,
                worker.current_job_id,
            )
            return
        paperless.close()
        job_store.close()
        # Last, and only here: closing the scanner shuts SANE down for the
        # whole process, and sane_exit() closes every open handle while
        # holding the GIL.  A worker that did not stop may still be inside a
        # read, which is why the branch above returns instead.
        scanner.close()
        logger.info("App shutdown complete")

    # No generated schema and no interactive documentation: an unauthenticated
    # LAN appliance serving an HTMX UI has no stable API contract to advertise;
    # its endpoints are the UI's own.  All three are named, so switching
    # openapi_url back on later cannot bring /docs and /redoc back with it.
    app = FastAPI(lifespan=lifespan, openapi_url=None, docs_url=None, redoc_url=None)
    # Every error response the app sends is rendered there.
    install_error_handlers(app)
    _install_middleware(app, settings)

    app.state.worker = worker
    app.state.job_store = job_store
    app.state.settings = settings
    app.state.paperless = paperless
    app.state.cache = cache
    app.state.checks = checks_cache
    app.state.refresher = refresher
    app.state.templates = _build_templates()
    # The two routes that send a token-bearing request to paperless-ngx on
    # every call get a rate floor, decided in web/throttle.py: one refetch
    # per resource per floor window, and one shared connection-test answer.
    app.state.invalidate_floors = {
        "tags": MinimumInterval(),
        "correspondents": MinimumInterval(),
    }
    app.state.paperless_test_result = SingleFlightResult(
        ttl=MIN_MANUAL_REFRESH_SECONDS, wait_bound=PAPERLESS_TEST_WAIT_SECONDS
    )

    app.mount(STATIC_PATH, StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)

    return app
