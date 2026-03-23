"""
Pipeline orchestration: scan -> assemble PDF -> upload to paperless-ngx.

Coordinates the full scan workflow within a temporary directory that
is automatically cleaned up on success or failure.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from saneless.exceptions import ConfigError, ScanError
from saneless.pages import filter_empty_pages, generate_thumbnail
from saneless.pdf import assemble_pdf
from saneless.scanner.base import ScanSettings

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable

    from PIL import Image

    from saneless.config import Settings
    from saneless.paperless import PaperlessClient
    from saneless.scanner.base import ScannerBackend

__all__ = ["PipelineEvent", "PipelineRequest", "run_pipeline"]


class PipelineEvent(StrEnum):
    """Events emitted by the scan pipeline to report progress."""

    SCANNING = "SCANNING"
    AWAITING_FLIP = "AWAITING_FLIP"
    SCANNING_REVERSE = "SCANNING_REVERSE"
    ASSEMBLING = "ASSEMBLING"
    UPLOADING = "UPLOADING"
    DONE = "DONE"


logger = logging.getLogger(__name__)


@dataclass
class PipelineRequest:
    """Parameters for a scan pipeline run."""

    profile_name: str
    title: str
    tags: list[int] | None = None
    correspondent: int | None = None
    status_callback: Callable[[PipelineEvent], None] | None = None
    thumbnail_callback: Callable[[str], None] | None = None
    flip_event: threading.Event | None = None
    abort_event: threading.Event | None = None


def _noop_callback(_event: PipelineEvent) -> None:
    """Default no-op status callback."""


def _check_disk_space(tmp_dir: str, min_free_mb: int) -> None:
    """
    Raise ScanError if insufficient disk space in tmp_dir.

    Args:
        tmp_dir: Path to the temporary directory used for scanning.
        min_free_mb: Minimum free space required in megabytes.

    Raises:
        ScanError: If free space is below the required threshold.

    """
    path = Path(tmp_dir)
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(path)
    free_mb = usage.free // (1024 * 1024)
    if free_mb < min_free_mb:
        msg = (
            f"Insufficient disk space: {free_mb} MB free in {path}, "
            f"{min_free_mb} MB required (configure min_free_space_mb to adjust)"
        )
        raise ScanError(msg)


def _is_manual_duplex(source: str) -> bool:
    """Check if the source string indicates manual duplex scanning."""
    return "manual" in source.lower() and "duplex" in source.lower()


def _handle_duplex_mismatch(
    passes: tuple[list[Image.Image], list[Image.Image]],
    tmp_path: Path,
    paperless: PaperlessClient,
    request: PipelineRequest,
    notify: Callable[[PipelineEvent], None],
) -> str:
    """
    Save and upload partial PDFs when duplex page counts mismatch.

    Instead of discarding scanned data, assembles fronts and backs into
    separate PDFs and uploads both to paperless-ngx for manual review.

    Args:
        passes: Tuple of (front-side images, back-side images).
        tmp_path: Temporary directory for PDF assembly.
        paperless: Paperless-ngx client for upload.
        request: Pipeline request with title, tags, correspondent.
        notify: Status callback function.

    Returns:
        Warning message describing the mismatch and recovery action.

    """
    fronts, backs = passes
    notify(PipelineEvent.ASSEMBLING)
    fronts_pdf = assemble_pdf(fronts, tmp_path / "fronts")
    backs_pdf = assemble_pdf(backs, tmp_path / "backs")
    logger.info(
        "Duplex mismatch: assembled %d fronts and %d backs as separate PDFs",
        len(fronts),
        len(backs),
    )

    notify(PipelineEvent.UPLOADING)
    created = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    title = request.title
    paperless.upload_document(
        fronts_pdf,
        f"{title} (fronts)",
        request.tags,
        request.correspondent,
        created,
    )
    paperless.upload_document(
        backs_pdf,
        f"{title} (backs)",
        request.tags,
        request.correspondent,
        created,
    )

    warning = (
        f"Page count mismatch: {len(fronts)} fronts, {len(backs)} backs. "
        f"Partial PDFs saved."
    )
    logger.warning(warning)
    return warning


def _interleave_duplex(
    fronts: list[Image.Image],
    backs: list[Image.Image],
) -> list[Image.Image]:
    """
    Interleave front and back pages for manual duplex.

    Backs are reversed because the user flips the stack face-down,
    so the last front's back is scanned first in pass B.

    Args:
        fronts: Front-side pages from pass A.
        backs: Back-side pages from pass B (in scan order).

    Returns:
        Interleaved pages: A1, B1, A2, B2, ...

    Raises:
        ScanError: If front and back page counts do not match.

    """
    if len(fronts) != len(backs):
        msg = f"Page count mismatch: {len(fronts)} fronts, {len(backs)} backs"
        raise ScanError(msg)
    backs_reversed = list(reversed(backs))
    result: list[Image.Image] = []
    for front, back in zip(fronts, backs_reversed, strict=True):
        result.append(front)
        result.append(back)
    return result


def _scan_manual_duplex(
    scanner: ScannerBackend,
    device_id: str,
    scan_settings: ScanSettings,
    request: PipelineRequest,
    notify: Callable[[PipelineEvent], None],
) -> list[Image.Image] | tuple[list[Image.Image], list[Image.Image]]:
    """
    Perform a two-pass manual duplex scan with flip coordination.

    Args:
        scanner: Scanner backend instance.
        device_id: SANE device identifier string.
        scan_settings: Scan settings for the scanner.
        request: Pipeline request with flip/abort events.
        notify: Status callback function.

    Returns:
        Interleaved list of images on matching page counts, or a tuple
        of (fronts, backs) when counts mismatch for recovery handling.

    Raises:
        ScanError: If scan is aborted by user.

    """
    # Pass A: scan fronts
    front_pages = list(scanner.scan_pages(device_id, scan_settings))
    logger.info("Pass A: scanned %d front page(s)", len(front_pages))

    # Generate thumbnail from first page
    if front_pages and request.thumbnail_callback:
        thumb = generate_thumbnail(front_pages[0])
        request.thumbnail_callback(thumb)

    # Signal awaiting flip
    if request.flip_event is not None:
        notify(PipelineEvent.AWAITING_FLIP)
        request.flip_event.wait()

        # Check if aborted
        if request.abort_event is not None and request.abort_event.is_set():
            msg = "Manual duplex scan cancelled by user"
            raise ScanError(msg)

    # Pass B: scan backs
    notify(PipelineEvent.SCANNING_REVERSE)
    back_pages = list(scanner.scan_pages(device_id, scan_settings))
    logger.info("Pass B: scanned %d back page(s)", len(back_pages))

    # Raw count validation BEFORE empty page detection (SCAN-07)
    if len(front_pages) != len(back_pages):
        return (front_pages, back_pages)

    images = _interleave_duplex(front_pages, back_pages)
    logger.info("Interleaved %d total pages", len(images))
    return images


def _scan_simplex(
    scanner: ScannerBackend,
    device_id: str,
    scan_settings: ScanSettings,
    request: PipelineRequest,
) -> list[Image.Image]:
    """
    Perform a simplex / hardware duplex / flatbed scan.

    Args:
        scanner: Scanner backend instance.
        device_id: SANE device identifier string.
        scan_settings: Scan settings for the scanner.
        request: Pipeline request with optional thumbnail callback.

    Returns:
        List of scanned page images.

    """
    images = list(scanner.scan_pages(device_id, scan_settings))
    logger.info("Scanned %d page(s)", len(images))

    # Generate thumbnail from first page
    if images and request.thumbnail_callback:
        thumb = generate_thumbnail(images[0])
        request.thumbnail_callback(thumb)

    return images


def _resolve_device(scanner: ScannerBackend, settings: Settings) -> str:
    """
    Resolve the scanner device ID from settings or auto-detection.

    Args:
        scanner: Scanner backend instance.
        settings: Application settings.

    Returns:
        SANE device identifier string.

    Raises:
        ConfigError: If no device is configured and auto-detection finds none.

    """
    device_id = settings.scanner.device
    if device_id:
        return device_id
    devices = scanner.get_devices()
    if not devices:
        msg = "No scanner found: settings.scanner.device is empty and auto-detection found no devices"
        raise ConfigError(msg)
    logger.info("Auto-detected scanner: %s", devices[0].name)
    return devices[0].name


def run_pipeline(
    scanner: ScannerBackend,
    paperless: PaperlessClient,
    settings: Settings,
    request: PipelineRequest,
) -> dict:
    """
    Run the full scan-to-upload pipeline.

    Scans pages from the configured device, assembles them into a PDF,
    uploads to paperless-ngx, and polls for task completion. All
    temporary files are cleaned up automatically via TemporaryDirectory.

    Args:
        scanner: Scanner backend instance.
        paperless: Paperless-ngx API client.
        settings: Application settings.
        request: Pipeline request parameters.

    Returns:
        Task result dict from paperless-ngx polling.

    Raises:
        ConfigError: If the profile or device is not configured.
        ScanError: If scanning fails.
        PaperlessError: If upload or polling fails.

    """
    notify = request.status_callback or _noop_callback

    if request.profile_name not in settings.profiles:
        msg = f"Unknown profile: {request.profile_name}"
        raise ConfigError(msg)

    profile = settings.profiles[request.profile_name]
    device_id = _resolve_device(scanner, settings)

    scan_settings = ScanSettings(
        source=profile.source,
        resolution=profile.resolution,
        mode=profile.mode,
        auto_source_mode=profile.auto_source_mode,
    )

    # Ensure tmp_dir exists
    Path(settings.output.tmp_dir).mkdir(parents=True, exist_ok=True)
    _check_disk_space(settings.output.tmp_dir, settings.output.min_free_space_mb)

    with tempfile.TemporaryDirectory(dir=settings.output.tmp_dir) as tmp_dir:
        tmp_path = Path(tmp_dir)

        # Step 1: Scan
        notify(PipelineEvent.SCANNING)
        logger.info(
            "Scanning with profile '%s' on device '%s'",
            request.profile_name,
            device_id,
        )

        if _is_manual_duplex(profile.source):
            duplex_result = _scan_manual_duplex(
                scanner,
                device_id,
                scan_settings,
                request,
                notify,
            )
            if isinstance(duplex_result, tuple):
                warning = _handle_duplex_mismatch(
                    duplex_result,
                    tmp_path,
                    paperless,
                    request,
                    notify,
                )
                notify(PipelineEvent.DONE)
                logger.info(
                    "Pipeline complete for '%s' (duplex mismatch recovery)",
                    request.title,
                )
                return {"status": "DONE", "warning": warning}
            images = duplex_result
        else:
            images = _scan_simplex(scanner, device_id, scan_settings, request)

        # Step 1.5: Strip EXIF from all images (Pitfall #5)
        for img in images:
            img.info.pop("exif", None)

        # Step 2: Filter empty pages (gated on profile toggle, per D-17)
        if profile.enable_empty_page_detection:
            filtered = filter_empty_pages(
                images,
                mean_threshold=profile.empty_page_mean_threshold,
                stddev_threshold=profile.empty_page_stddev_threshold,
            )
            if len(filtered) < len(images):
                logger.info(
                    "Empty page filter: %d -> %d pages",
                    len(images),
                    len(filtered),
                )
            if not filtered:
                msg = "All pages were detected as empty"
                raise ScanError(msg)
        else:
            filtered = images
            logger.info("Empty page detection disabled for profile")

        # Step 3: Assemble PDF
        notify(PipelineEvent.ASSEMBLING)
        pdf_path = assemble_pdf(filtered, tmp_path)
        logger.info("PDF assembled: %s", pdf_path)

        # Step 4: Upload
        notify(PipelineEvent.UPLOADING)
        created = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        task_uuid = paperless.upload_document(
            pdf_path,
            request.title,
            request.tags,
            request.correspondent,
            created,
        )

        # Step 5: Poll for result
        if task_uuid != "fallback":
            result = paperless.poll_task(
                task_uuid,
                timeout=settings.output.paperless_task_timeout,
            )
        else:
            result = {"status": "FALLBACK", "path": settings.paperless.consume_dir}

        notify(PipelineEvent.DONE)
        logger.info("Pipeline complete for '%s'", request.title)

    return result
