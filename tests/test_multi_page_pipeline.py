"""
The pipeline's contracts for a multi-page document, before the loop uses them.

These tests pin the names the multi-page loop, the worker and the CLI build
against: the claim-once answer slot both kinds of wait share, the pass
coordinator seam, the three waiting events, the two request fields, the spool
ledger's accept and discard operations, and the document page cap.  They also
check the test doubles the later tests drive the loop with, so a failure there
is reported as the double's fault and not the pipeline's.

No test here sleeps: a wait on an unanswered slot is given ``0``.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

import pytest
from PIL import Image

import saneless.pipeline as pipeline_module
from saneless.config import ProfileConfig
from saneless.exceptions import ScanError
from saneless.pages import is_blank
from saneless.pipeline import (
    AnswerSlot,
    FlipAnswerSlot,
    PassCoordinator,
    PipelineEvent,
    PipelineRequest,
    _SpoolLedger,
    _wait_event,
)
from saneless.scanner.base import MAX_PAGES_PER_PASS, ScanSettings
from saneless.spool import SpooledPageSink
from saneless.vocabulary import (
    FlipOutcome,
    JobState,
    PassAnswer,
    PassPrompt,
    PassWait,
    pass_wait_state,
)
from tests.golden_support import DistinctPageScanner
from tests.multi_page_support import ScriptedPassCoordinator

if TYPE_CHECKING:
    from enum import StrEnum
    from pathlib import Path

# The free space a test sink insists on, in MiB: small enough for any runner.
_RESERVE_MB = 1

_SETTINGS = ScanSettings(source="Flatbed", resolution=100, mode="Gray")


def _prompt(offered: frozenset[PassAnswer], number: int = 1) -> PassPrompt:
    """
    Build a next-pass prompt offering ``offered``.

    Args:
        offered: The answers the prompt accepts.
        number: The prompt's sequence number within the run.

    Returns:
        A prompt with nothing else of interest in it.

    """
    return PassPrompt(
        number=number,
        wait=PassWait.NEXT_PASS,
        pages_kept=1,
        offered=offered,
        timeout_seconds=0,
    )


def _sink_with_pages(directory: Path, label: str, count: int) -> SpooledPageSink:
    """
    Spool ``count`` small pages into a fresh sink.

    Args:
        directory: The spool directory.
        label: The sink's per-pass file-name prefix.
        count: How many pages to spool.

    Returns:
        The sink, holding ``count`` records whose files exist.

    """
    sink = SpooledPageSink(directory, label, _RESERVE_MB)
    for _ in range(count):
        with Image.new("L", (40, 60), 0) as page:
            sink.add(page, dpi=100)
    return sink


_BOTH_ENUMS = pytest.mark.parametrize(
    ("first", "second"),
    [
        (FlipOutcome.CONTINUED, FlipOutcome.ABORTED),
        (PassAnswer.NEXT, PassAnswer.FINISH),
    ],
    ids=["flip", "pass"],
)


class TestAnswerSlot:
    """The claim-once slot, over the flip wait's answers and a pass wait's."""

    @_BOTH_ENUMS
    def test_the_first_offer_claims_and_a_later_one_is_dropped(
        self, first: StrEnum, second: StrEnum
    ) -> None:
        """The first offer is the answer; a later offer loses and changes nothing."""
        slot: AnswerSlot[StrEnum] = AnswerSlot()
        assert slot.offer(first) is True
        assert slot.offer(second) is False
        assert slot.answer is first

    @_BOTH_ENUMS
    def test_settle_claims_an_unanswered_slot(
        self, first: StrEnum, second: StrEnum
    ) -> None:
        """Settling an unanswered slot makes its argument the answer."""
        slot: AnswerSlot[StrEnum] = AnswerSlot()
        assert slot.settle(first) is first
        assert slot.answer is first
        assert second is not first

    @_BOTH_ENUMS
    def test_settle_after_an_offer_returns_the_earlier_answer(
        self, first: StrEnum, second: StrEnum
    ) -> None:
        """A settle that loses the race hands back the answer that beat it."""
        slot: AnswerSlot[StrEnum] = AnswerSlot()
        slot.offer(first)
        assert slot.settle(second) is first
        assert slot.answer is first

    @_BOTH_ENUMS
    def test_a_zero_wait_on_an_unanswered_slot_claims_nothing(
        self, first: StrEnum, second: StrEnum
    ) -> None:
        """``wait(0)`` returns without an answer, and the slot can still be claimed."""
        slot: AnswerSlot[StrEnum] = AnswerSlot()
        slot.wait(0)
        assert slot.answer is None
        assert slot.offer(first) is True
        assert slot.answer is not second

    def test_the_flip_slot_is_the_shared_slot(self) -> None:
        """The flip wait's slot keeps its name and is the shared slot underneath."""
        assert issubclass(FlipAnswerSlot, AnswerSlot)
        slot = FlipAnswerSlot()
        assert slot.offer(FlipOutcome.CONTINUED) is True
        assert slot.answer is FlipOutcome.CONTINUED

    def test_the_slot_is_public_api(self) -> None:
        """``AnswerSlot`` is exported beside the flip slot."""
        assert "AnswerSlot" in pipeline_module.__all__
        assert "FlipAnswerSlot" in pipeline_module.__all__


