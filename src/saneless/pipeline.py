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
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Final, assert_never

from saneless.exceptions import (
    ConfigError,
    PaperlessError,
    SanelessError,
    ScanCancelledError,
    ScanError,
    describe,
)
from saneless.pages import filter_empty_pages
from saneless.pdf import assemble_pdf, build_pdf_filename
from saneless.scanner.base import ScanBatch, ScanSettings
from saneless.spool import SpooledPageSink
from saneless.vocabulary import FlipOutcome, JobState, ScanOutcome

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from saneless.config import ProfileConfig, Settings
    from saneless.paperless import PaperlessClient
    from saneless.scanner.base import PageRecord, ScannerBackend

__all__ = [
    "FlipAnswerSlot",
    "FlipCoordinator",
    "PipelineEvent",
    "PipelineRequest",
    "ScanResult",
    "run_pipeline",
]


class PipelineEvent(StrEnum):
    """Events emitted by the scan pipeline to report progress."""

    SCANNING = "SCANNING"
    AWAITING_FLIP = "AWAITING_FLIP"
    SCANNING_REVERSE = "SCANNING_REVERSE"
    ASSEMBLING = "ASSEMBLING"
    UPLOADING = "UPLOADING"
    DONE = "DONE"

    @property
    def job_state(self) -> JobState:
        """
        Return the job state this event implies.

        The projection is total: every event has a ``JobState`` twin.
        ``SCANNING_REVERSE`` projects to ``JobState.SCANNING_REVERSE``, so the
        second pass of a manual-duplex scan is persisted as its own busy state
        and the job leaves ``AWAITING_FLIP`` the moment pass B starts -- which
        is what takes the flip prompt, and its Continue and Abort controls, off
        the screen while the backs feed (DPLX-06).

        Note that the returned state is not an instruction to write it:
        ``DONE`` is terminal and the worker writes it only after the
        pipeline has returned.  Callers decide which states they apply.

        Returns:
            The matching JobState.

        Raises:
            AssertionError: If the value is not a PipelineEvent member.

        """
        match self:
            case PipelineEvent.SCANNING:
                state = JobState.SCANNING
            case PipelineEvent.AWAITING_FLIP:
                state = JobState.AWAITING_FLIP
            case PipelineEvent.SCANNING_REVERSE:
                state = JobState.SCANNING_REVERSE
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

# The spool's subdirectory inside the job workspace, and the per-pass prefix
# each acquisition pass's file names carry: ``a-0001.png`` for a simplex job or
# a duplex job's fronts, ``b-0001.png`` for its backs (D-02).  The labels are
# for telling two passes apart in one directory; document order comes from the
# record list and never from these names.
#
# Named constants rather than literals at the call sites, for a lint reason
# worth recording so nobody "tidies" them back: ruff's S106 reads any keyword
# argument whose name contains "pass" as a possible hardcoded password, and
# ``pass_label=`` does, so a string literal there fails the lint and this
# project adds no suppressions. The constants are spelled ``_SPOOL_LABEL_*``
# and not ``_PASS_*_LABEL`` for the sibling rule S105, which reads the same
# substring in a variable's own name. Naming them also gives anything that
# asserts on the convention one place to import it from.
_SPOOL_DIR_NAME: Final = "spool"
_SPOOL_LABEL_A: Final = "a"
_SPOOL_LABEL_B: Final = "b"

FAILED_DIR_WARN_THRESHOLD = 20
"""
How many preserved PDFs make ``<data_dir>/failed/`` worth mentioning in the log.

This is an *attention* threshold, not a retention policy. Reaching it changes
nothing except that a WARNING is emitted: saneless never deletes, moves,
truncates or rotates a file it preserved, because the whole point of preserving
one was that it is the only remaining copy of a scanned document.
"""


