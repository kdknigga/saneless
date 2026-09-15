"""
Scanner abstraction layer base types and ABC.

Defines the interface that all scanner backends must implement,
along with data types for device information, capabilities, and
scan settings.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, assert_never

if TYPE_CHECKING:
    from pathlib import Path

    from PIL import Image

__all__ = [
    "DeviceCapabilities",
    "DeviceInfo",
    "PageRecord",
    "PageSink",
    "ScanBatch",
    "ScanSettings",
    "ScannerBackend",
    "SourceKind",
    "classify_source",
]


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
    # ANSWERED, not deferred -- Phase 24, D-01. UNKNOWN keeps single-page
    # routing, so uses_feeder is False.
    #
    # C-06's safer default -- "if the device exposes a source option, treat
    # anything that is not the flatbed entry as multi-page" -- is DECLINED.
    # It is a bet about scanners nobody has seen, and it trades a cheap,
    # visible failure for an expensive one.
    #
    # The residual risk is accepted knowingly: a genuinely new feeder name
    # yields a one-page PDF until someone adds a token to _FEEDER_TOKENS
    # above. That is visible to the operator and is fixed by one entry in a
    # tuple. The opposite failure -- treating a flatbed as a feeder and
    # re-scanning the platen until something stops it -- is the expensive one,
    # and it is bounded separately by _MAX_ADF_PAGES in sane_backend.py
    # (plan 24-03).
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


@dataclass
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

    ``resolution_range`` is defaulted and sits with the other defaulted fields.
    Moving it above ``raw_options`` would reorder the dataclass and break the
    positional construction call sites across the suite rely on.

    Attributes:
        sources: The scan sources the device offers.
        resolutions: The exact resolutions the device offers, when it
            constrains the option with a word list. Empty otherwise.
        modes: The scan modes the device offers.
        raw_options: The device's option tuples, as ``get_options()`` returns
            them.
        resolution_range: The ``(min, max, step)`` the device reports, when it
            constrains the option with a range. None otherwise.

    """

    sources: list[str]
    resolutions: list[int]
    modes: list[str]
    raw_options: list[tuple] = field(default_factory=list)
    resolution_range: tuple[float, float, float] | None = None


@dataclass
class ScanSettings:
    """Settings for a scan operation."""

    source: str
    resolution: int
    mode: str
    auto_source_mode: str = "flatbed"
    # Manual duplex: resolve a document feeder from the device's own source
    # list instead of validating ``source`` verbatim (D-02). The backend
    # prefers ``source`` when the device reports it and it feeds, falls back
    # to the first reported feeder, and refuses when there is none -- it never
    # substitutes ``Auto``, which is how C-01 took two platen snapshots.
    #
    # A plain bool, not a scanner-side DuplexMode enum: inside the scanner
    # that enum's NONE and HARDWARE members would behave identically, one
    # behaviour with two spellings. The bool also keeps ProfileConfig.duplex's
    # Literal as the only spelling of "duplex", with one conversion point in
    # run_pipeline. Like auto_source_mode it is a plain value, because Phase 21
    # D-03 forbids this package depending on the job-state enums.
    resolve_feeder_source: bool = False
    paper_size: str = "full"


@dataclass(frozen=True)
class ScanBatch:
    """
    The pages one acquisition produced, and what the device actually did.

    Three fields, and deliberately no per-page structure. Phase 29's HARD-01
    owns the ordered per-page record design -- which sheet produced which page,
    in what order, with what per-page fault -- and an object that grew
    page-level detail here would quietly pre-empt that decision. Do not extend
    this casually: a fact that is per-page belongs to HARD-01, not here. It
    deliberately carries no geometry either, because D-19 reuses the existing
    crop fallback rather than reporting the area back out.

    ``frozen=True`` is a departure from the plain ``@dataclass`` used by
    ``DeviceInfo``, ``DeviceCapabilities``, ``ScanSettings`` and
    ``pipeline.ScanResult``. There is no frozen precedent in this codebase, so
    the choice is stated rather than inherited: this is a report of what a
    device has already done, and nothing downstream has any business rewriting
    it afterwards.

    Attributes:
        pages: The acquired pages, in the order the device produced them.
        actual_resolution: The resolution the device reported back, in whole
            dpi. Not the requested one -- SANE substitutes silently -- and it
            is the value both the crop arithmetic and the PDF's declared page
            size have to agree on.
        pages_rejected: How many fed sheets failed their integrity checks and
            were skipped. This is deliberately NOT the pipeline's blank-page
            removal count: that one is empty-page detection and is rendered to
            users as pages removed for being blank, so reporting a sheet the
            device could not read through it would be a new small lie.

    """

    pages: list[Image.Image]
    actual_resolution: int
    pages_rejected: int


