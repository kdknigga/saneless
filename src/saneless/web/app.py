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
from .routes import router
from .scan_block import scan_is_blocked
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

    Jinja allows mutating ``filters`` and ``globals`` only before a template
    is loaded.  The templates own no vocabulary: labels come from these filters
    and state comparisons go through the ``JobState`` global.  Each name is
    bound to the shared function itself, never a wrapper, so the web and
    ``saneless doctor`` render with the one implementation.

    Returns:
        The template environment every renderer in the app uses.

    """
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    # ``state_label`` is deliberately not registered: it reads the state alone
    # and so calls a warned upload "Complete".
    templates.env.filters["job_label"] = job_label
    templates.env.filters["job_status_class"] = job_status_class
    templates.env.filters["is_amber_category"] = is_amber_category
    templates.env.filters["outcome_line"] = outcome_line
    templates.env.filters["progress_label"] = progress_label
    templates.env.filters["flip_answer_label"] = flip_answer_label
    templates.env.filters["pass_answer_label"] = pass_answer_label
    templates.env.filters["scan_button_label"] = scan_button_label
    templates.env.filters["check_name"] = check_name
    # A row the registry skipped carries `CheckState.OK` so that `saneless
    # doctor` exits 0; these three draw it as skipped.  The state-only
    # `check_state_*` lookups are deliberately not registered, since they would
    # draw that row green.
    templates.env.filters["check_row_class"] = check_row_class
    templates.env.filters["check_row_glyph"] = check_row_glyph
    templates.env.filters["check_row_label"] = check_row_label
    templates.env.filters["check_step"] = render_check_step
    templates.env.filters["error_message"] = error_message
    templates.env.filters["error_next_step"] = error_next_step
    templates.env.filters["local_time"] = local_time
    templates.env.filters["page_counts"] = page_counts
    # An informational note: never route it through job.warning, which would
    # turn a plain DONE amber.
    templates.env.filters["removed_pages"] = removed_pages
    # Jinja2 3.1.6 builds `Environment.globals` from an unannotated dict, so the
    # checkers infer a narrow value type; the `Any`-typed names widen it in code
    # instead of a suppression.
    job_state_global: Any = JobState
    templates.env.globals["JobState"] = job_state_global
    check_surface_global: Any = CheckSurface
    templates.env.globals["CheckSurface"] = check_surface_global
    pass_wait_states_global: Any = PASS_WAIT_STATES
    templates.env.globals["pass_wait_states"] = pass_wait_states_global
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

    Neither reads the app's services, so the refresher is unit-testable with
    no FastAPI application.  Nothing is started or probed: the cache is cold.

    Args:
        settings: The loaded configuration every check reads.
        scanner: The backend the scanner check probes through the gate.
        paperless: The long-lived client the Paperless check reuses.
        worker: The source of the scanner gate and the profile-write outcome.

    Returns:
        The cold cache and the unstarted refresher that fills it.

    """
    # No config key sets this ttl: it is the class default, bound when
    # CheckCache is defined.  A test that wants another ttl passes its own.
    checks_cache = CheckCache()

    def build_check_context() -> CheckContext:
        """
        Assemble the context for one refresh, reading the worker each time.

        ``profile_storage`` is not captured: the worker records it after
        ``create_app`` has returned.
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
        # The same fact strip_view.checks_context renders, read from the same
        # place, so the strip's words and its colour cannot disagree.
        scan_active=lambda: worker.current_job_id is not None,
    )
    return checks_cache, refresher


def _stop_threads(worker: ScanWorker, refresher: CheckRefresher) -> tuple[bool, bool]:
    """
    Stop both background threads against one deadline; say whether each stopped.

    The two sequential joins spend one ``STOP_JOIN_SECONDS`` between them, not
    one each, because a container's stop grace period is sized on that number.
    The worker's join alone may extend by ``PRESERVATION_JOIN_SECONDS`` while a
    stopped scan's pages are kept; the refresher then gets only a poll.
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

    Both threads are stopped (stopping one that never started is safe), then
    the Paperless client and the job store are closed; if a thread did not
    stop, nothing is closed and this returns False.  The scanner stays with
    ``serve``, which closes it.
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

    ``data_dir`` is created 0700 first, since ``sqlite3.connect`` creates no
    parent directories; one that already exists keeps its mode.

    The conversion to incremental auto-vacuum runs on the server path only,
    because a CLI command opening the same file would race the server's
    connection.  Its ``VACUUM`` needs free space about the database's size, so
    a failure is a WARNING and the server starts with the file as it is.
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

    Returns the kept-file sentence for each recovered job, by job id; the job
    store appends it to the row's restart text.  The sentence keeps "preserved
    at " before every path, which is what lets the job view show the path to
    the job's owner only.  A sweep failure is logged and returns no sentences.
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

    Workspaces first, so a job whose pages were kept is failed with a text
    naming the kept file before the other active rows get their restart text.
    When the store refuses either write, the worker is marked recovery-pending
    with the same sentences and writes them once the store accepts writes.

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
    # App-wide, so a POST route added later cannot forget the cross-site check.
    app.add_middleware(CrossOriginGuard)
    # Runs before the cross-site check, on every method including GET, because
    # a DNS-rebinding page can read as easily as it can start a scan.
    app.add_middleware(HostGuard, allowed_hosts=settings.web.allowed_hosts)
    # Outermost, so the guards' refusals carry the headers too.  Only the 500
    # for an unhandled exception is sent outside it; render_error adds them there.
    app.add_middleware(SecurityHeaders)