class FlipCoordinator(ABC):
    """
    The one way a manual-duplex run waits for the operator to flip the stack.

    Pass A has fed the fronts; before pass B the pipeline asks this seam a
    single question -- has the stack been turned? -- and gets back a single,
    total ``FlipOutcome``.  The web worker answers it from the Continue and
    Abort routes, and the CLI answers it from a terminal prompt.

    This is an ``ABC`` and not a ``typing.Protocol`` on purpose, and the rule is
    observable in the tree: ``Protocol`` describes shapes this project does not
    own (``SaneDevice`` for python-sane's handle, ``_SettingsFactory`` for
    pydantic's constructor), while ``ABC`` defines seams the project implements
    itself (``ScannerBackend``).  DPLX-04's lowercase "protocol" means
    "contract", not ``typing.Protocol``.
    """

    @abstractmethod
    def wait_for_flip(self, timeout: float) -> FlipOutcome:
        """
        Block until the flip wait resolves, for at most ``timeout`` seconds.

        An implementation answers once, and its answer is final: whichever of
        Continue, Abort or the clock resolves the wait first is what this
        returns, and a signal arriving after that is dropped (D-16).

        Args:
            timeout: The longest the wait may hold the calling thread, in
                seconds.  ``0`` is a valid bound and returns at once.

        Returns:
            ``CONTINUED`` when the operator flipped the stack, ``ABORTED`` when
            they gave up at the prompt, or ``TIMED_OUT`` when neither happened
            within ``timeout``.

        """

    @property
    def abort_cause(self) -> Exception | None:
        """
        Why the wait answered ``ABORTED``, when it was not the operator's choice.

        An ``ABORTED`` answer usually means someone gave up at the prompt, and
        the pipeline reports that as a cancellation.  But a coordinator can
        also answer ``ABORTED`` because its prompt broke -- a read error such as
        an I/O error or undecodable input (WR-08) -- and nobody chose to stop.
        End of input, a closed terminal included, is not such a break: it is
        the operator's cancel (D-02).  Such a
        coordinator returns the exception here, so the pipeline records a
        failure rather than a cancellation without a fourth ``FlipOutcome``
        member (Phase 25 D-09, D-02).

        Concrete rather than abstract, so a coordinator whose aborts are always
        an operator's needs no change.

        Returns:
            The exception that forced the abort, or ``None`` when there was
            none -- including whenever the answer was not ``ABORTED``.

        """
        return None


class FlipAnswerSlot:
    """
    One flip answer, claimed once, and final: the claim both coordinators share.

    The web worker's and the CLI's coordinators differ in where an answer comes
    from -- the Continue and Abort routes, or a terminal prompt -- but not in
    how it is claimed.  That claim lives here, once, so a fix to it reaches both
    (IN-02).  This is a concrete helper the coordinators compose, not a second
    seam: ``FlipCoordinator`` stays the only contract the pipeline waits on
    (D-09), and anything a coordinator adds on top -- the web one's arming, for
    instance -- stays in that coordinator.

    The answer is written under the lock *before* the event is set, so a waiter
    that wakes always finds an answer to read -- there is no window in which the
    event says "resolved" and the slot still says nothing.  That ordering is
    what makes it race-free by construction rather than by timing.
    """

    def __init__(self) -> None:
        """Start unanswered."""
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._outcome: FlipOutcome | None = None

    @property
    def answer(self) -> FlipOutcome | None:
        """The claimed answer, or ``None`` while the slot is unanswered."""
        with self._lock:
            return self._outcome

    def offer(self, outcome: FlipOutcome) -> bool:
        """
        Claim the answer with ``outcome`` if nothing has claimed it yet.

        An offer that loses leaves the slot and the event untouched (D-16).

        Args:
            outcome: The answer this caller is offering.

        Returns:
            Whether ``outcome`` became the answer.

        """
        with self._lock:
            if self._outcome is not None:
                return False
            self._outcome = outcome
        self._event.set()
        return True

    def settle(self, outcome: FlipOutcome) -> FlipOutcome:
        """
        Claim the answer with ``outcome`` unless one is claimed, and return it.

        This is the path that ends a wait: a timeout, or a CLI answer.
        Returning the answer in effect, rather than asserting one exists, is
        what narrows ``FlipOutcome | None`` to ``FlipOutcome`` without an
        ``assert`` -- which ``S101`` bans in ``src/``.

        Args:
            outcome: The answer this caller is offering.

        Returns:
            The claimed answer: ``outcome`` if it was first, otherwise the
            answer that beat it.

        """
        with self._lock:
            if self._outcome is None:
                self._outcome = outcome
            claimed = self._outcome
        self._event.set()
        return claimed

    def wait(self, timeout: float) -> None:
        """
        Block until the slot is answered, for at most ``timeout`` seconds.

        It reports nothing: the caller reads the result through ``settle``, which
        is right whether the wait was answered or expired.  A
        ``KeyboardInterrupt`` raised while waiting propagates to the caller.

        Args:
            timeout: The longest to wait, in seconds.

        """
        self._event.wait(timeout)


