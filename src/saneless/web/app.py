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
from saneless.vocabulary import JobState, progress_label, state_label
from saneless.worker import ScanWorker

from .cache import MetadataCache
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
        token=settings.paperless.token,
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
        """Manage worker lifecycle and job pruning on startup/shutdown."""
        validate_settings_dirs(settings)
        worker.start()
        job_store.prune(
            settings.output.history_retention_days,
            settings.output.history_max_rows,
        )
        logger.info("App started, old jobs pruned")
        yield
        worker.stop()
        paperless.close()
        job_store.close()
        logger.info("App shutdown complete")

    app = FastAPI(lifespan=lifespan)

    app.state.worker = worker
    app.state.job_store = job_store
    app.state.settings = settings
    app.state.paperless = paperless
    app.state.cache = cache
    app.state.templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    # Registered before any template is loaded, which is the only requirement
    # Jinja places on mutating `filters` and `globals` on a live Environment.
    # The templates own no vocabulary of their own: labels come from these two
    # filters and state comparisons go through the JobState global.
    app.state.templates.env.filters["state_label"] = state_label
    app.state.templates.env.filters["progress_label"] = progress_label
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
