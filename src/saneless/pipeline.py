"""
Pipeline orchestration: scan -> assemble PDF -> upload to paperless-ngx.

Coordinates the full scan workflow within a temporary directory that
is automatically cleaned up on success or failure.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, assert_never

from saneless.exceptions import (
    ConfigError,
    PaperlessError,
    SanelessError,
    ScanError,
)
from saneless.pages import filter_empty_pages, generate_thumbnail
from saneless.pdf import assemble_pdf, build_pdf_filename
from saneless.scanner.base import ScanSettings
from saneless.vocabulary import JobState, ScanOutcome

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Iterator, Sequence

    from PIL import Image

    from saneless.config import ProfileConfig, Settings
    from saneless.paperless import PaperlessClient
    from saneless.scanner.base import ScannerBackend

__all__ = ["PipelineEvent", "PipelineRequest", "ScanResult", "run_pipeline"]


class PipelineEvent(StrEnum):
    """Events emitted by the scan pipeline to report progress."""

    SCANNING = "SCANNING"
    AWAITING_FLIP = "AWAITING_FLIP"
    SCANNING_REVERSE = "SCANNING_REVERSE"
    ASSEMBLING = "ASSEMBLING"
    UPLOADING = "UPLOADING"
    DONE = "DONE"

    @property
    def job_state(self) -> JobState | None:
        """
        Return the persisted job state this event implies, if any.

        ``None`` means this event changes no persisted state.  Today that is
        exactly ``SCANNING_REVERSE``: the second pass of a manual-duplex scan
        is reported to the operator as progress prose, but the job stays in
        whichever state it was already in because there is no ``JobState``
        twin for it.  A future member gains one; until then the seam is typed
        and visible here rather than hidden in a caller's ``if``/``elif``.

        Note that a non-``None`` result is not an instruction to write that
        state: ``DONE`` is terminal and the worker writes it only after the
        pipeline has returned.  Callers decide which states they apply.

        Returns:
            The matching JobState, or None when the event persists nothing.

        Raises:
            AssertionError: If the value is not a PipelineEvent member.

        """
        match self:
            case PipelineEvent.SCANNING:
                state = JobState.SCANNING
            case PipelineEvent.AWAITING_FLIP:
                state = JobState.AWAITING_FLIP
            case PipelineEvent.SCANNING_REVERSE:
                # No JobState twin: progress prose only, nothing persisted.
                state = None
            case PipelineEvent.ASSEMBLING:
                state = JobState.ASSEMBLING
            case PipelineEvent.UPLOADING:
                state = JobState.UPLOADING
            case PipelineEvent.DONE:
                state = JobState.DONE
            case _:
                assert_never(self)
        return state


logger = logging.getLogger(__name__)

FAILED_DIR_WARN_THRESHOLD = 20
"""
How many preserved PDFs make ``<data_dir>/failed/`` worth mentioning in the log.