@dataclass(frozen=True)
class _FlipContext:
    """
    What ``_scan_manual_duplex`` needs to wait for a flip.

    Bundled into one record rather than passed as two parameters because
    ``_scan_manual_duplex`` already sits exactly on ruff's ``PLR0913``
    argument limit, and CLAUDE.md forbids both raising the limit and
    suppressing the rule -- the same reason ``_DeliveryContext`` exists.

    It also carries the coordinator as non-Optional.
    ``PipelineRequest.flip_coordinator`` is ``FlipCoordinator | None`` because a
    simplex run legitimately has none, so the narrowing to "there is one"
    happens exactly once, where this record is built, and the callee never
    needs an ``assert`` (which ``S101`` bans in ``src/``) to prove it.

    Attributes:
        coordinator: The seam that answers the flip wait.
        timeout: Seconds the wait may hold the pipeline, from
            ``output.flip_timeout_seconds``.

    """

    coordinator: FlipCoordinator
    timeout: float


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
    # One field, one atomic answer.  This replaced a flip event and an abort
    # event, where an Abort set both so the waiter woke and then had to inspect
    # the second to learn why -- the two-step M-02's dead Abort lived in.
    flip_coordinator: FlipCoordinator | None = None


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
        OSError: If the directory cannot be created or measured; the caller
            translates it (IN-07).

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


def _open_workspace(tmp_dir: str, min_free_mb: int) -> tempfile.TemporaryDirectory[str]:
    """
    Create this run's temporary workspace under ``tmp_dir``, checking for room.

    Each of the three steps can raise a raw ``OSError`` -- a full disk, or a
    ``tmp_dir`` removed since start-up.  That is the setup problem
    ``validate_settings_dirs`` reports at start-up, so it is a ``ConfigError``
    here too, not an UNKNOWN error the CLI would call a saneless bug (IN-07).
    Only the workspace's creation is guarded: an ``OSError`` from the scan run
    inside it keeps its own translation.

    Args:
        tmp_dir: The configured directory for temporary files.
        min_free_mb: Minimum free space required in megabytes.

    Returns:
        The created workspace, for the caller's ``with`` block to clean up.

    Raises:
        ConfigError: If ``tmp_dir`` cannot be created or measured, or the
            workspace cannot be created in it; names ``tmp_dir``.
        ScanError: If free space is below ``min_free_mb``.

    """
    try:
        Path(tmp_dir).mkdir(parents=True, exist_ok=True)
        _check_disk_space(tmp_dir, min_free_mb)
        return tempfile.TemporaryDirectory(dir=tmp_dir)
    except OSError as exc:
        msg = f"Could not prepare the working directory {tmp_dir}: {describe(exc)}"
        raise ConfigError(msg) from exc


def _require_pages(batch: ScanBatch) -> None:
    """
    Raise ScanError if a scanner pass came back with no pages at all.

    This is the pipeline's own contract check against any ``ScannerBackend``
    (EXC-03, N-06). Without it an empty batch was misreported as "all pages
    blank" with empty-page detection on, and leaked img2pdf's bare
    ``ValueError`` with detection off or on an empty manual-duplex half.

    It never pre-empts Phase 24 D-03's truthful feeder message: the SANE
    backend raises its own, more specific ``FeederEmptyError`` for an empty
    feeder, or an all-unreadable ``ScanError``, before it ever returns a
    batch. What it guarantees is that ``_drop_empty_pages`` and
    ``assemble_pdf`` never see an empty list.

    Args:
        batch: The batch one ``scan_pages`` call returned.

    Raises:
        ScanError: If the batch carries no pages.

    """
    if not batch.pages:
        msg = "No pages were scanned"
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
        failed_dir: The directory preserved PDFs are moved into. Every scan
            this guard preserved has already been moved in by the time this
            runs, so all of them are included in the count.

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
            # Once, after the loop, so the count reflects the finished state.
            # The duplex-mismatch recovery passes two PDFs under a single
            # guard, so calling this per file emitted the same "N preserved
            # scans have accumulated" WARNING twice with different counts --
            # log noise on the one path already flagged as an anomaly, and a
            # contradiction of this helper's own "one WARNING" docstring.
            if destinations:
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
    pages: Sequence[PageRecord],
    profile: ProfileConfig,
) -> list[PageRecord]:
    """
    Drop blank pages when the profile enables empty-page detection.

    Records in, records out. The judgement is made from the statistics each
    record already carries, measured once when the page was spooled (D-06);
    nothing here re-opens a page file, and nothing here deletes one.

    Args:
        pages: The scanned page records, in document order.
        profile: The profile whose toggle and thresholds apply.

    Returns:
        The records to assemble: filtered when detection is on, the input
        sequence's records unchanged when it is off.

    Raises:
        ScanError: If every page of a non-empty batch was detected as blank.
            The input is never empty: ``_require_pages`` has already refused
            an empty batch with ``No pages were scanned``.

    """
    if not profile.enable_empty_page_detection:
        logger.info("Empty page detection disabled for profile")
        return list(pages)

    filtered = filter_empty_pages(
        pages,
        mean_threshold=profile.empty_page_mean_threshold,
        stddev_threshold=profile.empty_page_stddev_threshold,
    )
    if len(filtered) < len(pages):
        logger.info("Empty page filter: %d -> %d pages", len(pages), len(filtered))
    if not filtered:
        msg = "All pages were blank"
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


