"""FastAPI application factory with lifespan management."""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from saneless.config import validate_settings_dirs
from saneless.job import JobStore
from saneless.paperless import PaperlessClient
from saneless.vocabulary import (
    RESTART_REASON,
    JobState,
    flip_answer_label,
    progress_label,
    state_label,
)
from saneless.worker import STOP_JOIN_SECONDS, ScanWorker

from .cache import MetadataCache
from .cross_origin import CrossOriginGuard
from .errors import install_error_handlers
from .routes import router

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from saneless.config import Settings
    from saneless.scanner.base import ScannerBackend

__all__ = ["create_app"]

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


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

        Shutdown stops the worker first and closes the Paperless client and
        the job store only when the worker confirms it stopped (D-09).
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
        logger.info("App started")
        yield
        # worker.stop() blocks the event loop for at most STOP_JOIN_SECONDS,
        # during lifespan shutdown, after uvicorn has stopped serving (D-08).
        if not worker.stop():
            # D-09: the store closes only after a confirmed stop, so a stuck
            # thread never hits "Cannot operate on a closed database".  D-07:
            # the abandoned job is not written here -- that would race its own
            # final write; the next startup's recovery records it.
            logger.warning(
                "Scan worker did not stop within %s s (job %s still running); "
                "leaving the job store and Paperless client open for process exit",
                STOP_JOIN_SECONDS,
                worker.current_job_id,
            )
            return
        paperless.close()
        job_store.close()
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
    app.state.templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    # Registered before any template is loaded, which is the only requirement
    # Jinja places on mutating `filters` and `globals` on a live Environment.
    # The templates own no vocabulary of their own: labels come from these
    # filters and state comparisons go through the JobState global.
    app.state.templates.env.filters["state_label"] = state_label
    app.state.templates.env.filters["progress_label"] = progress_label
    app.state.templates.env.filters["flip_answer_label"] = flip_answer_label
    # Jinja2 3.1.6 builds `Environment.globals` from the unannotated
    # `DEFAULT_NAMESPACE` dict, so a checker infers its value type as the union of
    # the six built-in helpers instead of the `MutableMapping[str, Any]` namespace
    # Jinja documents everywhere else. Handing the enum over through an explicitly
    # `Any`-typed name states that widening in code rather than suppressing the
    # resulting false positive with a comment.
    job_state_global: Any = JobState
    app.state.templates.env.globals["JobState"] = job_state_global

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)

    return app