def create_app(settings: Settings, scanner: ScannerBackend) -> FastAPI:
    """
    Create and configure the FastAPI application.

    The directories are validated first, so a refused one leaves nothing open.
    If anything after that raises, the job store and the Paperless client are
    closed before the error propagates; once built, the app's lifespan owns
    them.  The scanner belongs to the caller until the lifespan has started.

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

        Recovery runs before the worker starts, so a queued job lost from
        memory in a restart can never scan whatever paper is in the feeder
        now.  A recovery that cannot write starts the worker degraded with
        recovery pending, so ``/health`` answers 503; a prune failure is only
        logged.

        The check refresher starts last and never probes on the way up: the
        first render says ``Checking…`` and a bounded poll fills the strip in.
        See docs/explanation/decisions/0011-lazy-bounded-health-strip.md.

        A failed startup stops whichever thread it started, closes the
        Paperless client and the job store, and re-raises.
        ``lifecycle.started`` tells ``serve`` whether to close the scanner: it
        is also true when a failed startup left a thread that may be in SANE.
        Shutdown closes the client, the store and the scanner only when both
        threads confirm they stopped, and logs a failing close without raising.
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
            # After the worker: the refresher reads worker.scanner_gate and
            # worker.profile_storage at probe time.
            refresher.start()
        except BaseException:
            released = _release_after_failed_start(
                worker, refresher, job_store, paperless
            )
            # A thread still running may be inside SANE, so the lifespan keeps
            # the scanner rather than let serve shut it down.
            lifecycle.started = not released
            raise
        lifecycle.started = True
        logger.info("App started")
        yield
        worker_stopped, refresher_stopped = _stop_threads(worker, refresher)
        if not (worker_stopped and refresher_stopped):
            # A stuck thread may still use the store, the client or SANE, and
            # sane_exit() with a SANE call outstanding risks a segfault.  The
            # abandoned job is left for the next startup's recovery, since
            # writing it here would race its own final write.
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
            # Last in, first out: the client closes first and the scanner last,
            # since closing it shuts SANE down for the whole process.
            closing.callback(_close_logged, "scanner", scanner.close)
            closing.callback(_close_logged, "job store", job_store.close)
            closing.callback(_close_logged, "Paperless client", paperless.close)
        logger.info("App shutdown complete")

    # No schema, no /docs and no /redoc.  All three are named, so re-enabling
    # one cannot bring the others back with it.
    # See docs/explanation/decisions/0012-no-openapi.md.
    app = FastAPI(lifespan=lifespan, openapi_url=None, docs_url=None, redoc_url=None)
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
        # every call get a rate floor.
        invalidate_floors={
            "tags": MinimumInterval(),
            "correspondents": MinimumInterval(),
        },
        paperless_test_result=SingleFlightResult(
            ttl=MIN_MANUAL_REFRESH_SECONDS, wait_bound=PAPERLESS_TEST_WAIT_SECONDS
        ),
        # The status polls' "seen" tokens travel in URLs, and so into access
        # logs; a per-process key makes one unlinkable to its content.  A
        # restart costs each open page one full render.
        status_token_key=secrets.token_bytes(32),
        # Decided once: the settings are fixed for the life of the process.
        scan_blocked=scan_is_blocked(settings),
        lifecycle=lifecycle,
    )
    app.state.services = services

    app.mount(STATIC_PATH, StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)

    return app