@dataclass(frozen=True)
class _AcquisitionContext:
    """
    What one scanning pass needs to reach the device and spool what it gets.

    Bundled into one record rather than passed as four more parameters, for
    the reason ``_DeliveryContext`` already records: ``_scan_manual_duplex``
    sits exactly on ruff's ``PLR0913`` argument limit, and CLAUDE.md forbids
    both raising the limit and suppressing the rule. The fields also genuinely
    travel together -- all four are fixed for the whole job, and a pass whose
    device or spool directory differed from its sibling's would be a bug
    rather than a feature.

    Attributes:
        device_id: The SANE device every pass of this job scans through.
        settings: The scan settings every pass is run with.
        spool_dir: Where each pass's sink writes its pages. It lives inside
            the job's workspace, so the page files are deleted with it.
        min_free_space_mb: The reserve each per-page disk check keeps free for
            assembly -- the operator's own ``min_free_space_mb``, the same
            value the up-front check uses (D-07).

    """

    device_id: str
    settings: ScanSettings
    spool_dir: Path
    min_free_space_mb: int


@dataclass(frozen=True)
class _DuplexMismatch:
    """
    The two passes of a manual duplex run whose page counts disagreed.

    A named record rather than the bare ``(fronts, backs)`` tuple this used to
    be. The recovery path now also needs the resolution the device actually
    used and the sheets it could not read, and a four-element tuple would make
    every call site remember an order.

    Attributes:
        fronts: Page records produced by pass A, in pass order.
        backs: Page records produced by pass B, in pass order.
        dpi: The resolution the device reported actually using.
        pages_rejected: Sheets skipped across both passes for failing their
            integrity checks.

    """

    fronts: Sequence[PageRecord]
    backs: Sequence[PageRecord]
    dpi: int
    pages_rejected: int


def _duplex_resolution(front: ScanBatch, back: ScanBatch) -> int:
    """
    Reconcile the resolution the two manual-duplex passes reported.

    Both passes run with identical settings against one device, so the two
    values should be identical in practice. If they are not, the device changed
    its mind mid-job, and that is a fact worth saying out loud rather than
    resolving silently.

    Pass A's value wins. The choice is arbitrary between two equally plausible
    numbers, which is exactly why it is logged; failing the run instead would
    throw away a scan that completed, over a disagreement the crop fallback
    already tolerates.

    Args:
        front: The batch pass A produced.
        back: The batch pass B produced.

    Returns:
        The resolution to assemble the document at.

    """
    if front.actual_resolution != back.actual_resolution:
        logger.warning(
            "Manual duplex passes disagree on resolution: pass A reports %s dpi, "
            "pass B reports %s dpi; assembling at %s dpi",
            front.actual_resolution,
            back.actual_resolution,
            front.actual_resolution,
        )
    return front.actual_resolution


def _rejected_pages_warning(count: int) -> str | None:
    """
    Describe sheets the scanner could not read, and log them, if there were any.

    Worded so it cannot be mistaken for blank-page removal. The pipeline's blank
    count is empty-page detection, which Phase 30 renders to users as pages
    removed for being blank; a sheet that failed its integrity checks is a
    different event with a different remedy, and the two must not be conflated.

    This is the only channel the count has. ``pages_scanned`` is the length of
    the pages that arrived, which already excludes a skipped sheet, so a
    ten-sheet stack with one unreadable page reports nine and nobody learns a
    page was lost. It logs as it builds, following ``_handle_duplex_mismatch``,
    which likewise logs the warning it returns.

    Args:
        count: How many sheets the backend skipped.

    Returns:
        The warning text, or None when nothing was rejected.

    """
    if count <= 0:
        return None
    warning = (
        f"{count} page(s) could not be read by the scanner and were skipped. "
        f"They were not removed for being blank; rescan those sheets."
    )
    logger.warning(warning)
    return warning


def _join_warnings(*parts: str | None) -> str | None:
    """
    Combine into the single field that carries them everything a run has to say.

    ``ScanResult`` has one warning field and a run can have more than one thing
    to report: a consume-directory fallback and an unreadable sheet are
    independent events that can both happen. Letting either overwrite the other
    would be the kind of small silence this phase exists to remove.

    Args:
        parts: The candidate warnings, any of which may be None.

    Returns:
        The non-empty warnings joined by a space, or None if there were none.

    """
    present = [part for part in parts if part]
    return " ".join(present) if present else None


