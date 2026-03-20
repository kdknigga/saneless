"""FastAPI application factory with lifespan management."""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from saneless.job import JobStore
from saneless.paperless import PaperlessClient
from saneless.worker import ScanWorker

from .cache import MetadataCache
from .routes import router

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

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
    job_store = JobStore(
        db_path=str(Path(settings.output.tmp_dir) / "saneless.db"),
    )
    cache = MetadataCache(ttl=settings.output.paperless_cache_ttl_seconds)
    worker = ScanWorker(scanner, paperless, settings, job_store)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """Manage worker lifecycle and job pruning on startup/shutdown."""
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

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)

    return app
