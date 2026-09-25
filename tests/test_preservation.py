"""Tests for keeping a failed run's pages in ``failed/`` (saneless.preservation)."""

from __future__ import annotations

import errno
import logging
import math
import os
import stat
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pikepdf
import pytest

import saneless.preservation as preservation_module
from saneless.exceptions import PdfError, ScanInterrupted
from saneless.preservation import (
    BACKS_SUFFIX,
    FAILED_DIR_WARN_THRESHOLD,
    FRONTS_SUFFIX,
    PARTIAL_SUFFIX,
    KeptKind,
    PreservationReport,
    RunArtefacts,
    RunStage,
    ensure_room_to_assemble,
    preserve_most_finished,
    warn_if_failed_dir_growing,
)
from saneless.spool import SpooledPageSink
from tests.golden_support import distinct_page, embedded_streams, png_idat

if TYPE_CHECKING:
    from pathlib import Path

    from saneless.scanner.base import PageRecord

_JOB_ID = "job-pres-1"
_TITLE = "Kept Scan"
# What build_pdf_filename makes of the job id and title above, after the
# timestamp: every name this module writes for the run ends with it.
_NAME_TAIL = "job-pres-kept-scan"
_MIB = 1024 * 1024
_PRIVATE_FILE_MODE = 0o600


def _spool(
    directory: Path, label: str, count: int, *, first: int = 0
) -> tuple[PageRecord, ...]:
    """
    Spool ``count`` distinct pages into ``directory`` through a real sink.

    The only place in this file that builds a sink, so a change to the sink's
    signature touches one helper.

    Args:
        directory: The spool directory; created if absent.
        label: The pass label, ``a`` or ``b``.
        count: How many pages to spool.
        first: The ``distinct_page`` index of the first page, so two passes
            never spool identical pages.

    Returns:
        The records, in acquisition order.

    """
    directory.mkdir(parents=True, exist_ok=True)
    sink = SpooledPageSink(directory, label, 0)
    for index in range(count):
        sink.add(distinct_page(first + index), dpi=300)
    return sink.records


def _artefacts(tmp_path: Path, stage: RunStage) -> RunArtefacts:
    """
    Build the artefacts of a run whose workspace and spool exist and are empty.

    Args:
        tmp_path: pytest's per-test temporary directory.
        stage: How far the run got.

    Returns:
        Artefacts naming a workspace under ``tmp_path`` and a ``failed/`` that
        does not exist yet.

    """
    workspace = tmp_path / "work"
    spool_dir = workspace / "spool"
    spool_dir.mkdir(parents=True)
    return RunArtefacts(
        job_id=_JOB_ID,
        title=_TITLE,
        workspace=workspace,
        spool_dir=spool_dir,
        failed_dir=tmp_path / "state" / "failed",
        reserve_mb=0,
        stage=stage,
    )


def _streams_of(records: tuple[PageRecord, ...] | list[PageRecord]) -> list[bytes]:
    """
    Return the image data each record's spooled file would embed, in order.

    Args:
        records: The pages, in the order a PDF should hold them.

    Returns:
        One IDAT payload per record, comparable with ``embedded_streams``.

    """
    return [png_idat(record.path.read_bytes()) for record in records]


def _mode(path: Path) -> int:
    """
    Return a path's permission bits.

    Args:
        path: The file to inspect.

    Returns:
        The mode without the file-type bits.

    """
    return stat.S_IMODE(path.stat().st_mode)


def _only_page_dir(failed_dir: Path) -> Path:
    """
    Return the one job-keyed page directory in ``failed_dir``.

    Args:
        failed_dir: The durable directory.

    Returns:
        The single subdirectory, after asserting that it is the only one.

    """
    directories = [entry for entry in failed_dir.iterdir() if entry.is_dir()]
    assert len(directories) == 1
    return directories[0]


def _refusing_assembly(message: str) -> MagicMock:
    """
    Return an ``assemble_pdf`` stand-in that always raises ``PdfError``.

    Args:
        message: The error's text.

    Returns:
        A MagicMock ready for ``monkeypatch.setattr``.

    """
    return MagicMock(side_effect=PdfError(message))


