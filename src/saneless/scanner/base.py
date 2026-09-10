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
    from collections.abc import Iterator

    from PIL import Image

__all__ = [
    "DeviceCapabilities",
    "DeviceInfo",
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
    # UNKNOWN keeps today's single-page routing (uses_feeder is False). The
    # safer "anything that is not the flatbed entry is multi-page" default that
    # the code review floats is a bet about unseen scanners and belongs to
    # Phase 24, not here (D-11).
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
    """Available options and constraints reported by a scanner device."""

    sources: list[str]
    resolutions: list[int]
    modes: list[str]
    raw_options: list[tuple] = field(default_factory=list)


@dataclass
class ScanSettings:
    """Settings for a scan operation."""

    source: str
    resolution: int
    mode: str
    auto_source_mode: str = "flatbed"
    paper_size: str = "full"


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
    def scan_pages(
        self, device_id: str, settings: ScanSettings
    ) -> Iterator[Image.Image]:
        """
        Acquire pages from scanner.

        Args:
            device_id: SANE device identifier string.
            settings: Scan settings (source, resolution, mode).

        Yields:
            PIL Image objects for each scanned page.

        """
