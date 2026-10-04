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

# The most pages one acquisition pass, one scan_pages() call, may produce. It
# bounds each pass on its own, not a whole job: each manual-duplex pass and each
# pass of a document grown pass by pass gets the full cap. Sized to the largest
# production hoppers. See docs/explanation/decisions/0007-auto-feeder-page-cap.md.
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

    This is the only source-classification rule: answer "is this source a
    feeder?" by calling it, never by re-reading the source string.

    The branch order is load-bearing:

    1. ``"auto"`` matches by equality only, because ``"automatic document
       feeder"`` starts with ``auto``.
    2. ``"duplex"`` is tested before the feeder tokens, so names with no feeder
       token, such as ``"Card Duplex"`` and ``"Manual Duplex"``, are duplex.
    3. The feeder tokens come next; ``"Manual Feed Tray"`` is not a feeder.
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
    # An unrecognised source keeps single-page routing (uses_feeder is False),
    # so it never enters the feeder loop for being unrecognised. A new feeder
    # name is supported by adding its token to _FEEDER_TOKENS.
    # See docs/explanation/decisions/0001-unknown-sources-scan-one-page.md.
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

        A LAN device chooses its own vendor, model and type strings, so an ESC
        in them would reach the operator's terminal live. ``name`` is kept byte
        for byte, because it goes back to ``sane.open`` and into the
        configuration; it is escaped where it is displayed instead.
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
        start_error: The class name of the failure that kept the scanner
            library from starting, or ``None`` when it started.

    """

    devices: tuple[DeviceInfo, ...]
    list_error: str | None = None
    configured_opened: bool | None = None
    open_error: str | None = None
    start_error: str | None = None


@dataclass(kw_only=True)
class DeviceCapabilities:
    """
    Available options and constraints reported by a scanner device.

    A SANE device constrains resolution with either a word list or a
    ``(min, max, step)`` range, never both, so at most one of ``resolutions``
    and ``resolution_range`` is populated and neither is derived from the
    other. The range keeps the ``float`` members the device reported, such as
    the SANE ``test`` backend's ``(1.0, 1200.0, 1.0)``; callers wanting whole
    dpi round at the point of use.

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

    Frozen, so nothing between ``run_pipeline`` and the backend can change the
    settings resolved for a job.

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
    # The profile's own Literal, so the scanner package does not depend on the
    # job-state enums. "manual" makes the backend pick a feeder from the
    # device's sources and refuse when there is none, never substituting Auto.
    # "hardware" stays distinct from "none" because some devices select duplex
    # through a separate ADF-mode option that must be set.
    duplex: Literal["none", "hardware", "manual"] = "none"
    paper_size: PaperSize = "full"


@dataclass(frozen=True, slots=True)
class PassCapReached:
    """
    One acquisition pass stopped at its page cap, and which sheet it dropped.

    Reaching the cap is not a failure: the pages already scanned are kept, and
    this records where the pass stopped so the operator knows where to resume.

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

    Only facts about the pass as a whole live here; a per-page fact belongs on
    ``PageRecord``. The batch is the backend's only channel out besides the
    records, and it carries no geometry: a scan area the device clamped falls
    back to the existing crop.

    Attributes:
        pages: The page records the sink produced, in document order. Order
            comes from this tuple and ``PageRecord.sequence``, never from the
            spool directory: after a manual-duplex interleave the file names
            do not sort into document order.
        actual_resolution: The resolution the device reported back, in whole
            dpi, which SANE may substitute silently for the requested one. The
            crop arithmetic and the PDF's page size both use it.
        pages_rejected: How many fed sheets failed their integrity checks and
            were skipped. Not the blank-page removal count, which users see as
            pages removed for being blank.
        substituted_source: The source the profile asked for, when the device
            offered no such source and its ``Auto`` source was used in its
            place *and* ``auto_source_mode`` sent that ``Auto`` through the
            document feeder. None otherwise, including when ``Auto`` stood in
            for a flatbed and scanned the glass as asked.
        cap_reached: The page cap the pass stopped at and the sheet it fed but
            did not keep, when the device was still feeding at the cap. None
            when the feed ended on its own.

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

    Facts, never verdicts: there is deliberately no ``is_blank`` field. The
    pipeline's blank-page filter applies the profile's threshold to
    ``ink_coverage`` and ``paper_white``, so a verdict baked in here would
    freeze one profile's threshold into the record.

    Document order comes from ``sequence``, never from sorting or globbing the
    spool directory, whose file names do not sort into document order after a
    manual-duplex interleave. The interleave reorders records; it never edits
    one.

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
        dpi: The resolution the device read back for this page, not the one
            the profile asked for. Every PDF lays the page out at it: the
            document, a mismatch half and a preserved partial alike.
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
    forgets it, so it never holds more than one decoded page. The pipeline
    owns the implementation (``saneless.spool.SpooledPageSink``) and decides
    where a page lands. See docs/explanation/decisions/0005-page-sink-contract.md.

    An ``ABC``, not a ``typing.Protocol``, by the convention CONTRIBUTING.md
    states for seams this project implements itself.
    """

    @abstractmethod
    def add(self, image: Image.Image, *, dpi: int) -> PageRecord:
        """
        Take ownership of one acquired page and materialise it.

        The sink owns the page from this call onwards: it decides where the
        page lands, writes it, and measures it. The caller must not retain the
        image afterwards, or peak memory grows with the page count again.

        ``dpi`` has no default because only the caller knows it: a default
        would lay out a page read back at 150 dpi as if it were 300.

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
        device the backend did not list can be used, without scanning.  Nothing
        the check causes to be logged may name a device id or carry an
        exception's text, since a ``net:`` id is a LAN address, so a backend
        whose own open or close logging could break that rule overrides this.
        The default goes through ``get_capabilities``.

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

        The default calls ``get_devices`` and then ``open_and_close``, so a
        test double needs neither override; a real backend lists and opens in
        one isolated step.

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

        The backend never holds more than one decoded page and never decides
        where a page is stored.
        See docs/explanation/decisions/0005-page-sink-contract.md.

        The PDF assembly that follows keeps the same one-page memory bound.
        See docs/explanation/decisions/0006-per-page-pdf-and-qpdf-merge.md.

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

        The default does nothing; ``SaneBackend`` overrides it to run the
        process-level SANE shutdown. An implementation logs a close failure
        and never raises it, because an exception out of a close would replace
        the error that ended the scan.
        """
        logger.debug("close() is a no-op for %s", type(self).__name__)

    def reinitialise(self) -> None:
        """
        Restart whatever process-wide library this backend drives, before a job.

        Called at the top of each scan job, and again before every pass of a
        multi-page scan after the first, never with a device handle open.  The
        second pass of a manual-duplex scan does not call it.

        The default does nothing. ``SaneBackend`` overrides it to restart SANE
        at each of those points, and refuses while a read is stuck or a handle
        is open.
        """
        logger.debug("reinitialise() is a no-op for %s", type(self).__name__)
