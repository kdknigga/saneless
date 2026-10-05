"""The feeder benchmark refuses a run whose pages are not the stated workload."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from saneless.scanner.base import PageRecord, ScanBatch
from saneless.scanner.sane_backend import SaneBackend
from scripts.feeder_throughput import (
    _PAGE_PIXELS,
    _SHEETS_PER_PASS,
    BenchmarkError,
    _one_run,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from saneless.scanner.base import PageSink, ScanSettings


def _record(sequence: int, size: tuple[int, int]) -> PageRecord:
    """
    Describe one spooled page of ``size`` pixels.

    Returns:
        The record.

    """
    return PageRecord(
        sequence=sequence,
        path=Path(f"page-{sequence}.png"),
        size=size,
        mode="L",
        dpi=300,
        ink_coverage=0.0,
        paper_white=255,
    )


@pytest.mark.parametrize("odd_page", [0, _SHEETS_PER_PASS - 1], ids=["first", "last"])
def test_a_pass_with_any_page_of_another_size_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, odd_page: int
) -> None:
    """
    Every page of every pass must be the stated size, not only the first.

    Args:
        monkeypatch: Stands in for the backend's scans.
        tmp_path: The run's spool root.
        odd_page: The index of the one page of another size.

    """
    backend = SaneBackend()

    def scan_pages(_device: str, _settings: ScanSettings, _sink: PageSink) -> ScanBatch:
        sizes = [_PAGE_PIXELS] * _SHEETS_PER_PASS
        sizes[odd_page] = (_PAGE_PIXELS[0], _PAGE_PIXELS[1] - 1)
        return ScanBatch(
            pages=tuple(_record(index, size) for index, size in enumerate(sizes)),
            actual_resolution=300,
            pages_rejected=0,
        )

    @contextlib.contextmanager
    def no_session() -> Generator[None]:
        yield

    monkeypatch.setattr(backend, "scan_pages", scan_pages)
    monkeypatch.setattr(backend, "scan_session", no_session)

    with pytest.raises(BenchmarkError, match="pixel pages"):
        _one_run(backend, "Gray", 1, tmp_path)