@dataclass(frozen=True)
class PageRecord:
    """
    One acquired page, after it was written to the spool (HARD-01, D-02).

    Facts, never verdicts. Every field here is something that was measured
    while the page was in memory; nothing here is a judgement about what the
    page means. In particular there is deliberately **no** ``is_blank`` field:
    blank-page policy belongs to the pipeline, under the profile's toggle
    (Phase 24 D-05), and ``pipeline._drop_empty_pages`` applies the profile's
    ``empty_page_mean_threshold`` / ``empty_page_stddev_threshold`` to the
    ``mean`` and ``stddev`` stored here. A verdict baked in at acquisition
    would freeze one profile's thresholds into the record and make the toggle
    a lie.

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
        mode: The page's Pillow mode, ``"L"`` or ``"RGB"`` for a SANE snap.
        mean: Greyscale mean luminance, measured once at spool time.
        stddev: Greyscale standard deviation, measured once at spool time.

    """

    sequence: int
    path: Path
    size: tuple[int, int]
    mode: str
    mean: float
    stddev: float


class PageSink(ABC):
    """
    Where the backend puts each page it acquires (HARD-01, M-08, D-01).

    The backend acquires one page, crops it if it has to, hands it here, and
    forgets it. It never accumulates a list of images, which is the whole of
    HARD-01's memory bound: peak memory is a property of who holds a page, not
    of how the pages are produced. The concrete implementation is
    pipeline-owned (``saneless.spool.SpooledPageSink``), because where a page
    lands and what is measured about it are pipeline concerns; this module
    declares only the shape the two sides agree on.

    This is an ``ABC`` and not a ``typing.Protocol``, following the rule
    already stated in ``pipeline.FlipCoordinator``'s docstring and observable
    in the tree: ``Protocol`` describes shapes this project does not own
    (``SaneDevice`` for python-sane's handle), while ``ABC`` defines seams the
    project implements itself (``ScannerBackend``). A page sink is a seam this
    project implements.

    Two alternatives were rejected (D-01):

    1. Going back to a generator. Phase 24 moved away from one because a
       generator can only hand back images: its return value -- the resolution
       the device settled on and the sheets it rejected -- is discarded by the
       ``list()`` every caller wrapped it in. Streaming pages out is not worth
       throwing those two facts away again.
    2. A bare callback with no declared contract. The type checkers could not
       see what it promised, so neither a wrong argument nor a wrong return
       value would have been caught anywhere.
    """

    @abstractmethod
    def add(self, image: Image.Image) -> PageRecord:
        """
        Take ownership of one acquired page and materialise it.

        The sink owns the page from this call onwards: it decides where the
        page lands, writes it, and measures it. The caller must not retain the
        image afterwards -- a retained reference is exactly the accumulation
        this seam exists to prevent, and it would put the memory bound back
        where Phase 29 found it.

        Args:
            image: The page the device produced, already cropped if the
                requested paper size required it.

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

    @abstractmethod
    def scan_pages(self, device_id: str, settings: ScanSettings) -> ScanBatch:
        """
        Acquire pages from scanner.

        This returns a completed batch rather than yielding pages. A generator
        can only hand back images, and its return value is discarded by the
        ``list()`` every caller wrapped it in, so the two facts the backend
        measures -- the resolution the device settled on, and the sheets it
        could not read -- had no way out of the backend at all.

        Args:
            device_id: SANE device identifier string.
            settings: Scan settings (source, resolution, mode).

        Returns:
            A ScanBatch carrying the pages, the resolution the device actually
            used, and how many fed sheets failed their integrity checks.

        """
