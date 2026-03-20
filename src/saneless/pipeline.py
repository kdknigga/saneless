"""Pipeline orchestration: scan -> assemble PDF -> upload to paperless-ngx.

Coordinates the full scan workflow within a temporary directory that
is automatically cleaned up on success or failure.
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .exceptions import ConfigError
from .paperless import PaperlessClient
from .pdf import assemble_pdf
from .scanner.base import ScannerBackend, ScanSettings

__all__ = ["run_pipeline"]

logger = logging.getLogger(__name__)


def _noop_callback(_msg: str) -> None:
    """Default no-op status callback."""


def run_pipeline(
    scanner: ScannerBackend,
    paperless: PaperlessClient,
    settings: Settings,
    profile_name: str,
    title: str,
    tags: list[int] | None = None,
    correspondent: int | None = None,
    status_callback: Callable[[str], None] | None = None,
) -> dict:
    """Run the full scan-to-upload pipeline.

    Scans pages from the configured device, assembles them into a PDF,
    uploads to paperless-ngx, and polls for task completion. All
    temporary files are cleaned up automatically via TemporaryDirectory.

    Args:
        scanner: Scanner backend instance.
        paperless: Paperless-ngx API client.
        settings: Application settings.
        profile_name: Name of the scan profile to use.
        title: Document title for paperless-ngx.
        tags: Optional list of tag IDs.
        correspondent: Optional correspondent ID.
        status_callback: Optional function called with status messages.

    Returns:
        Task result dict from paperless-ngx polling.

    Raises:
        ConfigError: If the profile or device is not configured.
        ScanError: If scanning fails.
        PaperlessError: If upload or polling fails.
    """
    notify = status_callback or _noop_callback

    if profile_name not in settings.profiles:
        msg = f"Unknown profile: {profile_name}"
        raise ConfigError(msg)

    profile = settings.profiles[profile_name]

    device_id = settings.scanner.device
    if not device_id:
        msg = "No scanner device configured (settings.scanner.device is empty)"
        raise ConfigError(msg)

    scan_settings = ScanSettings(
        source=profile.source,
        resolution=profile.resolution,
        mode=profile.mode,
    )

    # Ensure tmp_dir exists
    Path(settings.output.tmp_dir).mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=settings.output.tmp_dir) as tmp_dir:
        tmp_path = Path(tmp_dir)

        # Step 1: Scan
        notify("Scanning...")
        logger.info("Scanning with profile '%s' on device '%s'", profile_name, device_id)
        images = list(scanner.scan_pages(device_id, scan_settings))
        logger.info("Scanned %d page(s)", len(images))

        # Step 2: Assemble PDF
        notify("Assembling PDF...")
        pdf_path = assemble_pdf(images, tmp_path)
        logger.info("PDF assembled: %s", pdf_path)

        # Step 3: Upload
        notify("Uploading to paperless-ngx...")
        created = datetime.now(tz=timezone.utc).isoformat()
        task_uuid = paperless.upload_document(
            pdf_path, title, tags, correspondent, created,
        )

        # Step 4: Poll for result
        if task_uuid != "fallback":
            result = paperless.poll_task(
                task_uuid, timeout=settings.output.paperless_task_timeout,
            )
        else:
            result = {"status": "FALLBACK", "path": settings.paperless.consume_dir}

        notify(f"Done: {title}")
        logger.info("Pipeline complete for '%s'", title)

    return result
