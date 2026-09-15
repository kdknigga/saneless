"""Tests for the page-record contract and the page spool (HARD-01, M-08)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from PIL import Image

from saneless.scanner.base import PageRecord, PageSink


class TestPageRecordContract:
    """The frozen record of facts a spooled page yields (D-02)."""

    def test_fields_are_exactly_the_contract(self) -> None:
        """PageRecord carries the six agreed fields, in the agreed order."""
        names = [field.name for field in dataclasses.fields(PageRecord)]
        assert names == ["sequence", "path", "size", "mode", "mean", "stddev"]

    def test_carries_no_verdict_field(self) -> None:
        """No is_blank/is_empty flag: verdicts belong to the pipeline (D-02)."""
        names = {field.name for field in dataclasses.fields(PageRecord)}
        assert "is_blank" not in names
        assert "is_empty" not in names

    def test_is_frozen(self) -> None:
        """A record of what already happened cannot be rewritten afterwards."""
        record = PageRecord(
            sequence=1,
            path=Path("a-0001.png"),
            size=(200, 300),
            mode="RGB",
            mean=255.0,
            stddev=0.0,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            record.sequence = 2

    def test_sequence_is_one_based(self) -> None:
        """The first page's sequence is 1, not 0 (D-02)."""
        record = PageRecord(
            sequence=1,
            path=Path("a-0001.png"),
            size=(200, 300),
            mode="RGB",
            mean=255.0,
            stddev=0.0,
        )
        assert record.sequence == 1


class TestPageSinkContract:
    """The seam the backend hands each acquired page to (D-01)."""

    def test_is_abstract(self) -> None:
        """PageSink cannot be instantiated: it declares a contract only."""
        with pytest.raises(TypeError):
            PageSink()

    def test_subclass_implementing_add_is_concrete(self) -> None:
        """A subclass that implements add() can be instantiated and used."""

        class _RecordingSink(PageSink):
            """A sink that records nothing to disk, for the contract test."""

            def add(self, image: Image.Image) -> PageRecord:
                """Return a record describing ``image`` without writing it."""
                return PageRecord(
                    sequence=1,
                    path=Path("a-0001.png"),
                    size=image.size,
                    mode=image.mode,
                    mean=255.0,
                    stddev=0.0,
                )

        sink = _RecordingSink()
        record = sink.add(Image.new("RGB", (200, 300), "white"))
        assert record.size == (200, 300)
        assert record.mode == "RGB"