def _handle_duplex_mismatch(
    passes: tuple[Sequence[PageRecord], Sequence[PageRecord]],
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
        passes: Tuple of (front-side records, back-side records).
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


def _finish_duplex_mismatch(
    mismatch: _DuplexMismatch,
    tmp_path: Path,
    paperless: PaperlessClient,
    request: PipelineRequest,
    settings: Settings,
) -> ScanResult:
    """
    Deliver a mismatched manual duplex run and report how it resolved.

    This is ``run_pipeline``'s ``_DuplexMismatch`` arm. It is a separate
    function only because ``run_pipeline`` would otherwise go over ruff's
    ``PLR0915`` statement limit, and CLAUDE.md forbids suppressing that rule.

    Args:
        mismatch: Both passes, the resolution the device used, and the sheets
            it could not read.
        tmp_path: Temporary directory for PDF assembly.
        paperless: Paperless-ngx client for upload.
        request: Pipeline request with title, tags, correspondent, job id and
            status callback.
        settings: Application settings, for the preservation directory and the
            task timeout.

    Returns:
        SUCCESS when both partial PDFs reached the paperless-ngx API, otherwise
        FALLBACK, always carrying the mismatch warning.

    Raises:
        PaperlessError: If either upload or either poll fails.

    """
    warning, delivered = _handle_duplex_mismatch(
        (mismatch.fronts, mismatch.backs),
        tmp_path,
        paperless,
        request,
        _DeliveryContext(
            dpi=mismatch.dpi,
            failed_dir=settings.output.failed_dir,
            task_timeout=settings.output.paperless_task_timeout,
        ),
    )
    notify = request.status_callback or _noop_callback
    notify(PipelineEvent.DONE)
    logger.info("Pipeline complete for '%s' (duplex mismatch recovery)", request.title)
    mismatch_pages = len(mismatch.fronts) + len(mismatch.backs)
    # The mismatch path does not run _drop_empty_pages, on purpose (D-08), so
    # pages_removed is a hardcoded 0.  A mismatched run is an anomaly sent to a
    # person for manual review, and a blank back side is evidence about why
    # the two passes disagreed.  Removing it would destroy the information the
    # partial PDFs exist to provide.
    #
    # Filtering here was considered and rejected because it is dangerous:
    # _drop_empty_pages raises ScanError when every page is empty, so an
    # all-blank backs pass would fail the run and lose the fronts too.  That
    # turns a recoverable anomaly into exactly the data loss Phase 23 spent a
    # phase removing.  Because nothing is filtered, pages_removed=0 is simply
    # true.
    #
    # Per Phase 24 D-04, _MAX_ADF_PAGES applies to each scan_pages call, so to
    # each pass: each pass can feed up to that many sheets.
    return ScanResult(
        outcome=ScanOutcome.SUCCESS if delivered else ScanOutcome.FALLBACK,
        pages_scanned=mismatch_pages,
        pages_removed=0,
        pages_uploaded=mismatch_pages,
        warning=_join_warnings(
            warning,
            _rejected_pages_warning(mismatch.pages_rejected),
        ),
    )


def _interleave_duplex(
    fronts: Sequence[PageRecord],
    backs: Sequence[PageRecord],
) -> list[PageRecord]:
    """
    Interleave front and back pages for manual duplex.

    Backs are reversed because the user flips the stack face-down,
    so the last front's back is scanned first in pass B.

    Records are reordered, never files. Nothing is renamed, moved or rewritten
    on the spool: after this runs, the ``a-`` and ``b-`` file names no longer
    sort into document order at all, and that is precisely why document order
    is the order of this list and never the directory's (D-02, D-04).

    Args:
        fronts: Front-side records from pass A.
        backs: Back-side records from pass B (in scan order).

    Returns:
        Interleaved records: A1, B1, A2, B2, ...

    Raises:
        ScanError: If front and back page counts do not match.

    """
    if len(fronts) != len(backs):
        msg = f"Page count mismatch: {len(fronts)} fronts, {len(backs)} backs"
        raise ScanError(msg)
    backs_reversed = list(reversed(backs))
    result: list[PageRecord] = []
    for front, back in zip(fronts, backs_reversed, strict=True):
        result.append(front)
        result.append(back)
    return result


def _scan_manual_duplex(
    scanner: ScannerBackend,
    acquisition: _AcquisitionContext,
    request: PipelineRequest,
    flip: _FlipContext,
) -> ScanBatch | _DuplexMismatch:
    """
    Perform a two-pass manual duplex scan with flip coordination.

    Between the passes the run waits on ``flip.coordinator``, bounded by
    ``flip.timeout``, so a forgotten flip prompt fails this job instead of
    parking the single worker thread forever (M-07).

    Each pass gets its own sink, labelled ``a`` for the fronts and ``b`` for
    the backs, so the two passes spool into names that can be told apart while
    sharing one directory. That is for debuggability only: document order comes
    from the record list, never from those names (D-02). Only pass A's sink
    carries the thumbnail callback, so the strip shows the first front and
    fires exactly once per job (D-05).

    Args:
        scanner: Scanner backend instance.
        acquisition: The device, the scan settings and the spool this job's
            passes use.
        request: Pipeline request with the thumbnail and status callbacks.
        flip: The flip coordinator and the timeout bounding its wait.

    Returns:
        A ScanBatch of interleaved pages when the two passes agree on count,
        carrying the device's resolution and the rejections from both passes;
        or a _DuplexMismatch holding both passes when the counts disagree.

    Raises:
        ScanCancelledError: If the operator aborts at the flip prompt.  Raised
            before pass B starts.
        ScanError: ``No pages were scanned`` if pass A returns no pages, before
            anyone is asked to flip; ``No back pages were scanned in pass B``,
            naming pass A's count, if pass B returns none, before the count
            comparison. Otherwise, if the flip prompt itself failed, or if the
            flip wait times out.  Both raise before pass B starts.
        AssertionError: If the coordinator returns a value that is not a
            FlipOutcome member.

    """
    # Derived rather than passed: it is exactly what the caller would hand us,
    # and the caller is already handing us the request it comes from.
    notify = request.status_callback or _noop_callback

    # Pass A: scan fronts.  The thumbnail callback rides on this sink, which
    # fires it while pass A is still running rather than after it returns
    # (D-05) -- the page is in memory at that moment, and re-opening a 26 MB
    # page later to make a 300 px strip would be a second decode.
    front_sink = SpooledPageSink(
        directory=acquisition.spool_dir,
        pass_label=_SPOOL_LABEL_A,
        min_free_space_mb=acquisition.min_free_space_mb,
        thumbnail_callback=request.thumbnail_callback,
    )
    front_batch = scanner.scan_pages(
        acquisition.device_id, acquisition.settings, front_sink
    )
    # Before the flip prompt, so nobody is asked to flip nothing (EXC-03).
    _require_pages(front_batch)
    front_pages = front_batch.pages
    logger.info("Pass A: scanned %d front page(s)", len(front_pages))

    notify(PipelineEvent.AWAITING_FLIP)
    outcome = flip.coordinator.wait_for_flip(flip.timeout)
    # A match with assert_never rather than an if-chain: a fourth FlipOutcome
    # member then fails ty and pyrefly at edit time instead of falling through
    # into pass B.  An explicit abort -- web Abort, n, Ctrl-D, Ctrl-C -- is a
    # cancellation, not a scanner failure (EXC-04, N-08).  A broken prompt
    # (an ABORTED that carries an abort_cause) and a timeout are failures,
    # because nobody chose to stop (D-02).  A shutdown-claimed abort also
    # arrives here as ScanCancelledError; the worker records it as a restart
    # by checking aborted_by_shutdown before anything else (WR-06).
    match outcome:
        case FlipOutcome.CONTINUED:
            pass
        case FlipOutcome.ABORTED:
            cause = flip.coordinator.abort_cause
            if cause is not None:
                msg = f"Flip prompt failed: {describe(cause)}"
                raise ScanError(msg) from cause
            msg = "Manual duplex scan cancelled at the flip prompt"
            raise ScanCancelledError(msg)
        case FlipOutcome.TIMED_OUT:
            msg = (
                f"Manual duplex flip wait timed out after {flip.timeout:g} "
                "seconds: nobody confirmed the stack was flipped"
            )
            raise ScanError(msg)
        case _:
            assert_never(outcome)

    # Pass B: scan backs.  No thumbnail callback on this sink: pass A already
    # fired it, and the strip is meant to show the first front.
    notify(PipelineEvent.SCANNING_REVERSE)
    back_sink = SpooledPageSink(
        directory=acquisition.spool_dir,
        pass_label=_SPOOL_LABEL_B,
        min_free_space_mb=acquisition.min_free_space_mb,
    )
    back_batch = scanner.scan_pages(
        acquisition.device_id, acquisition.settings, back_sink
    )
    # Before the count comparison, so no half is assembled from an empty list.
    # Not _require_pages: "No pages were scanned" is false once pass A fed the
    # fronts, so the message names the pass and what pass A scanned (IN-01).
    # The fronts are still lost here; keeping them needs Phase 29's spooling.
    if not back_batch.pages:
        msg = (
            "No back pages were scanned in pass B "
            f"(pass A scanned {len(front_pages)} front page(s))"
        )
        raise ScanError(msg)
    back_pages = back_batch.pages
    logger.info("Pass B: scanned %d back page(s)", len(back_pages))

    dpi = _duplex_resolution(front_batch, back_batch)
    # Summed, not picked: a sheet lost on either pass is a sheet lost.
    rejected = front_batch.pages_rejected + back_batch.pages_rejected

    # Raw count validation BEFORE empty page detection (SCAN-07)
    if len(front_pages) != len(back_pages):
        return _DuplexMismatch(
            fronts=front_pages,
            backs=back_pages,
            dpi=dpi,
            pages_rejected=rejected,
        )

    interleaved = _interleave_duplex(front_pages, back_pages)
    logger.info("Interleaved %d total pages", len(interleaved))
    return ScanBatch(
        pages=tuple(interleaved), actual_resolution=dpi, pages_rejected=rejected
    )


def _flip_context(request: PipelineRequest, settings: Settings) -> _FlipContext:
    """
    Build the flip context for a manual-duplex run, refusing one with no coordinator.

    Called only by ``run_pipeline``, only for a ``duplex = "manual"`` profile, and
    before any scanner contact -- see the comment at the call site for why that
    placement matters.

    Args:
        request: The pipeline request, which must carry a flip coordinator.
        settings: Application settings, for ``output.flip_timeout_seconds``.

    Returns:
        The coordinator, narrowed to non-Optional, with the configured timeout.

    Raises:
        ConfigError: If the request carries no flip coordinator.

    """
    if request.flip_coordinator is None:
        # Refusing is the only safe answer: with no coordinator, pass B would
        # start the instant pass A ends and re-feed an empty tray (C-02).
        # Starting anyway would turn a bad request into lost pages.
        msg = (
            f"Profile '{request.profile_name}' is manual duplex, which needs a "
            "flip coordinator to tell saneless when the stack has been turned "
            "over, and none was supplied"
        )
        raise ConfigError(msg)
    coordinator = request.flip_coordinator
    return _FlipContext(
        coordinator=coordinator,
        timeout=settings.output.flip_timeout_seconds,
    )


def _scan_simplex(
    scanner: ScannerBackend,
    acquisition: _AcquisitionContext,
    request: PipelineRequest,
) -> ScanBatch:
    """
    Perform a simplex / hardware duplex / flatbed scan.

    The one pass spools under the ``a`` label, the same label a manual-duplex
    job's fronts use, so a spool directory reads the same way whichever route
    produced it.

    The thumbnail is not generated here. It rides on the sink and fires while
    the first page is being spooled, which is both cheaper -- the page is in
    memory at that moment, rather than needing a second decode afterwards --
    and visibly earlier, since the strip appears during acquisition instead of
    after the last sheet (D-05).

    Args:
        scanner: Scanner backend instance.
        acquisition: The device, the scan settings and the spool this job's
            pass uses.
        request: Pipeline request with optional thumbnail callback.

    Returns:
        The batch the device produced: its page records, the resolution it
        actually used, and how many fed sheets it could not read.

    Raises:
        ScanError: ``No pages were scanned`` if the backend returned no pages.

    """
    sink = SpooledPageSink(
        directory=acquisition.spool_dir,
        pass_label=_SPOOL_LABEL_A,
        min_free_space_mb=acquisition.min_free_space_mb,
        thumbnail_callback=request.thumbnail_callback,
    )
    batch = scanner.scan_pages(acquisition.device_id, acquisition.settings, sink)
    _require_pages(batch)
    logger.info("Scanned %d page(s)", len(batch.pages))
    return batch


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
        ConfigError: If the profile or device is not configured, a manual
            duplex profile is run with no flip coordinator, or the working
            directory under ``tmp_dir`` cannot be created or measured.
        ScanCancelledError: If the operator aborts a manual duplex scan at the
            flip prompt.
        ScanError: If scanning fails; ``No pages were scanned`` if a scan pass
            returned no pages; ``All pages were blank`` if empty-page detection
            removed every page; or if a manual duplex flip prompt fails or its
            wait times out.
        PaperlessError: If upload or polling fails. The message names where
            the assembled PDF was preserved, or -- if preservation failed
            too -- reports both failures.

    """
    notify = request.status_callback or _noop_callback

    if request.profile_name not in settings.profiles:
        msg = f"Unknown profile: {request.profile_name}"
        raise ConfigError(msg)

    profile = settings.profiles[request.profile_name]

    # The one place saneless decides a scan is manual duplex, and it reads
    # profile.duplex -- never source, which is a pure SANE value (DPLX-03).
    #
    # _flip_context refuses a manual-duplex request that has no flip
    # coordinator, and where it is called matters: it must come before
    # _resolve_device, which calls get_devices() when scanner.device is empty,
    # so a request that cannot run never touches the device.  Refusing inside
    # _scan_manual_duplex would be too late -- that function scans pass A
    # first, so it would use up a full feeder pass before failing.
    manual_duplex = profile.duplex == "manual"
    flip = _flip_context(request, settings) if manual_duplex else None

    device_id = _resolve_device(scanner, settings)

    scan_settings = ScanSettings(
        source=profile.source,
        resolution=profile.resolution,
        mode=profile.mode,
        auto_source_mode=profile.auto_source_mode,
        # The single conversion point from the config Literal to the scanner's
        # feeder-resolution flag (D-02). Do not add a second: the scanner
        # package never sees ProfileConfig.duplex or the job vocabulary.
        resolve_feeder_source=manual_duplex,
        paper_size=profile.paper_size,
    )

    workspace = _open_workspace(
        settings.output.tmp_dir, settings.output.min_free_space_mb
    )

    with workspace as tmp_dir:
        tmp_path = Path(tmp_dir)

        # The spool sits INSIDE the workspace, so every page file is removed
        # with it when this block exits.  That is the same trap _preserving's
        # docstring names: anything that has to outlive the job -- a preserved
        # partial scan -- must be moved out before then, and a guard placed
        # outside this block would run when the pages were already gone.
        #
        # A subdirectory rather than tmp_path itself, so the assembled PDF and
        # the duplex-mismatch halves are not written among the pages.
        spool_dir = tmp_path / _SPOOL_DIR_NAME
        spool_dir.mkdir()

        acquisition = _AcquisitionContext(
            device_id=device_id,
            settings=scan_settings,
            spool_dir=spool_dir,
            min_free_space_mb=settings.output.min_free_space_mb,
        )

        # Step 1: Scan
        notify(PipelineEvent.SCANNING)
        logger.info(
            "Scanning with profile '%s' on device '%s'",
            request.profile_name,
            device_id,
        )

        if flip is None:
            batch = _scan_simplex(scanner, acquisition, request)
        else:
            duplex_result = _scan_manual_duplex(scanner, acquisition, request, flip)
            # A match with assert_never, not a dict or an isinstance chain, on
            # purpose: a variant missing from a dict draws no diagnostic from
            # either ty or pyrefly, while the same omission in a match is
            # caught by both, at edit time, before a third result type can fall
            # silently through an else.
            #
            # This dispatch is what closes N-07.  N-07's other half -- "replace
            # the tuple with a result dataclass" -- was already done in an
            # earlier phase, when the (fronts, backs) tuple became
            # _DuplexMismatch.
            match duplex_result:
                case ScanBatch():
                    batch = duplex_result
                case _DuplexMismatch():
                    # Returns early and deliberately skips _drop_empty_pages
                    # below -- see _finish_duplex_mismatch for why (D-08).
                    return _finish_duplex_mismatch(
                        duplex_result, tmp_path, paperless, request, settings
                    )
                case _:
                    assert_never(duplex_result)

        records = batch.pages
        actual_dpi = batch.actual_resolution
        rejected_warning = _rejected_pages_warning(batch.pages_rejected)

        # There is no per-page EXIF strip here any more, and restoring one
        # would have nothing to act on (Pitfall #5).  The pages are files the
        # spool wrote: the backend drops ``info["exif"]`` before handing a page
        # over, and Pillow's PNG encoder emits an EXIF chunk only for one
        # passed to it through ``encoderinfo``, which the spool never does.
        # The thumbnail helper in ``pages`` still strips it for its JPEG.

        # Step 2: Filter empty pages (gated on profile toggle, per D-17)
        filtered = _drop_empty_pages(records, profile)

        # Step 3: Assemble PDF
        notify(PipelineEvent.ASSEMBLING)
        # The resolution the device read back is the authoritative DPI, not the
        # one the profile asked for. SANE substitutes silently -- measured, a
        # request for 5000 comes back as 1200 -- and the read-back value is the
        # same one the backend's crop arithmetic used, so the cropped shape and
        # the declared page size cannot disagree. A device that substitutes
        # would otherwise produce both a mis-cropped page and a MediaBox at odds
        # with its own content, re-opening part of OUTC-06.
        pdf_path = assemble_pdf(
            filtered,
            tmp_path,
            filename=build_pdf_filename(request.job_id, request.title),
            dpi=actual_dpi,
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
            pages_scanned=len(records),
            # Blank-page detection only. A sheet the scanner could not read is
            # reported through the warning instead, because Phase 30 renders
            # this number to users as pages removed for being blank.
            pages_removed=len(records) - len(filtered),
            pages_uploaded=len(filtered),
            warning=_join_warnings(warning, rejected_warning),
        )

        notify(PipelineEvent.DONE)
        logger.info("Pipeline complete for '%s'", request.title)

    return result
