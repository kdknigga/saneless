"""FastAPI application factory with lifespan management."""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from saneless.checks import (
    CheckContext,
    check_name,
    check_state_class,
    check_state_glyph,
    check_state_label,
)
from saneless.config import validate_settings_dirs
from saneless.job import JobStore
from saneless.paperless import PaperlessClient
from saneless.vocabulary import (
    RESTART_REASON,
    JobState,
    error_message,
    error_next_step,
    flip_answer_label,
    local_time,
    page_counts,
    progress_label,
    state_label,
)
from saneless.worker import STOP_JOIN_SECONDS, ScanWorker

from .cache import MetadataCache
from .checks_cache import CheckCache
from .cross_origin import CrossOriginGuard
from .errors import install_error_handlers
from .refresher import CheckRefresher
from .routes import router

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
    both read (APPL-12).

    Returns:
        The template environment every renderer in the app uses.

    """
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    templates.env.filters["state_label"] = state_label
    templates.env.filters["progress_label"] = progress_label
    templates.env.filters["flip_answer_label"] = flip_answer_label
    templates.env.filters["check_name"] = check_name
    templates.env.filters["check_state_class"] = check_state_class
    templates.env.filters["check_state_glyph"] = check_state_glyph
    templates.env.filters["check_state_label"] = check_state_label
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
    keeps it unit-testable with no FastAPI application at all (plan 30-07).

    Nothing is started and nothing is probed.  The cache is returned cold, so
    the first render says ``Checking…`` and the refresher's first tick with a
    watcher does the work (D-06).

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
    # key for this one, and D-03 fixes the answer at the class default.  One
    # default, read at call time, is what a test or a future setting overrides.
    checks_cache = CheckCache()

    def build_check_context() -> CheckContext:
        """
        Assemble the context for one refresh, reading the worker each time.

        ``profile_storage`` is read here rather than captured because the
        worker records it when it writes the generated profiles, which happens
        after ``create_app`` has already returned (D-22).
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
    )
    return checks_cache, refresher


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
    paperless = PaperlessClient(
        url=settings.paperless.url,
        token=settings.paperless.token.get_secret_value(),
        consume_dir=settings.paperless.consume_dir,
    )
    Path(settings.output.tmp_dir).mkdir(parents=True, exist_ok=True)
    # sqlite3.connect does not create parent directories, so data_dir must
    # exist before JobStore opens the database.
    Path(settings.output.data_dir).mkdir(parents=True, exist_ok=True)
    job_store = JobStore(db_path=str(settings.output.db_path))
    cache = MetadataCache(ttl=settings.output.paperless_cache_ttl_seconds)
    worker = ScanWorker(scanner, paperless, settings, job_store)
    checks_cache, refresher = _build_check_machinery(
        settings, scanner, paperless, worker
    )

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        """
        Recover, prune and start the worker at startup; stop it at shutdown.

        Startup runs in a fixed order (D-13): validate the directories, fail
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
        a self-stopping poll fills the strip in (D-06).  That is a deliberate
        continuation of Phase 26's non-blocking startup -- an unplugged scanner
        host is a TCP connect that hangs until the OS gives up, and a server
        that would not finish starting because of one is a worse appliance than
        one that starts and says so.

        Shutdown stops both threads before closing anything, and closes the
        Paperless client, the job store and the scanner only when both confirm
        they stopped (D-09, D-18, A-7).
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
        # Both stop events are set before either join begins, so the two
        # bounded joins overlap and the worst case stays STOP_JOIN_SECONDS
        # instead of doubling to one bound per thread (A-7).  request_stop()
        # is the signal half of the refresher's stop; worker.stop() sets its
        # own event as its first act and only then joins.
        refresher.request_stop()
        # worker.stop() blocks the event loop for at most STOP_JOIN_SECONDS,
        # during lifespan shutdown, after uvicorn has stopped serving (D-08).
        worker_stopped = worker.stop()
        refresher_stopped = refresher.stop()
        if not (worker_stopped and refresher_stopped):
            # D-09: the store closes only after a confirmed stop, so a stuck
            # thread never hits "Cannot operate on a closed database".  D-07:
            # the abandoned job is not written here -- that would race its own
            # final write; the next startup's recovery records it.
            #
            # A-7 extends the same guarantee to the refresher, which holds this
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
        # read, which is why the branch above returns instead (D-18).
        scanner.close()
        logger.info("App shutdown complete")

    app = FastAPI(lifespan=lifespan)
    # Every error response the app sends is rendered there (D-01).
    install_error_handlers(app)
    # App-wide, on every method except GET, HEAD and OPTIONS, so a POST route
    # added later cannot forget the cross-site check (D-23).  web_host still
    # defaults to 0.0.0.0; the LAN exposure that implies is documented.
    app.add_middleware(CrossOriginGuard)

    app.state.worker = worker
    app.state.job_store = job_store
    app.state.settings = settings
    app.state.paperless = paperless
    app.state.cache = cache
    app.state.checks = checks_cache
    app.state.refresher = refresher
    app.state.templates = _build_templates()

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)

    return app
