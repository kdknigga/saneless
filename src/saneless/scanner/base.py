"""
Scanner abstraction layer base types and ABC.

Defines the interface that all scanner backends must implement,
along with data types for device information, capabilities, and
scan settings.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final, Literal, assert_never

from saneless.exceptions import (
    ListingAbortedError,
    ListingCrashedError,
    ListingNoAnswerError,
    ListingTimedOutError,
)
from saneless.text_safety import neutralise_controls

if TYPE_CHECKING:
    import threading
    from pathlib import Path

    from PIL import Image

    from saneless.vocabulary import PaperSize

__all__ = [
    "MAX_PAGES_PER_PASS",
    "DeviceCapabilities",
    "DeviceInfo",
    "DeviceSurvey",
    "PageRecord",
    "PageSink",
    "PassCapReached",
    "ScanBatch",
    "ScanSettings",
    "ScannerBackend",
    "SourceKind",
    "classify_source",
]

logger = logging.getLogger(__name__)

# The most pages one acquisition pass may produce: one scan_pages() call.
#
# SCOPE: per pass, not per job. The two manual-duplex passes each call
# scan_pages() separately, and a document grown pass by pass calls it once per
# pass, so each is bounded by this number on its own. A cap on a whole
# document is a separate number, paired with this one where it is declared.
#
# The value matches the largest production ADF hoppers, so no real stack should
# reach it in one pass. That is a judgement about hardware, not a measurement,
# and it is cheap to revise precisely because the error names the cap. It lives
# here, backend-neutral, so every number derived from it is derived from one
# place; what the cap bounds, and what it does not, is explained where the SANE
# backend enforces it (_MAX_ADF_PAGES in sane_backend.py).
MAX_PAGES_PER_PASS: Final = 500


class SourceKind(StrEnum):
    """What kind of scan source a SANE source name denotes."""

    FLATBED = "FLATBED"
    FEEDER = "FEEDER"
    FEEDER_DUPLEX = "FEEDER_DUPLEX"
    AUTO = "AUTO"
    UNKNOWN = "UNKNOWN"

    @property
    def uses_feeder(self) -> bool:
        """Whether this kind of source feeds a stack of sheets."""
        match self:
            case SourceKind.FEEDER | SourceKind.FEEDER_DUPLEX:
                feeds = True
            case SourceKind.FLATBED | SourceKind.AUTO | SourceKind.UNKNOWN:
                feeds = False
            case _:
                assert_never(self)
        return feeds


_FEEDER_TOKENS = ("adf", "document feeder", "feeder")


def classify_source(source: str) -> SourceKind:
    """
    Classify a SANE source name into a SourceKind.

    This is the ONLY source-classification rule in the codebase. Every
    question of the form "is this source a feeder?" must be answered by
    calling this function and inspecting the returned member; do not
    re-derive the answer from the source string anywhere else.

    The branch order below is deliberate and load-bearing:

    1. ``"auto"`` is matched by EXACT equality, never as a substring.
       ``"automatic document feeder"`` starts with the letters ``auto``, so a
       substring test would classify the single most common real-world feeder
       name as AUTO and route a ten-page stack down the single-page path.
    2. ``"duplex"`` is tested before the feeder tokens so that names carrying
       no feeder token at all -- Fujitsu's ``"Card Duplex"``, this project's
       ``"Manual Duplex"`` -- classify as duplex rather than falling through.
    3. The feeder tokens are matched next. ``"feeder"`` deliberately does not
       match ``"Manual Feed Tray"``: "feed" is not "feeder".
    4. ``"flatbed"`` is matched last of the positive rules.

    Args:
        source: SANE source name string (e.g., "Flatbed", "ADF Duplex").
            Leading and trailing whitespace is ignored and matching is
            case-insensitive.

    Returns:
        The SourceKind the name denotes, or SourceKind.UNKNOWN if the name
        matches no rule.

    """
    lower = source.strip().lower()
    if lower == "auto":
        return SourceKind.AUTO
    if "duplex" in lower:
        return SourceKind.FEEDER_DUPLEX
    if any(token in lower for token in _FEEDER_TOKENS):
        return SourceKind.FEEDER
    if "flatbed" in lower:
        return SourceKind.FLATBED
    # ANSWERED, not deferred. UNKNOWN keeps single-page routing, so
    # uses_feeder is False.
    #
    # The proposed "safer" default -- "if the device exposes a source option, treat
    # anything that is not the flatbed entry as multi-page" -- is DECLINED.
    # It is a bet about scanners nobody has seen, and it trades a cheap,
    # visible failure for an expensive one.
    #
    # The residual risk is accepted knowingly: a genuinely new feeder name
    # yields a one-page PDF until someone adds a token to _FEEDER_TOKENS
    # above. That is visible to the operator and is fixed by one entry in a
    # tuple. The opposite failure -- treating a flatbed as a feeder and
    # re-scanning the platen until something stops it -- is the expensive one,
    # and it is bounded separately, per pass: by MAX_PAGES_PER_PASS above for a
    # source named as a feeder, which sane_backend.py enforces as
    # _MAX_ADF_PAGES, and by the much lower _MAX_AUTO_FEEDER_PAGES there for a
    # source sent through the feeder that does not classify as one.
    #
    # This is a settled answer. Do not re-open it as an unmade decision.
    return SourceKind.UNKNOWN


@dataclass
class DeviceInfo:
    """Information about a discovered scanning device."""

    name: str
    vendor: str
    model: str
    device_type: str

    def __post_init__(self) -> None:
        """
        Escape control characters in the fields that are only ever shown.

        A saned or eSCL device on the LAN chooses its own vendor, model and
        type strings, and an ESC among them would reach the operator's terminal
        as a live escape sequence. Those three are display-only, so they are
        made safe once, here.

        ``name`` is kept byte for byte: it is passed back to ``sane.open`` and
        written into the configuration, so changing one character would break
        the round trip. It is escaped at each place it is displayed instead.
        """
        self.vendor = neutralise_controls(self.vendor)
        self.model = neutralise_controls(self.model)
        self.device_type = neutralise_controls(self.device_type)


@dataclass(frozen=True, slots=True)
class DeviceSurvey:
    """
    What one list-then-open found: the Scanner health check's evidence.

    Every failure is recorded as an exception's class name and nothing else.
    The check reports these, and nothing it causes to be logged may carry a
    device id or an exception's text, since a ``net:`` id is a LAN address and
    the text of a SANE error can repeat it.

    Attributes:
        devices: The devices listed, empty when the listing raised.
        list_error: The class name of the exception the listing raised, or
            ``None`` when it did not raise.
        configured_opened: Whether opening the configured device worked, or
            ``None`` when no open was attempted, because no device was
            configured or the listing already included it.
        open_error: The class name of the exception the open raised, or
            ``None`` when it did not raise or was not attempted.

    """

    devices: tuple[DeviceInfo, ...]
    list_error: str | None = None
    configured_opened: bool | None = None
    open_error: str | None = None


@dataclass(kw_only=True)
class DeviceCapabilities:
    """
    Available options and constraints reported by a scanner device.

    ``resolutions`` and ``resolution_range`` are two different facts, not two
    spellings of one. A SANE device constrains its resolution option with
    *either* a word list *or* a ``(min, max, step)`` range, never both, so at
    most one of these two fields is ever populated and **neither is derived
    from the other**. Expanding a range into a list of plausible DPIs would
    print saneless's own invention rather than the device's answer, and
    inferring a range from a word list would claim the device accepts every
    value between the listed ones. Whichever shape the device actually gave is
    the one that gets filled in; an option it leaves unconstrained fills
    neither. The two therefore cannot disagree with each other.

    The range keeps the ``float`` members the device reported -- measured
    against the SANE ``test`` backend, ``(1.0, 1200.0, 1.0)``. Callers wanting
    whole dpi coerce at the point of use rather than at the point of reading,
    so nothing here quietly rounds off what the scanner said.

    Construction is keyword-only, so the field order is nobody's dependency,
    and the option list is carried as names rather than as a backend's own
    option records: nothing outside a backend has to know where in a SANE
    option tuple the name sits.

    Attributes:
        sources: The scan sources the device offers.
        resolutions: The exact resolutions the device offers, when it
            constrains the option with a word list. Empty otherwise.
        modes: The scan modes the device offers.
        option_names: The names of the options the device reports, in the
            order it reports them.
        resolution_range: The ``(min, max, step)`` the device reports, when it
            constrains the option with a range. None otherwise.

    """

    sources: list[str]
    resolutions: list[int]
    modes: list[str]
    option_names: tuple[str, ...] = ()
    resolution_range: tuple[float, float, float] | None = None


@dataclass(frozen=True, kw_only=True)
class ScanSettings:
    """
    Settings for a scan operation.

    Frozen, so the settings resolved for a job are the settings the scanner
    uses: nothing between ``run_pipeline`` and the backend can change a field
    after construction. Keyword-only, so every field is named at the call site
    and adding one cannot silently shift another's value.

    Attributes:
        source: The source name, as the profile gives it.
        resolution: The requested resolution in dpi.
        mode: The requested scan mode.
        auto_source_mode: Where an ``Auto`` source is routed, mirroring
            ``ProfileConfig.auto_source_mode``.
        duplex: The profile's duplex choice, mirroring
            ``ProfileConfig.duplex``.
        paper_size: The paper size to frame the page to.

    """

    source: str
    resolution: int
    mode: str
    auto_source_mode: Literal["flatbed", "adf"] = "flatbed"
    # Mirrors ProfileConfig.duplex, converted at one point in run_pipeline.
    # The scanner package still does not depend on the job-state enums, so
    # this is the profile's own Literal rather than a scanner-side enum.
    #
    # "manual" resolves a document feeder from the device's own source list
    # instead of validating ``source`` verbatim: the backend prefers
    # ``source`` when the device reports it and it feeds, falls back to the
    # first reported feeder, and refuses when there is none -- it never
    # substitutes ``Auto``, which is how manual duplex once took two platen
    # snapshots and reported success.
    #
    # "hardware" reaches the scanner as its own value, not folded into
    # "none": a device that selects duplex through a separate ADF-mode option
    # needs that option set, so the two are not one behaviour with two
    # spellings.
    duplex: Literal["none", "hardware", "manual"] = "none"
    paper_size: PaperSize = "full"


@dataclass(frozen=True, slots=True)
class PassCapReached:
    """
    One acquisition pass stopped at its page cap, and which sheet it dropped.

    A pass is bounded because a device that never reports the end of its feed
    -- a platen rescanned as a feeder -- would otherwise scan forever. Reaching
    the bound is not a failure: the pages already scanned are kept, and this
    records where the pass stopped so the operator knows where to resume.

    Attributes:
        cap: The most pages the pass could keep.
        sheet_not_kept: The number of the fed sheet that was acquired past the
            cap and discarded, counted from one over every sheet fed in the
            pass, readable or not. The overrun is only seen on that sheet, so
            it is always ``cap + 1``.
        auto_source: Whether the lower bound for a source that is not a named
            feeder applied, rather than the per-pass cap a named feeder gets.

    """

    cap: int
    sheet_not_kept: int
    auto_source: bool


@dataclass(frozen=True)
class ScanBatch:
    """
    The pages one acquisition produced, and what the device actually did.

    Five fields, and the per-page structure is ``pages`` itself: the ordered
    page records are that design, and every per-page fact -- which file a
    sheet landed in, what was measured about it, where it sat in the pass --
    belongs on ``PageRecord`` rather than here. What survives at batch level
    is what the backend knows about the pass as a whole: the two things the
    device reports (``actual_resolution`` and ``pages_rejected``), one thing
    its own source resolution produced (``substituted_source``), and whether
    the pass stopped at its page cap (``cap_reached``). This is the one
    channel carrying them alongside the records, and there is no second route
    out of the backend. Do not extend this casually: a fact that
    is per-page belongs on the record. It deliberately carries no geometry
    either, because a scan area the device clamped falls back to the existing
    crop rather than being reported back out.

    ``frozen=True`` because this is a report of what a device has already
    done, and nothing downstream has any business rewriting it afterwards.

    Attributes:
        pages: The page records the sink produced, in document order. The
            order of this tuple is the document order, and
            ``PageRecord.sequence`` -- not the filesystem, not a glob, not a
            sort -- is the proof of it. After a manual-duplex
            interleave the spooled file names do not sort into document order
            at all, so recovering order from the directory is not a shortcut
            but a bug.
        actual_resolution: The resolution the device reported back, in whole
            dpi. Not the requested one -- SANE substitutes silently -- and it
            is the value both the crop arithmetic and the PDF's declared page
            size have to agree on.
        pages_rejected: How many fed sheets failed their integrity checks and
            were skipped. This is deliberately NOT the pipeline's blank-page
            removal count: that one is empty-page detection and is rendered to
            users as pages removed for being blank, so reporting a sheet the
            device could not read through it would be a new small lie.
        substituted_source: The source the profile asked for, when the device
            offered no such source and its ``Auto`` source was used in its
            place *and* ``auto_source_mode`` sent that ``Auto`` through the
            document feeder. None otherwise, including when ``Auto`` stood in
            for a flatbed and scanned the glass as asked: nothing was lost
            then, so there is nothing for the job to report.
        cap_reached: The page cap the pass stopped at and the sheet it fed but
            did not keep, when the device was still feeding at the cap. None
            when the feed ended on its own. A capped pass keeps its pages and
            is not an error: this is a fact about the whole pass, reported so
            the job can say where to resume.

    """

    pages: tuple[PageRecord, ...]
    actual_resolution: int
    pages_rejected: int
    substituted_source: str | None = None
    cap_reached: PassCapReached | None = None


@dataclass(frozen=True)
class PageRecord:
    """
    One acquired page, after it was written to the spool.

    Facts, never verdicts. Every field here is something that was measured
    while the page was in memory; nothing here is a judgement about what the
    page means. In particular there is deliberately **no** ``is_blank`` field:
    blank-page policy belongs to the pipeline, under the profile's toggle,
    and the pipeline's blank-page filter applies the profile's
    ``empty_page_coverage_threshold`` to the ``ink_coverage`` and
    ``paper_white`` stored here. A verdict baked in at acquisition would
    freeze one profile's threshold into the record and make both the
    threshold and the toggle a lie.

    ``sequence`` is 1-based and is assigned at acquisition, by the sink, in the
    order the device produced the sheets. It is the proof of document order,
    and it exists so that order is never recovered by sorting or globbing the
    spool directory: the two passes of a manual-duplex job spool into
    distinguishable file names for debuggability only, and after the interleave
    the file names no longer sort into document order at all.

    ``frozen=True`` for the same reason ``ScanBatch`` is frozen: this is a
    report of what has already happened on disk, and nothing downstream has any
    business rewriting it afterwards. The interleave reorders records; it never
    edits one.

    Attributes:
        sequence: The 1-based position this page had in its acquisition pass,
            assigned when the page was spooled.
        path: The spooled PNG this record describes. It is exactly the file
            the PDF's page content is built from -- nothing re-encodes it.
        size: The page's ``(width, height)`` in pixels, as the device produced
            it and as the PNG stores it.
        mode: The page's Pillow mode as spooled: ``"1"``, ``"L"`` or
            ``"RGB"``. A SANE snap arrives as ``"L"`` or ``"RGB"``; any other
            mode the sink accepts is converted to one of those first.
        dpi: The resolution the device read back for this page, which every
            PDF lays the page out at: the document, a mismatch half and a
            preserved partial alike. A fact, like the rest: it is what the
            device said, not what the profile asked for.
        ink_coverage: The share of the page, inside a thin trimmed margin,
            that is ink, in percent (0-100), measured once at spool time by
            ``pages.measure_ink``.
        paper_white: The paper's grey level (0-255) in the page's darkest
            channel, measured at the same moment.

    """

    sequence: int
    path: Path
    size: tuple[int, int]
    mode: str
    dpi: int
    ink_coverage: float
    paper_white: int


class PageSink(ABC):
    """
    Where the backend puts each page it acquires.

    The backend acquires one page, crops it if it has to, hands it here, and
    forgets it. It never accumulates a list of images, which is the whole of
    the memory bound: peak memory is a property of who holds a page, not
    of how the pages are produced. The concrete implementation is
    pipeline-owned (``saneless.spool.SpooledPageSink``), because where a page
    lands and what is measured about it are pipeline concerns; this module
    declares only the shape the two sides agree on.

    This is an ``ABC`` and not a ``typing.Protocol``, following the rule
    already stated in ``saneless.flip.FlipCoordinator``'s docstring and
    observable in the tree: ``Protocol`` describes shapes this project does not
    own (``SaneDevice`` for python-sane's handle), while ``ABC`` defines seams
    the project implements itself (``ScannerBackend``). A page sink is a seam
    this project implements.

    Two alternatives were rejected:

    1. Going back to a generator. The backend moved away from one because a
       generator can only hand back images: its return value -- the resolution
       the device settled on and the sheets it rejected -- is discarded by the
       ``list()`` every caller wrapped it in. Streaming pages out is not worth
       throwing those two facts away again.
    2. A bare callback with no declared contract. The type checkers could not
       see what it promised, so neither a wrong argument nor a wrong return
       value would have been caught anywhere.
    """

    @abstractmethod
    def add(self, image: Image.Image, *, dpi: int) -> PageRecord:
        """
        Take ownership of one acquired page and materialise it.

        The sink owns the page from this call onwards: it decides where the
        page lands, writes it, and measures it. The caller must not retain the
        image afterwards -- a retained reference is exactly the accumulation
        this seam exists to prevent, and it would put peak memory back to
        growing with the page count.

        ``dpi`` is **required**, with no default, because it is a fact about
        the page that only the caller knows. A default would have kept every
        caller compiling while laying out a page read back at 150 dpi as if it
        were the profile's 300 -- half size, in a preserved partial nobody
        re-scans.

        Args:
            image: The page the device produced, already cropped if the
                requested paper size required it.
            dpi: The resolution the device read back, which the page is
                recorded and laid out at.

        Returns:
            A PageRecord describing where the page was written and what was
            measured about it, with the next 1-based sequence number.

        """


class ScannerBackend(ABC):
    """
    Abstract base class for scanner backends.

    All scanner operations go through this interface, allowing
    different implementations (real SANE, mock for testing, etc.).
    """

    @abstractmethod
    def get_devices(self) -> list[DeviceInfo]:
        """Enumerate available scanning devices."""

    @abstractmethod
    def get_capabilities(self, device_id: str) -> DeviceCapabilities:
        """
        Query device capabilities and available options.

        Args:
            device_id: SANE device identifier string.

        Returns:
            Device capabilities including available sources,
            resolutions, and modes.

        """

    def open_and_close(self, device_id: str) -> None:
        """
        Open a device and close it again, which is the first thing a scan does.

        The Scanner health check calls this to learn whether a configured
        device the backend did not list can be used, without scanning.  It is
        its own method rather than a call to ``get_capabilities`` because the
        check's log is held to a stricter rule than a scan's: nothing the
        check causes to be logged may name a device id or carry an
        exception's text, since a ``net:`` id is a LAN address.  A backend
        whose own open or close logging could break that rule overrides this.

        Deliberately not an ``@abstractmethod``.  The default opens the device
        the only way this interface otherwise offers, through
        ``get_capabilities``, which is right for a backend that logs nothing
        of its own.

        Args:
            device_id: SANE device identifier string.

        """
        self.get_capabilities(device_id)

    def list_and_open(
        self, open_if_unlisted: str, *, abort: threading.Event | None = None
    ) -> DeviceSurvey:
        """
        List the devices, then open a configured device the listing lacks.

        This is the Scanner health check's list-then-open.  A configured id
        the listing does not include may still be usable, since a scan opens
        it without listing first, so it is opened and closed again to find
        out.  An id the listing includes is not opened.

        Deliberately not an ``@abstractmethod``.  The default makes the two
        calls this interface already offers, ``get_devices`` and then
        ``open_and_close``, which keeps every stub and test double working
        unchanged.  A real backend overrides it to list and open in one
        isolated step.

        A listing that crashed, was stopped at its deadline or on ``abort``,
        or gave no usable answer is not a failed listing: it propagates, so
        the caller can report it as what it was.
        Any other failure, of the listing or of the open, is recorded by
        class name.  Nothing is logged here; the caller logs type names.

        Args:
            open_if_unlisted: The configured device id, or ``""`` when none
                is configured.
            abort: Set by another thread to stop the listing part way; the
                backend passes it to its listing.  The default makes two
                calls that cannot be stopped part way, and ignores it.

        Returns:
            What the listing and the open found.

        Raises:
            ListingCrashedError: The listing died from a signal.
            ListingTimedOutError: The listing did not finish in time.
            ListingNoAnswerError: The listing gave no usable answer.
            ListingAbortedError: The listing was stopped on ``abort``.

        """
        _ = abort
        devices: tuple[DeviceInfo, ...] = ()
        list_error: str | None = None
        try:
            devices = tuple(self.get_devices())
        except (
            ListingCrashedError,
            ListingTimedOutError,
            ListingNoAnswerError,
            ListingAbortedError,
        ):
            raise
        except Exception as exc:
            list_error = type(exc).__name__
        if not open_if_unlisted or any(
            device.name == open_if_unlisted for device in devices
        ):
            return DeviceSurvey(devices=devices, list_error=list_error)
        try:
            self.open_and_close(open_if_unlisted)
        except Exception as exc:
            return DeviceSurvey(
                devices=devices,
                list_error=list_error,
                configured_opened=False,
                open_error=type(exc).__name__,
            )
        return DeviceSurvey(
            devices=devices, list_error=list_error, configured_opened=True
        )

    @abstractmethod
    def scan_pages(
        self, device_id: str, settings: ScanSettings, sink: PageSink
    ) -> ScanBatch:
        """
        Acquire pages from scanner, handing each one to the sink.

        This returns a completed batch rather than yielding pages. A generator
        can only hand back images, and its return value is discarded by the
        ``list()`` every caller wrapped it in, so the two facts the backend
        measures -- the resolution the device settled on, and the sheets it
        could not read -- had no way out of the backend at all.

        The *input* is a sink for the matching reason. The backend must
        never hold more than one decoded page: peak memory is a property of
        who holds a page, not of how the pages are produced, and a backend
        that accumulated them would put peak memory back where it was
        measured at 1395 MB for 48 pages. Where a page lands is not the
        backend's business either -- the workspace, the ``tmp_dir`` under it
        and the job id in its name are all pipeline facts -- so the caller
        supplies the concrete sink and the backend only fills it.

        Two alternatives were rejected. Going back to a generator streams
        pages out but throws away the two measured facts again, which is why
        the backend moved away from one. A bare callback with no declared
        contract was rejected because neither type checker could see what it
        promised, so neither a wrong argument nor a wrong return value would
        have been caught anywhere.

        Args:
            device_id: SANE device identifier string.
            settings: Scan settings (source, resolution, mode).
            sink: Where each accepted page goes. The backend calls ``add`` on
                it exactly once per accepted page, after that page passed its
                integrity checks and was cropped, and retains nothing
                afterwards.

        Returns:
            A ScanBatch carrying the records the sink returned, the resolution
            the device actually used, and how many fed sheets failed their
            integrity checks.

        """

    def close(self) -> None:
        """
        Release whatever this backend holds process-wide.

        Deliberately **not** an ``@abstractmethod``, and the default body does
        nothing but say so. Most backends hold no process-global resource at
        all, so requiring the method would force an empty override onto every
        test stub and buy nothing; the one implementation that does hold one,
        ``SaneBackend``, overrides it to run the process-level SANE shutdown.
        Declaring it here is what lets an entry point shut a backend down
        through this abstraction instead of by naming the concrete class.

        An implementation logs a close failure and never raises it. That is
        the rule ``SaneBackend._open_device``'s ``finally`` block already
        follows for ``dev.close()``, and it holds for the same reason: an
        exception raised out of a close would replace the error that ended the
        scan, which is the one the operator actually needs to see.

        The DEBUG line is the body: a bare docstring would be an empty method
        on an ABC, which ruff's ``B027`` flags precisely because such a method
        is usually an unfinished override, and this project adds no ``noqa``
        to say otherwise. Logging which backend declined to close says it in
        code instead, and it is the line that tells an operator reading a
        shutdown log that nothing was skipped by accident.
        """
        logger.debug("close() is a no-op for %s", type(self).__name__)

    def reinitialise(self) -> None:
        """
        Restart whatever process-wide library this backend drives, before a job.

        Called at the top of each scan job, and again before every pass of a
        multi-page scan after the first, never with a device handle open.  The
        second pass of a manual-duplex scan does not call it.

        Deliberately **not** an ``@abstractmethod``, for the reason ``close()``
        gives: the default does nothing, because a backend holding no
        process-global library has nothing to restart, and requiring the
        method would force an empty override onto every test stub.
        ``SaneBackend`` overrides it to restart SANE at each of those points,
        and refuses there while a read is stuck or a handle is open.

        The DEBUG line is the body, as in ``close()``, so ruff's ``B027`` has
        no empty method on an ABC to flag.
        """
        logger.debug("reinitialise() is a no-op for %s", type(self).__name__)