class _AlwaysFinish(PassCoordinator):
    """The smallest pass coordinator: implements ``ask`` and nothing else."""

    def ask(self, prompt: PassPrompt) -> PassAnswer:
        """
        Finish the document whatever was asked.

        Args:
            prompt: Ignored.

        Returns:
            Always ``PassAnswer.FINISH``.

        """
        del prompt
        return PassAnswer.FINISH


class TestPassCoordinatorContract:
    """The seam a multi-page run waits on between passes."""

    def test_ask_is_the_only_abstract_method(self) -> None:
        """
        ``ask`` is abstract, and is the only thing a coordinator must write.

        Checked through the class's own record of what is abstract rather
        than by calling it, which the type checkers rightly refuse to allow.
        """
        assert inspect.isabstract(PassCoordinator)
        assert PassCoordinator.__abstractmethods__ == frozenset({"ask"})

    def test_a_minimal_coordinator_has_no_abort_cause(self) -> None:
        """``abort_cause`` is concrete and ``None``, so implementing ``ask`` is enough."""
        coordinator = _AlwaysFinish()
        prompt = _prompt(frozenset({PassAnswer.FINISH}))
        assert coordinator.ask(prompt) is PassAnswer.FINISH
        assert coordinator.abort_cause is None

    def test_the_seam_is_public_api(self) -> None:
        """``PassCoordinator`` is exported beside ``FlipCoordinator``."""
        assert "PassCoordinator" in pipeline_module.__all__


class TestMultiPageEventsAndRequest:
    """The three waiting events, and a request's two new, defaulted fields."""

    @pytest.mark.parametrize(
        "name", ["AWAITING_NEXT_PASS", "AWAITING_BLANK_DECISION", "AWAITING_RETRY"]
    )
    def test_each_waiting_event_projects_to_its_namesake(self, name: str) -> None:
        """A waiting event persists the job state of the same name."""
        assert PipelineEvent(name).job_state is JobState(name)

    @pytest.mark.parametrize("wait", list(PassWait))
    def test_each_wait_has_exactly_its_own_event(self, wait: PassWait) -> None:
        """A wait's event persists the same state the wait itself names."""
        assert _wait_event(wait).job_state is pass_wait_state(wait)

    def test_the_three_waits_have_three_distinct_events(self) -> None:
        """No two waits share an event, so the job always says which is open."""
        assert len({_wait_event(wait) for wait in PassWait}) == len(PassWait)

    def test_a_request_is_single_pass_by_default(self) -> None:
        """Every existing construction stays a single-pass scan with no coordinator."""
        request = PipelineRequest(profile_name="p", title="t")
        assert request.multi_page is False
        assert request.pass_coordinator is None


class TestSpoolLedgerAcceptAndDiscard:
    """What happens to a pass's page files when the pass is kept or thrown away."""

    def test_discard_removes_the_files_and_the_pass(self, tmp_path: Path) -> None:
        """A discarded pass leaves no page file behind and no entry to preserve."""
        sink = _sink_with_pages(tmp_path, "a", 2)
        paths = [record.path for record in sink.records]
        ledger = _SpoolLedger()
        ledger.register("(partial)", sink)

        ledger.discard(sink)

        assert [path.exists() for path in paths] == [False, False]
        assert ledger.spooled() == []
        assert ledger.page_count() == 0

    def test_forget_keeps_the_files_and_drops_the_pass(self, tmp_path: Path) -> None:
        """An accepted pass's pages stay on disk; the ledger just stops tracking it."""
        sink = _sink_with_pages(tmp_path, "a", 2)
        paths = [record.path for record in sink.records]
        ledger = _SpoolLedger()
        ledger.register("(partial)", sink)

        ledger.forget(sink)

        assert [path.exists() for path in paths] == [True, True]
        assert ledger.spooled() == []

    @pytest.mark.parametrize("operation", ["discard", "forget"])
    def test_another_pass_is_untouched(self, tmp_path: Path, operation: str) -> None:
        """Either operation affects only the sink it names."""
        target = _sink_with_pages(tmp_path, "a", 2)
        other = _sink_with_pages(tmp_path, "b", 1)
        other_paths = [record.path for record in other.records]
        ledger = _SpoolLedger()
        ledger.register("(partial)", target)
        ledger.register("(partial)", other)

        getattr(ledger, operation)(target)

        assert ledger.spooled() == [("(partial)", other.records)]
        assert all(path.exists() for path in other_paths)

    def test_discard_tolerates_a_page_already_gone(self, tmp_path: Path) -> None:
        """A page file that is already missing does not stop the rest going."""
        sink = _sink_with_pages(tmp_path, "a", 2)
        first, second = (record.path for record in sink.records)
        first.unlink()
        ledger = _SpoolLedger()
        ledger.register("(partial)", sink)

        ledger.discard(sink)

        assert not second.exists()
        assert ledger.spooled() == []