class TestTheMostFinishedArtefactIsKept:
    """Each stage keeps the most finished thing that exists for it."""

    def test_delivering_moves_the_assembled_pdfs_privately(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both undelivered PDFs move to failed/ at 0600, and nothing is built."""
        artefacts = _artefacts(tmp_path, RunStage.DELIVERING)
        _spool(artefacts.spool_dir, "a", 2)
        pdfs = [artefacts.workspace / "fronts.pdf", artefacts.workspace / "backs.pdf"]
        for pdf in pdfs:
            pdf.write_bytes(b"%PDF-assembled")
        artefacts.pdfs = pdfs
        assembling = MagicMock()
        monkeypatch.setattr(preservation_module, "assemble_pdf", assembling)

        report = preserve_most_finished(artefacts)

        kept = [artefacts.failed_dir / "fronts.pdf", artefacts.failed_dir / "backs.pdf"]
        assert report.kind is KeptKind.ASSEMBLED
        assert report.kept_paths == kept
        assert all(_mode(path) == _PRIVATE_FILE_MODE for path in kept)
        assert not any(pdf.exists() for pdf in pdfs)
        sentence = report.sentence()
        assert sentence is not None
        assert sentence.startswith(f"The scan was preserved at {kept[0]}, {kept[1]}")
        assembling.assert_not_called()

    def test_assembling_keeps_the_page_files_and_builds_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure during assembly goes straight to the page files."""
        artefacts = _artefacts(tmp_path, RunStage.ASSEMBLING)
        records = _spool(artefacts.spool_dir, "a", 3)
        artefacts.passes = [(PARTIAL_SUFFIX, records)]
        artefacts.document = records
        assembling = MagicMock()
        monkeypatch.setattr(preservation_module, "assemble_pdf", assembling)

        report = preserve_most_finished(artefacts)

        assembling.assert_not_called()
        page_dir = _only_page_dir(artefacts.failed_dir)
        assert page_dir.name.endswith(_NAME_TAIL)
        assert sorted(entry.name for entry in page_dir.iterdir()) == [
            "a-0001.png",
            "a-0002.png",
            "a-0003.png",
        ]
        assert all(_mode(page) == _PRIVATE_FILE_MODE for page in page_dir.iterdir())
        assert report.kind is KeptKind.PAGE_FILES
        assert report.sentence() == (
            f"The 3 spooled page file(s) were preserved at {page_dir}"
        )

    def test_after_acquisition_the_unfiltered_document_is_one_pdf(
        self, tmp_path: Path
    ) -> None:
        """The document's records, in document order, become one PDF."""
        artefacts = _artefacts(tmp_path, RunStage.FILTERING)
        fronts = _spool(artefacts.spool_dir, "a", 2)
        backs = _spool(artefacts.spool_dir, "b", 2, first=2)
        # The interleave a two-sheet manual duplex produces: not name order.
        document = (fronts[0], backs[1], fronts[1], backs[0])
        artefacts.passes = [(FRONTS_SUFFIX, fronts), (BACKS_SUFFIX, backs)]
        artefacts.document = document
        expected = _streams_of(document)

        report = preserve_most_finished(artefacts)

        assert report.kind is KeptKind.DOCUMENT
        (kept,) = report.kept_paths
        assert kept.parent == artefacts.failed_dir
        assert kept.name.endswith(f"{_NAME_TAIL}.pdf")
        assert _mode(kept) == _PRIVATE_FILE_MODE
        assert embedded_streams(kept) == expected
        assert list(artefacts.failed_dir.iterdir()) == [kept]
        sentence = report.sentence()
        assert sentence is not None
        assert f"The 4 scanned page(s) were preserved at {kept}" in sentence

    def test_mid_acquisition_each_pass_is_a_pdf_with_the_backs_in_sheet_order(
        self, tmp_path: Path
    ) -> None:
        """
        The (backs) PDF runs in sheet order, the reverse of pass B's.

        Pass B scans the flipped stack, so its first page is the last sheet's
        back. Reversed, the (backs) PDF's page N is the back of the (fronts)
        PDF's page N.
        """
        artefacts = _artefacts(tmp_path, RunStage.ACQUIRING)
        fronts = _spool(artefacts.spool_dir, "a", 3)
        backs = _spool(artefacts.spool_dir, "b", 3, first=3)
        artefacts.passes = [(FRONTS_SUFFIX, fronts), (BACKS_SUFFIX, backs)]
        expected_fronts = _streams_of(fronts)
        expected_backs = _streams_of(list(reversed(backs)))

        report = preserve_most_finished(artefacts)

        assert report.kind is KeptKind.PASSES
        fronts_pdf, backs_pdf = report.kept_paths
        assert fronts_pdf.name.endswith(f"{_NAME_TAIL}-fronts.pdf")
        assert backs_pdf.name.endswith(f"{_NAME_TAIL}-backs.pdf")
        assert embedded_streams(fronts_pdf) == expected_fronts
        assert embedded_streams(backs_pdf) == expected_backs
        with pikepdf.open(backs_pdf) as pdf:
            assert str(pdf.docinfo["/Title"]) == f"{_TITLE} {BACKS_SUFFIX}"
        sentence = report.sentence()
        assert sentence is not None
        assert (
            f"The 6 page(s) scanned before the error were preserved at "
            f"{fronts_pdf}, {backs_pdf}"
        ) in sentence

    def test_a_long_title_still_keeps_both_halves_as_two_files(
        self, tmp_path: Path
    ) -> None:
        """
        A title past the file name's cap cannot make the halves share a name.

        Both halves are named in the same second, so if the half were only
        words inside the title, the cap would cut it off and the backs would
        land on the fronts' name.
        """
        artefacts = _artefacts(tmp_path, RunStage.ACQUIRING)
        artefacts.title = (
            "An Unusually Long Title For A Two Sided Stack Of Tax Paperwork 2026"
        )
        fronts = _spool(artefacts.spool_dir, "a", 2)
        backs = _spool(artefacts.spool_dir, "b", 2, first=2)
        artefacts.passes = [(FRONTS_SUFFIX, fronts), (BACKS_SUFFIX, backs)]

        report = preserve_most_finished(artefacts)

        fronts_pdf, backs_pdf = report.kept_paths
        assert fronts_pdf.name.endswith("-fronts.pdf"), fronts_pdf.name
        assert backs_pdf.name.endswith("-backs.pdf"), backs_pdf.name
        assert sorted(artefacts.failed_dir.iterdir()) == sorted([fronts_pdf, backs_pdf])
        assert embedded_streams(fronts_pdf) == _streams_of(fronts)
        assert embedded_streams(backs_pdf) == _streams_of(list(reversed(backs)))

    def test_a_file_already_in_failed_dir_is_never_replaced(
        self, tmp_path: Path
    ) -> None:
        """A name already taken in failed/ is left alone; the PDF takes the next."""
        artefacts = _artefacts(tmp_path, RunStage.DELIVERING)
        records = _spool(artefacts.spool_dir, "a", 1)
        pdf = preservation_module.assemble_pdf(
            records,
            artefacts.workspace / "out",
            filename="20260925-120000-job-pres-kept-scan.pdf",
            title=_TITLE,
        )
        artefacts.pdfs = [pdf]
        artefacts.failed_dir.mkdir(parents=True)
        earlier = artefacts.failed_dir / pdf.name
        earlier.write_bytes(b"an earlier scan")

        report = preserve_most_finished(artefacts)

        (kept,) = report.kept_paths
        assert kept.name == "20260925-120000-job-pres-kept-scan-2.pdf"
        assert earlier.read_bytes() == b"an earlier scan"
        assert embedded_streams(kept) == _streams_of(records)

    def test_a_part_way_pass_b_says_which_sheets_its_backs_are(
        self, tmp_path: Path
    ) -> None:
        """
        Two backs of five fronts are sheets 4 and 5, not sheets 1 and 2.

        Pass B feeds the flipped stack from its last sheet, so when it stops
        part way its reversed PDF starts at the back of fronts page 4, and an
        operator pairing the halves by page number needs telling.
        """
        artefacts = _artefacts(tmp_path, RunStage.ACQUIRING)
        fronts = _spool(artefacts.spool_dir, "a", 5)
        backs = _spool(artefacts.spool_dir, "b", 2, first=5)
        artefacts.passes = [(FRONTS_SUFFIX, fronts), (BACKS_SUFFIX, backs)]

        report = preserve_most_finished(artefacts)

        sentence = report.sentence()
        assert sentence is not None
        assert "if every sheet was fed exactly once" in sentence
        assert "the backs of the last 2 of the 5 sheets" in sentence
        assert f"{BACKS_SUFFIX} page 1 goes with {FRONTS_SUFFIX} page 4" in sentence

    @pytest.mark.parametrize(("fronts", "backs"), [(4, 2), (3, 3)])
    def test_an_unreadable_sheet_voids_the_page_pairing(
        self, tmp_path: Path, fronts: int, backs: int
    ) -> None:
        """
        A sheet a pass could not read shifts every later page of that pass.

        So no front can be named for any back, whatever the counts, and the
        caution says to pair the halves by content instead.
        """
        artefacts = _artefacts(tmp_path, RunStage.ACQUIRING)
        front_records = _spool(artefacts.spool_dir, "a", fronts)
        back_records = _spool(artefacts.spool_dir, "b", backs, first=fronts)
        artefacts.passes = [
            (FRONTS_SUFFIX, front_records),
            (BACKS_SUFFIX, back_records),
        ]
        artefacts.unreadable_sheets = 1

        report = preserve_most_finished(artefacts)

        (caution,) = report.cautions
        assert "1 sheet(s) could not be read" in caution
        assert "cannot be paired" in caution
        assert "goes with" not in caution

    def test_a_whole_pass_b_needs_no_pairing_caution(self, tmp_path: Path) -> None:
        """As many backs as fronts: nothing to explain."""
        artefacts = _artefacts(tmp_path, RunStage.ACQUIRING)
        fronts = _spool(artefacts.spool_dir, "a", 2)
        backs = _spool(artefacts.spool_dir, "b", 2, first=2)
        artefacts.passes = [(FRONTS_SUFFIX, fronts), (BACKS_SUFFIX, backs)]

        report = preserve_most_finished(artefacts)

        assert report.cautions == []

    def test_mid_acquisition_an_empty_pass_is_left_out(self, tmp_path: Path) -> None:
        """A pass that spooled nothing yields no zero-page PDF."""
        artefacts = _artefacts(tmp_path, RunStage.ACQUIRING)
        fronts = _spool(artefacts.spool_dir, "a", 2)
        artefacts.passes = [(FRONTS_SUFFIX, fronts), (BACKS_SUFFIX, ())]

        report = preserve_most_finished(artefacts)

        (kept,) = report.kept_paths
        assert kept.name.endswith(f"{_NAME_TAIL}-fronts.pdf")
        assert report.kind is KeptKind.PASSES

    def test_mid_acquisition_with_no_pages_keeps_nothing(self, tmp_path: Path) -> None:
        """No page existed, so there is nothing to say and failed/ is untouched."""
        artefacts = _artefacts(tmp_path, RunStage.ACQUIRING)
        artefacts.passes = [(PARTIAL_SUFFIX, ())]

        report = preserve_most_finished(artefacts)

        assert report.kind is KeptKind.NOTHING
        assert report.kept_paths == []
        assert report.sentence() is None
        assert not artefacts.failed_dir.exists()

    def test_after_delivery_nothing_is_kept(self, tmp_path: Path) -> None:
        """A delivered document needs no copy in failed/."""
        artefacts = _artefacts(tmp_path, RunStage.DELIVERED)
        records = _spool(artefacts.spool_dir, "a", 2)
        artefacts.document = records
        pdf = artefacts.workspace / "delivered.pdf"
        pdf.write_bytes(b"%PDF-delivered")
        artefacts.pdfs = [pdf]

        report = preserve_most_finished(artefacts)

        assert report.kind is KeptKind.NOTHING
        assert report.sentence() is None
        assert pdf.exists()
        assert not artefacts.failed_dir.exists()


class TestTheSentenceSaysWhatSurvived:
    """The report's sentence is composed from what was actually moved."""

    def test_a_document_that_will_not_build_falls_back_to_the_page_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pages are kept, and the reason the PDF is missing is given."""
        artefacts = _artefacts(tmp_path, RunStage.FILTERING)
        records = _spool(artefacts.spool_dir, "a", 3)
        artefacts.document = records
        monkeypatch.setattr(
            preservation_module,
            "assemble_pdf",
            _refusing_assembly("qpdf refused the document"),
        )

        report = preserve_most_finished(artefacts)

        page_dir = _only_page_dir(artefacts.failed_dir)
        assert len(list(page_dir.iterdir())) == 3
        assert report.kind is KeptKind.PAGE_FILES
        sentence = report.sentence()
        assert sentence is not None
        assert f"preserved at {page_dir}" in sentence
        assert "qpdf refused the document" in sentence
        assert "could NOT" not in sentence

    def test_a_partial_move_names_the_files_it_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two of five pages moved before the volume went away: say exactly that."""
        artefacts = _artefacts(tmp_path, RunStage.ASSEMBLING)
        _spool(artefacts.spool_dir, "a", 5)
        real_move = preservation_module.move_private
        moved: list[Path] = []

        def _two_then_fail(source: Path, destination: Path) -> None:
            """Move two pages for real, then fail on the third."""
            if len(moved) == 2:
                msg = "the volume went away"
                raise OSError(msg)
            real_move(source, destination)
            moved.append(destination)

        monkeypatch.setattr(preservation_module, "move_private", _two_then_fail)

        report = preserve_most_finished(artefacts)

        assert report.kind is KeptKind.PAGE_FILES
        assert report.kept_paths == moved
        assert all(path.exists() for path in moved)
        sentence = report.sentence()
        assert sentence is not None
        assert f"preserved at {moved[0]}, {moved[1]}" in sentence
        assert "2 of the 5" in sentence
        assert "the volume went away" in sentence
        assert "could NOT" not in sentence

    def test_a_half_kept_duplex_names_the_half_and_keeps_the_pages(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fronts built, the backs did not: both facts, and the page files."""
        artefacts = _artefacts(tmp_path, RunStage.ACQUIRING)
        fronts = _spool(artefacts.spool_dir, "a", 2)
        backs = _spool(artefacts.spool_dir, "b", 1, first=2)
        artefacts.passes = [(FRONTS_SUFFIX, fronts), (BACKS_SUFFIX, backs)]
        real_assemble = preservation_module.assemble_pdf
        calls = 0

        def _fronts_then_refuse(
            records: tuple[PageRecord, ...],
            output_dir: Path,
            filename: str,
            *,
            title: str,
        ) -> Path:
            """Assemble the fronts for real, then refuse the backs."""
            nonlocal calls
            calls += 1
            if calls == 1:
                return real_assemble(records, output_dir, filename, title=title)
            msg = "qpdf refused the backs"
            raise PdfError(msg)

        monkeypatch.setattr(preservation_module, "assemble_pdf", _fronts_then_refuse)

        report = preserve_most_finished(artefacts)

        fronts_pdf, page_dir = report.kept_paths
        assert fronts_pdf.name.endswith("fronts.pdf")
        assert page_dir == _only_page_dir(artefacts.failed_dir)
        assert report.kind is KeptKind.PASSES
        sentence = report.sentence()
        assert sentence is not None
        assert f"preserved at {fronts_pdf}" in sentence
        assert f"preserved at {page_dir}" in sentence
        assert "qpdf refused the backs" in sentence
        assert "could NOT" not in sentence

    def test_when_nothing_survives_the_sentence_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one case that may say "could NOT", with the reason."""
        artefacts = _artefacts(tmp_path, RunStage.FILTERING)
        artefacts.document = _spool(artefacts.spool_dir, "a", 2)

        def _read_only(_failed_dir: Path) -> None:
            msg = "Read-only file system"
            raise OSError(msg)

        monkeypatch.setattr(preservation_module, "make_failed_dir", _read_only)

        report = preserve_most_finished(artefacts)

        assert report.kind is KeptKind.NOTHING
        assert report.kept_paths == []
        sentence = report.sentence()
        assert sentence is not None
        assert sentence.startswith(
            f"The scan could NOT be preserved to {artefacts.failed_dir}: "
        )
        assert "Read-only file system" in sentence
        assert "preserved at " not in sentence


class TestPreservationNeverRaises:
    """A failure while preserving is reported, never raised over the run's own."""

    @pytest.mark.parametrize(
        ("stage", "target", "error"),
        [
            *(
                (stage, target, error)
                for stage in (RunStage.ACQUIRING, RunStage.FILTERING)
                for target, error in (
                    ("assemble_pdf", RuntimeError("a bug in assembly")),
                    ("move_private", ValueError("a bug in the move")),
                    ("best_effort_chmod", TypeError("a bug in the chmod")),
                    ("make_failed_dir", KeyError("a bug in the mkdir")),
                    (
                        "ensure_room_to_assemble",
                        AttributeError("a bug in the disk rule"),
                    ),
                )
            ),
            # Moving what is already assembled builds nothing, so only the
            # move's own collaborators can fail there.
            (RunStage.DELIVERING, "move_private", ValueError("a bug in the move")),
            (RunStage.DELIVERING, "best_effort_chmod", TypeError("a chmod bug")),
            (RunStage.DELIVERING, "make_failed_dir", KeyError("a bug in the mkdir")),
            (RunStage.ASSEMBLING, "move_private", ValueError("a bug in the move")),
        ],
    )
    def test_any_exception_becomes_part_of_the_report(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        target: str,
        error: Exception,
        stage: RunStage,
    ) -> None:
        """Whatever is injected, a report comes back naming the failure."""
        artefacts = _artefacts(tmp_path, stage)
        records = _spool(artefacts.spool_dir, "a", 2)
        artefacts.passes = [(PARTIAL_SUFFIX, records)]
        if stage is not RunStage.ACQUIRING:
            artefacts.document = records
        if stage is RunStage.DELIVERING:
            pdf = artefacts.workspace / "assembled.pdf"
            pdf.write_bytes(b"%PDF-assembled")
            artefacts.pdfs = [pdf]
        monkeypatch.setattr(preservation_module, target, MagicMock(side_effect=error))

        report = preserve_most_finished(artefacts)

        assert isinstance(report, PreservationReport)
        sentence = report.sentence()
        assert sentence is not None
        assert str(error.args[0]) in sentence

    @pytest.mark.parametrize(
        "interruption",
        [KeyboardInterrupt(), ScanInterrupted("The server is stopping")],
    )
    def test_an_interruption_passes_straight_through(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        interruption: BaseException,
    ) -> None:
        """Ctrl-C and a signal are not failures to fold into a report."""
        artefacts = _artefacts(tmp_path, RunStage.ASSEMBLING)
        _spool(artefacts.spool_dir, "a", 2)
        monkeypatch.setattr(
            preservation_module, "move_private", MagicMock(side_effect=interruption)
        )

        with pytest.raises(type(interruption)):
            preserve_most_finished(artefacts)


class TestTheTwiceTheSpoolRule:
    """Assembly needs twice the spooled pages free, plus the reserve."""

    def _need(self, records: tuple[PageRecord, ...], reserve_mb: int) -> int:
        """Return the bytes the rule asks for."""
        spooled = sum(record.path.stat().st_size for record in records)
        return 2 * spooled + reserve_mb * _MIB

    def test_one_byte_short_is_refused_with_the_numbers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The refusal names the MB needed, the MB free and the page count."""
        records = _spool(tmp_path / "spool", "a", 3)
        need = self._need(records, 10)
        monkeypatch.setattr(preservation_module, "_free_bytes", lambda _d: need - 1)

        with pytest.raises(PdfError) as excinfo:
            ensure_room_to_assemble(records, tmp_path, reserve_mb=10)

        message = str(excinfo.value)
        assert f"{math.ceil(need / _MIB)} MB needed" in message
        assert f"{(need - 1) // _MIB} MB free" in message
        assert "3 page(s)" in message
        assert str(tmp_path) in message
        assert "min_free_space_mb" in message

    def test_exactly_the_need_is_enough(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rule is "under the need", so the need itself passes."""
        records = _spool(tmp_path / "spool", "a", 3)
        need = self._need(records, 10)
        monkeypatch.setattr(preservation_module, "_free_bytes", lambda _d: need)

        assert ensure_room_to_assemble(records, tmp_path, reserve_mb=10) is None

    def test_a_page_that_cannot_be_measured_is_a_pdf_error(
        self, tmp_path: Path
    ) -> None:
        """A vanished page file is refused as an assembly failure, not an OSError."""
        records = _spool(tmp_path / "spool", "a", 2)
        records[1].path.unlink()

        with pytest.raises(PdfError, match=r"a-0002\.png"):
            ensure_room_to_assemble(records, tmp_path, reserve_mb=0)

    def test_preservation_refused_by_the_rule_keeps_the_page_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No PDF is attempted without the room; the pages are kept instead."""
        artefacts = _artefacts(tmp_path, RunStage.FILTERING)
        artefacts.document = _spool(artefacts.spool_dir, "a", 3)
        monkeypatch.setattr(preservation_module, "_free_bytes", lambda _d: 0)
        assembling = MagicMock()
        monkeypatch.setattr(preservation_module, "assemble_pdf", assembling)

        report = preserve_most_finished(artefacts)

        assembling.assert_not_called()
        page_dir = _only_page_dir(artefacts.failed_dir)
        assert len(list(page_dir.iterdir())) == 3
        assert report.kind is KeptKind.PAGE_FILES
        sentence = report.sentence()
        assert sentence is not None
        assert f"The 3 spooled page file(s) were preserved at {page_dir}" in sentence
        assert "MB needed" in sentence


class TestTheFailedDirWarningMovedHere:
    """The growth warning lives with the rest of preservation."""

    def test_the_warning_is_logged_by_the_preservation_module(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """At the threshold one WARNING names the directory."""
        failed_dir = tmp_path / "failed"
        failed_dir.mkdir()
        for index in range(FAILED_DIR_WARN_THRESHOLD):
            (failed_dir / f"old-{index:02d}.pdf").write_bytes(b"%PDF-old")

        with caplog.at_level(logging.WARNING, logger="saneless.preservation"):
            warn_if_failed_dir_growing(failed_dir)

        messages = [
            record.getMessage()
            for record in caplog.records
            if record.name == "saneless.preservation"
        ]
        assert len(messages) == 1
        assert str(failed_dir) in messages[0]

    def test_preserving_warns_once_when_failed_dir_is_crowded(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One preservation that crosses the threshold warns once."""
        artefacts = _artefacts(tmp_path, RunStage.ACQUIRING)
        artefacts.failed_dir.mkdir(parents=True)
        for index in range(FAILED_DIR_WARN_THRESHOLD - 1):
            (artefacts.failed_dir / f"old-{index:02d}.pdf").write_bytes(b"%PDF-old")
        fronts = _spool(artefacts.spool_dir, "a", 1)
        backs = _spool(artefacts.spool_dir, "b", 1, first=1)
        artefacts.passes = [(FRONTS_SUFFIX, fronts), (BACKS_SUFFIX, backs)]

        with caplog.at_level(logging.WARNING, logger="saneless.preservation"):
            preserve_most_finished(artefacts)

        growth = [
            record.getMessage()
            for record in caplog.records
            if "accumulated" in record.getMessage()
        ]
        assert len(growth) == 1
        assert str(FAILED_DIR_WARN_THRESHOLD + 1) in growth[0]


class TestMovePrivateNeverReplaces:
    """Whatever route the move takes, a file already there is left alone."""

    @pytest.mark.parametrize("crossing", [False, True], ids=["link", "copy"])
    def test_an_existing_destination_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, crossing: bool
    ) -> None:
        """The move refuses, and neither file is touched."""
        if crossing:

            def cross_device(_src: object, _dst: object) -> None:
                raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))

            monkeypatch.setattr(os, "link", cross_device)
        source = tmp_path / "work" / "scan.pdf"
        source.parent.mkdir()
        source.write_bytes(b"the new scan")
        destination = tmp_path / "failed" / "scan.pdf"
        destination.parent.mkdir()
        destination.write_bytes(b"an earlier scan")

        with pytest.raises(FileExistsError):
            preservation_module.move_private(source, destination)

        assert destination.read_bytes() == b"an earlier scan"
        assert source.read_bytes() == b"the new scan"

    @pytest.mark.parametrize("crossing", [False, True], ids=["link", "copy"])
    def test_a_free_destination_is_moved_to(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, crossing: bool
    ) -> None:
        """Either route leaves the file at the destination and not at the source."""
        if crossing:

            def cross_device(_src: object, _dst: object) -> None:
                raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))

            monkeypatch.setattr(os, "link", cross_device)
        source = tmp_path / "scan.pdf"
        source.write_bytes(b"the scan")
        destination = tmp_path / "failed-scan.pdf"

        preservation_module.move_private(source, destination)

        assert destination.read_bytes() == b"the scan"
        assert not source.exists()