This is an *attention* threshold, not a retention policy. Reaching it changes
nothing except that a WARNING is emitted: saneless never deletes, moves,
truncates or rotates a file it preserved, because the whole point of preserving
one was that it is the only remaining copy of a scanned document.
"""


@dataclass
class PipelineRequest:
    """
    Parameters for a scan pipeline run.

    ``job_id`` names the assembled PDF, via
    :func:`saneless.pdf.build_pdf_filename`, and is what makes two scans of the
    same title two distinct files rather than one overwriting the other. The
    worker always supplies ``job.id``; the empty default exists only so the
    dozens of tests that construct a request from a profile name and a title
    need not invent one, and an empty value simply drops the segment.

    It is defaulted rather than required, and sits with the other defaulted
    fields: moving it into the non-default block above would reorder the
    dataclass and break positional construction.
    """

    profile_name: str
    title: str
    job_id: str = ""
    tags: list[int] | None = None
    correspondent: int | None = None
    status_callback: Callable[[PipelineEvent], None] | None = None
    thumbnail_callback: Callable[[str], None] | None = None
    flip_event: threading.Event | None = None
    abort_event: threading.Event | None = None


@dataclass
class ScanResult:
    """How a scan pipeline run resolved, and how many pages it moved."""

    outcome: ScanOutcome
    pages_scanned: int
    pages_removed: int
    pages_uploaded: int
    warning: str | None = None


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


def _warn_if_failed_dir_growing(failed_dir: Path) -> None:
    """
    Log one WARNING when preserved scans have piled up in ``failed_dir``.

    Warn only. Nothing in saneless prunes, sweeps, caps, rotates or deletes
    anything in that directory: every file in it is a document that reached
    paper and never reached paperless-ngx, and automatically deleting one
    would be precisely the data loss the preservation guard exists to prevent.
    The control here is operator visibility, not enforcement (T-23-29). A
    retention policy would need a config key and a user story that do not
    exist yet.

    **This function must never raise.** It is called from inside the
    preservation guard's exception handler, while a delivery exception is
    already in flight; a raise here would replace the real failure with a
    bookkeeping error and lose the message OUTC-04 requires the job to carry.
    Any filesystem trouble -- a permission change, a race, the directory
    disappearing underneath us -- ends the check silently instead.

    Args:
        failed_dir: The directory preserved PDFs are moved into. It has just
            been written to, so the newly preserved file is included in the
            count.

    """
    try:
        preserved = list(failed_dir.glob("*.pdf"))
        if len(preserved) < FAILED_DIR_WARN_THRESHOLD:
            return
        total_bytes = sum(pdf.stat().st_size for pdf in preserved)
    except OSError:
        # Deliberately silent, per the docstring: a bookkeeping failure must
        # not displace the delivery failure the guard is about to re-raise.
        return
    logger.warning(
        "%d preserved scans (%.1f MiB) have accumulated in %s -- saneless "
        "never deletes these files itself, so draining the directory is yours "
        "to do once those documents are safely in paperless-ngx",
        len(preserved),
        total_bytes / (1024 * 1024),
        failed_dir,
    )


@contextlib.contextmanager
def _preserving(pdf_paths: Sequence[Path], failed_dir: Path) -> Iterator[None]:
    """
    Move assembled PDFs out of harm's way when delivery fails, then re-raise.

    ``run_pipeline`` does its work inside a ``TemporaryDirectory``, so any
    exception raised after assembly unwinds that directory and deletes the
    finished scan. This guard opens before the upload and closes after the
    poll, still *inside* that directory, and relocates every PDF it was given
    to ``failed_dir`` before letting the exception continue. A guard opened
    outside the temporary directory would run after the scan was already gone,
    which is the trap this whole phase is named after.

    **The caught type is ``Exception``, deliberately.** The guard is narrow in
    *span* -- it covers the upload and the poll and nothing else -- and broad
    in *type*. Inside that window a bug in our own code is precisely when the
    scan most needs keeping, and nothing is masked, because the exception is
    always re-raised with the original chained on ``__cause__``.
    ``KeyboardInterrupt`` and ``SystemExit`` derive from ``BaseException`` and
    pass through untouched. D-06's 2026-09-11 amendment settled this against
    the narrower ``PaperlessError`` alternative; narrowing it later would be a
    change of behaviour, not a tidy-up, so please do not relitigate it here.

    The relocation goes through ``shutil`` rather than a bare rename:
    ``data_dir`` and ``tmp_dir`` are independent settings and may sit on
    different filesystems, where ``Path.rename`` raises ``EXDEV``, while
    ``shutil`` falls back to a copy plus a drop of the source. The destination is always a full
    explicit path, never the bare directory -- handed a directory, ``shutil``
    raises ``shutil.Error`` on a basename collision, and ``shutil.Error`` does
    **not** inherit from ``OSError``, so it would escape the handler below and
    mask the delivery failure with a confusing traceback. The explicit-path
    form renames straight through; its silent-overwrite behaviour is a
    non-event because :func:`saneless.pdf.build_pdf_filename` keys every name
    on the job id.

    Args:
        pdf_paths: The assembled PDFs to rescue, in the order they should be
            reported. Paths that no longer exist are skipped, and if none of
            them survive the original exception is re-raised untouched.
        failed_dir: The durable directory to move them into. It is created
            here rather than only at startup, because ``data_dir`` may have
            been removed since it was validated.

    Yields:
        Nothing. The block it wraps is the delivery attempt itself.

    Raises:
        PaperlessError: When the move itself fails -- naming both the delivery
            failure and the preservation failure, because a user told only
            that the upload failed while the scan was also destroyed has been
            actively misinformed -- and when a non-saneless exception escaped
            the delivery window, since rebuilding an arbitrary third-party
            exception from a single string is not safe.
        SanelessError: Otherwise the original exception's own type, re-raised
            with the destination path appended to its message so the job error
            names the file the operator has to go and find.

    """
    try:
        yield
    except Exception as exc:
        destinations: list[Path] = []
        try:
            failed_dir.mkdir(parents=True, exist_ok=True)
            for pdf_path in pdf_paths:
                if not pdf_path.exists():
                    continue
                destination = failed_dir / pdf_path.name
                shutil.move(pdf_path, destination)
                destinations.append(destination)
                _warn_if_failed_dir_growing(failed_dir)
        except OSError as move_exc:
            msg = f"{exc}. The scan could NOT be preserved to {failed_dir}: {move_exc}"
            raise PaperlessError(msg) from exc
        if not destinations:
            raise
        preserved = ", ".join(str(destination) for destination in destinations)
        msg = f"{exc}. The scan was preserved at {preserved}"
        if isinstance(exc, SanelessError):
            raise type(exc)(msg) from exc
        raise PaperlessError(msg) from exc


def _drop_empty_pages(
    images: list[Image.Image],
    profile: ProfileConfig,
) -> list[Image.Image]:
    """
    Drop blank pages when the profile enables empty-page detection.

    Args:
        images: The scanned pages, in order.
        profile: The profile whose toggle and thresholds apply.

    Returns:
        The pages to assemble: filtered when detection is on, the input
        list unchanged when it is off.

    Raises:
        ScanError: If every page was detected as empty.

    """
    if not profile.enable_empty_page_detection:
        logger.info("Empty page detection disabled for profile")
        return images

    filtered = filter_empty_pages(
        images,
        mean_threshold=profile.empty_page_mean_threshold,
        stddev_threshold=profile.empty_page_stddev_threshold,
    )
    if len(filtered) < len(images):
        logger.info("Empty page filter: %d -> %d pages", len(images), len(filtered))
    if not filtered:
        msg = "All pages were detected as empty"
        raise ScanError(msg)
    return filtered


def _consume_dir_warning(destination: Path | None) -> str:
    """
    Describe what a consume-directory delivery cost the document.

    OUTC-02 asks a fallback to be recorded "in the FALLBACK state with a
    warning", and the two halves carry different information: the state says
    the document took the other route, and the warning says what that route
    did not do.  ``docs/explanation/consume-directory-fallback.md`` documents
    the same consequence -- paperless-ngx applies its own matching rules to a
    file it finds in the consume directory, so the title, tags and
    correspondent chosen for this scan are not applied to it.

    Nothing was lost and no rescan is needed, so the register is deliberately
    a warning rather than an error: the web UI renders it amber beside
    "Saved to folder", not red beside "Failed".

    Args:
        destination: Where the PDF was written.
            ``UploadResult.__post_init__`` guarantees this for every delivery
            that did not reach the API; the ``None`` arm exists only because
            the field is typed optional, and a warning that reaches the user
            must never be empty or read "None".

    Returns:
        The warning text recorded on the job and rendered in the status area.

    """
    where = f" at {destination}" if destination is not None else ""
    return (
        f"Saved to the paperless-ngx consume directory{where} instead of "
        "uploading through the API, so the title, tags and correspondent "
        "chosen for this scan were not applied -- paperless-ngx will apply "
        "its own matching rules to the file instead."
    )


def _is_manual_duplex(source: str) -> bool:
    """Check if the source string indicates manual duplex scanning."""
    return "manual" in source.lower() and "duplex" in source.lower()


@dataclass(frozen=True)
class _DeliveryContext:
    """
    The settings-derived values the duplex-mismatch recovery needs.

    Bundled into one record rather than passed as three more parameters
    because ``_handle_duplex_mismatch`` already sits exactly on ruff's
    ``PLR0913`` argument limit, and CLAUDE.md forbids both raising the limit
    and suppressing the rule. A three-field frozen record still names every
    dependency -- which threading the whole ``Settings`` object in would not --
    and stays cheap to construct in a test.

    Attributes:
        dpi: Scan resolution, from ``profile.resolution``. The PDF's declared
            page size depends on it.
        failed_dir: Where the preservation guard moves partial PDFs when
            delivery fails.
        task_timeout: Seconds to wait for each paperless-ngx consume task.

    """

    dpi: int
    failed_dir: Path
    task_timeout: float


def _handle_duplex_mismatch(
    passes: tuple[list[Image.Image], list[Image.Image]],
    tmp_path: Path,
    paperless: PaperlessClient,
    request: PipelineRequest,
    delivery: _DeliveryContext,
) -> tuple[str, bool]:
    """
    Save and upload partial PDFs when duplex page counts mismatch.

    Instead of discarding scanned data, assembles fronts and backs into
    separate PDFs and uploads both to paperless-ngx for manual review.

    The two halves get visibly distinct names, both carrying the job id. That
    matters beyond tidiness: the two partial PDFs are later covered by a single
    preservation guard, and two files sharing a name would overwrite each other
    on the way into ``failed/`` -- losing exactly the half this whole recovery
    path exists to save.

    Args:
        passes: Tuple of (front-side images, back-side images).
        tmp_path: Temporary directory for PDF assembly.
        paperless: Paperless-ngx client for upload.
        request: Pipeline request with title, tags, correspondent, job id and
            status callback.
        delivery: The DPI, the preservation directory and the task timeout.
            Bundled because this function has no profile and no settings in
            scope, and because three more parameters would break PLR0913.

    Returns:
        A (warning, delivered_to_api) pair. The warning describes the mismatch
        and recovery action; delivered_to_api is True only when BOTH partial
        PDFs reached the paperless-ngx API. If either fell back to the consume
        directory the caller must report the run as a fallback, not a success.

        Both halves that reach the API are polled, and a paperless-ngx failure
        on either one raises rather than returning -- a half that failed
        consumption is not a half that was delivered, and this path is already
        an anomaly, which makes it the one most likely to be holding a document
        the user actually needs (D-08).

    Raises:
        PaperlessError: If either upload or either poll fails. Both partial
            PDFs are moved to ``delivery.failed_dir`` first, and the message
            names them.

    """
    fronts, backs = passes
    # Derived rather than passed: it is exactly what the caller would hand us,
    # and the caller is already handing us the request it comes from.
    notify = request.status_callback or _noop_callback
    notify(PipelineEvent.ASSEMBLING)
    fronts_pdf = assemble_pdf(
        fronts,
        tmp_path / "fronts",
        filename=build_pdf_filename(request.job_id, f"{request.title} (fronts)"),
        dpi=delivery.dpi,
    )
    backs_pdf = assemble_pdf(
        backs,
        tmp_path / "backs",
        filename=build_pdf_filename(request.job_id, f"{request.title} (backs)"),
        dpi=delivery.dpi,
    )
    logger.info(
        "Duplex mismatch: assembled %d fronts and %d backs as separate PDFs",
        len(fronts),
        len(backs),
    )

    notify(PipelineEvent.UPLOADING)
    created = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    title = request.title
    # One guard over both halves: they are a single document between them, so
    # a failure on either one has to keep both (D-08).
    with _preserving([fronts_pdf, backs_pdf], delivery.failed_dir):
        fronts_result = paperless.upload_document(
            fronts_pdf,
            f"{title} (fronts)",
            request.tags,
            request.correspondent,
            created,
        )
        backs_result = paperless.upload_document(
            backs_pdf,
            f"{title} (backs)",
            request.tags,
            request.correspondent,
            created,
        )

        # UploadResult.__post_init__ guarantees a task_uuid iff the document
        # reached the API, so each test is exactly `delivered_to_api` and
        # additionally narrows the id to str.  A half that only reached the
        # consume directory has no task to poll.
        fronts_task = fronts_result.task_uuid
        if fronts_task is not None:
            paperless.poll_task(fronts_task, timeout=delivery.task_timeout)
        backs_task = backs_result.task_uuid
        if backs_task is not None:
            paperless.poll_task(backs_task, timeout=delivery.task_timeout)

    delivered = fronts_result.delivered_to_api and backs_result.delivered_to_api

    warning = (
        f"Page count mismatch: {len(fronts)} fronts, {len(backs)} backs. "
        f"Partial PDFs saved."
    )
    logger.warning(warning)
    return warning, delivered


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
) -> ScanResult:
    """
    Run the full scan-to-upload pipeline.

    Scans pages from the configured device, assembles them into a PDF,
    uploads to paperless-ngx, and polls for task completion. All
    temporary files are cleaned up automatically via TemporaryDirectory.

    The one thing that deliberately escapes that cleanup is the assembled PDF
    when delivery fails: the upload and the poll run inside a preservation
    guard that relocates the finished scan to ``settings.output.failed_dir``
    and names the destination in the exception it re-raises.

    Args:
        scanner: Scanner backend instance.
        paperless: Paperless-ngx API client.
        settings: Application settings.
        request: Pipeline request parameters.

    Returns:
        A ScanResult naming how the run resolved and how many pages were
        scanned, dropped as empty, and uploaded.

    Raises:
        ConfigError: If the profile or device is not configured.
        ScanError: If scanning fails.
        PaperlessError: If upload or polling fails. The message names where
            the assembled PDF was preserved, or -- if preservation failed
            too -- reports both failures.

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
        paper_size=profile.paper_size,
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
                warning, delivered = _handle_duplex_mismatch(
                    duplex_result,
                    tmp_path,
                    paperless,
                    request,
                    _DeliveryContext(
                        dpi=profile.resolution,
                        failed_dir=settings.output.failed_dir,
                        task_timeout=settings.output.paperless_task_timeout,
                    ),
                )
                notify(PipelineEvent.DONE)
                logger.info(
                    "Pipeline complete for '%s' (duplex mismatch recovery)",
                    request.title,
                )
                fronts, backs = duplex_result
                mismatch_pages = len(fronts) + len(backs)
                return ScanResult(
                    outcome=(
                        ScanOutcome.SUCCESS if delivered else ScanOutcome.FALLBACK
                    ),
                    pages_scanned=mismatch_pages,
                    pages_removed=0,
                    pages_uploaded=mismatch_pages,
                    warning=warning,
                )
            images = duplex_result
        else:
            images = _scan_simplex(scanner, device_id, scan_settings, request)

        # Step 1.5: Strip EXIF from all images (Pitfall #5)
        for img in images:
            img.info.pop("exif", None)

        # Step 2: Filter empty pages (gated on profile toggle, per D-17)
        filtered = _drop_empty_pages(images, profile)

        # Step 3: Assemble PDF
        notify(PipelineEvent.ASSEMBLING)
        # profile.resolution is the authoritative DPI: the same value
        # crop_to_paper_size uses for its crop arithmetic, so the cropped
        # shape and the declared page size cannot disagree.
        pdf_path = assemble_pdf(
            filtered,
            tmp_path,
            filename=build_pdf_filename(request.job_id, request.title),
            dpi=profile.resolution,
        )
        logger.info("PDF assembled: %s", pdf_path)

        # Step 4: Upload and poll, both inside the preservation guard.  The
        # guard opens before the upload and closes after the poll, and it sits
        # INSIDE the TemporaryDirectory: a guard placed outside would run after
        # the directory -- and the finished scan with it -- were already gone.
        notify(PipelineEvent.UPLOADING)
        created = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        with _preserving([pdf_path], settings.output.failed_dir):
            upload_result = paperless.upload_document(
                pdf_path,
                request.title,
                request.tags,
                request.correspondent,
                created,
            )

            # Step 5: Poll for result.  UploadResult.__post_init__ guarantees a
            # task_uuid iff the document reached the API, so this test is
            # exactly `delivered_to_api` and additionally narrows the id to str.
            task_uuid = upload_result.task_uuid
            if task_uuid is not None:
                # The return value is discarded on purpose, and that is now
                # correct rather than a bug: since plan 23-04 a successful poll
                # means "it returned" and a failed one means "it raised".  Do
                # not re-add a status check on the result.
                paperless.poll_task(
                    task_uuid,
                    timeout=settings.output.paperless_task_timeout,
                )
                outcome = ScanOutcome.SUCCESS
                warning = None
            else:
                outcome = ScanOutcome.FALLBACK
                # A state alone would leave the user to work out for
                # themselves why the title and tags they chose never appeared
                # in paperless-ngx (OUTC-02).
                warning = _consume_dir_warning(upload_result.consume_dir_path)

        result = ScanResult(
            outcome=outcome,
            pages_scanned=len(images),
            pages_removed=len(images) - len(filtered),
            pages_uploaded=len(filtered),
            warning=warning,
        )

        notify(PipelineEvent.DONE)
        logger.info("Pipeline complete for '%s'", request.title)

    return result