class TestDocumentCapPairing:
    """The largest document and the largest pass are one number."""

    def test_the_document_cap_is_the_per_pass_cap(self) -> None:
        """Both caps are 500 pages, and the document's is defined by the pass's."""
        assert pipeline_module.MAX_DOCUMENT_PAGES == MAX_PAGES_PER_PASS == 500
        assert "MAX_DOCUMENT_PAGES" in pipeline_module.__all__


class TestMultiPageTestDoubles:
    """The doubles the multi-page tests drive the loop with do what they say."""

    def test_the_scripted_coordinator_records_and_answers(self) -> None:
        """It keeps the prompt it was asked and returns the scripted answer."""
        seen: list[PassPrompt] = []
        coordinator = ScriptedPassCoordinator([PassAnswer.NEXT], on_ask=seen.append)
        prompt = _prompt(frozenset({PassAnswer.NEXT, PassAnswer.FINISH}))

        assert coordinator.ask(prompt) is PassAnswer.NEXT
        assert coordinator.prompts == [prompt]
        assert seen == [prompt]

    def test_an_exhausted_script_fails_loudly(self) -> None:
        """A question the script has no answer for is the test's mistake."""
        coordinator = ScriptedPassCoordinator([PassAnswer.NEXT])
        prompt = _prompt(frozenset({PassAnswer.NEXT}))
        coordinator.ask(prompt)
        with pytest.raises(AssertionError, match="ran out"):
            coordinator.ask(_prompt(frozenset({PassAnswer.NEXT}), number=2))

    def test_an_answer_the_prompt_did_not_offer_fails_loudly(self) -> None:
        """A scripted operator answer outside ``offered`` is refused."""
        coordinator = ScriptedPassCoordinator([PassAnswer.FINISH])
        with pytest.raises(AssertionError, match="offered only"):
            coordinator.ask(_prompt(frozenset({PassAnswer.NEXT})))

    @pytest.mark.parametrize("answer", [PassAnswer.TIMED_OUT, PassAnswer.INTERRUPTED])
    def test_the_clock_and_a_stop_need_no_offer(self, answer: PassAnswer) -> None:
        """No prompt offers a timeout or an interruption, yet both can end a wait."""
        coordinator = ScriptedPassCoordinator([answer])
        assert coordinator.ask(_prompt(frozenset({PassAnswer.NEXT}))) is answer

    def test_the_abort_cause_follows_an_abort(self) -> None:
        """The scripted cause is reported once the coordinator has answered ABORT."""
        cause = OSError("the terminal went away")
        coordinator = ScriptedPassCoordinator(
            [PassAnswer.NEXT, PassAnswer.ABORT], cause=cause
        )
        offered = frozenset({PassAnswer.NEXT, PassAnswer.ABORT})
        coordinator.ask(_prompt(offered))
        assert coordinator.abort_cause is None
        coordinator.ask(_prompt(offered, number=2))
        assert coordinator.abort_cause is cause

    def test_a_scanner_fails_after_spooling_its_pass(self, tmp_path: Path) -> None:
        """``fail_on`` raises from the named call, after its pages reached the sink."""
        jam = ScanError("jam")
        scanner = DistinctPageScanner(passes=((0,), (1,)), fail_on={2: jam})
        first = SpooledPageSink(tmp_path, "a", _RESERVE_MB)
        second = SpooledPageSink(tmp_path, "b", _RESERVE_MB)

        scanner.scan_pages("dev", _SETTINGS, first)
        with pytest.raises(ScanError) as raised:
            scanner.scan_pages("dev", _SETTINGS, second)

        assert raised.value is jam
        assert len(second.records) == 1
        assert second.records[0].path.exists()
        assert sorted(scanner.spooled) == [0, 1]

    def test_a_scanner_spools_a_blank_page(self, tmp_path: Path) -> None:
        """A page named in ``blank`` measures blank at the default profile threshold."""
        threshold = ProfileConfig.model_fields["empty_page_coverage_threshold"].default
        scanner = DistinctPageScanner(passes=((0, 1),), blank={1})
        sink = SpooledPageSink(tmp_path, "a", _RESERVE_MB)

        scanner.scan_pages("dev", _SETTINGS, sink)

        inked, blank = sink.records
        assert not is_blank(
            inked.ink_coverage, inked.paper_white, coverage_threshold=threshold
        )
        assert is_blank(
            blank.ink_coverage, blank.paper_white, coverage_threshold=threshold
        )
