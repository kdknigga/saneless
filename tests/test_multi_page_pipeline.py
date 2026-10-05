"""
The pipeline's multi-page document: its contracts, and the pass loop itself.

The first half pins the names the multi-page loop, the worker and the CLI build
against: the claim-once answer slot both kinds of wait share, the pass
coordinator seam, the three waiting events, the two request fields, the spool
ledger's accept and discard operations, and the document page cap.  It also
checks the test doubles the loop is driven with, so a failure there is reported
as the double's fault and not the pipeline's.

The second half drives whole runs through ``run_pipeline``: which requests are
refused before the scanner is touched, and how a run of passes becomes one
document -- its page order, the questions asked between passes, and every way
the loop can end, the page cap and a pass that fails part way included -- and
how the operator decides, once per pass, about the pages that look blank.

No test here sleeps: a wait on an unanswered slot is given ``0``, and every
operator answer comes from a script.
"""

from __future__ import annotations

import dataclasses
import inspect
import io
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, override
from unittest.mock import MagicMock

import pytest
from PIL import Image

import saneless.flip as flip_module
import saneless.pipeline as pipeline_module
from saneless.config import ProfileConfig
from saneless.exceptions import (
    AllPagesBlankError,
    ConfigError,
    DiskSpaceError,
    FeederEmptyError,
    ListingCrashedError,
    PaperlessError,
    ScanCancelledError,
    ScanError,
    ScanInterrupted,
    SpoolError,
    failure_text,
)
from saneless.flip import AnswerSlot, FlipAnswerSlot, PassCoordinator
from saneless.pages import generate_thumbnail, is_blank
from saneless.paperless import ApiDelivery, PaperlessClient, TaskFiled, UploadResult
from saneless.pipeline import (
    PipelineEvent,
    PipelineRequest,
    _rejected_pages_warning,
    _SpoolLedger,
    _wait_event,
    run_pipeline,
)
from saneless.scanner.base import (
    MAX_PAGES_PER_PASS,
    PassCapReached,
    ScannerBackend,
    ScanSettings,
)
from saneless.spool import SpooledPageSink
from saneless.vocabulary import (
    ErrorCategory,
    FlipOutcome,
    JobState,
    PassAnswer,
    PassPrompt,
    PassWait,
    ScanOutcome,
    blank_timeout_finish_warning,
    cap_finish_warning,
    classify_error,
    pass_cap_note,
    pass_cap_warning,
    pass_wait_state,
    substituted_source_warning,
    timeout_finish_warning,
)
from tests.conftest import FIXED_JOB_ID, AlwaysContinueFlipCoordinator
from tests.golden_support import DistinctPageScanner, embedded_streams, png_idat
from tests.multi_page_support import (
    DUPLEX_PROFILE,
    ScriptedPassCoordinator,
    multi_page_settings,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from enum import StrEnum
    from pathlib import Path

    from saneless.config import Settings
    from saneless.pipeline import ScanResult
    from saneless.scanner.base import PageSink, ScanBatch


# The free space a test sink insists on, in MB: small enough for any runner.
_RESERVE_MB = 1

_SETTINGS = ScanSettings(source="Flatbed", resolution=100, mode="Gray")


def _assert_terminal_next_step(error: ScanError) -> None:
    """
    Check that a broken prompt's next step points at the terminal.

    The scanner category's advice would send the operator to a scanner that
    did nothing wrong, so the raise site carries its own.
    """
    next_step = error.next_step
    assert next_step is not None
    assert "terminal" in next_step
    assert "scanner" not in next_step
    assert "tty gone" not in next_step


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
        assert "AnswerSlot" in flip_module.__all__
        assert "FlipAnswerSlot" in flip_module.__all__


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

        Checked through the class's own record of what is abstract, because
        calling an abstract method is a type error.
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
        assert "PassCoordinator" in flip_module.__all__


class TestMultiPageEventsAndRequest:
    """The three waiting events, and a request's two defaulted fields."""

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
        """A request built without either field is a single-pass scan, no coordinator."""
        request = PipelineRequest(profile_name="p", title="t", job_id=FIXED_JOB_ID)
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

    def test_a_signal_while_discarding_never_leaves_a_pass_without_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The ledger lets go of the pass before its files are deleted.

        A signal raises wherever the main thread is; landing as the files go,
        it must leave at worst orphan files, never a ledger entry the guard
        would try to preserve from pages already deleted.
        """
        sink = _sink_with_pages(tmp_path, "a", 2)
        ledger = _SpoolLedger()
        ledger.register("(partial)", sink)
        tracked_while_deleting: list[bool] = []

        def _interrupted_unlink(records: Sequence[object]) -> None:
            del records
            tracked_while_deleting.append(bool(ledger.spooled()))
            msg = "signal"
            raise ScanInterrupted(msg)

        monkeypatch.setattr(pipeline_module, "_unlink_pages", _interrupted_unlink)

        with pytest.raises(ScanInterrupted):
            ledger.discard(sink)

        assert tracked_while_deleting == [False]
        assert ledger.spooled() == []

    def test_a_page_that_cannot_be_deleted_is_logged_and_the_rest_go(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        A discarded page left behind is named in the log, with its cause.

        It would come back in ``failed/`` beside some later failure, so the
        log has to say where it came from.
        """
        sink = _sink_with_pages(tmp_path, "a", 2)
        stuck, second = (record.path for record in sink.records)
        ledger = _SpoolLedger()
        ledger.register("(partial)", sink)
        real_unlink = type(stuck).unlink

        def _refuse_one(path: Path, *, missing_ok: bool = False) -> None:
            if path == stuck:
                msg = "read-only file system"
                raise OSError(msg)
            real_unlink(path, missing_ok=missing_ok)

        monkeypatch.setattr(type(stuck), "unlink", _refuse_one)

        with caplog.at_level(logging.WARNING, logger="saneless.pipeline"):
            ledger.discard(sink)

        assert stuck.exists()
        assert not second.exists()
        (record,) = [r for r in caplog.records if stuck.name in r.getMessage()]
        assert record.levelno == logging.WARNING
        assert "failed/" in record.getMessage()
        assert record.exc_info is not None

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


# The answers every next-pass prompt offers once a page is kept.
_EVERY_NEXT_PASS_ANSWER = frozenset(
    {PassAnswer.NEXT, PassAnswer.RESCAN, PassAnswer.FINISH, PassAnswer.ABORT}
)

# The operator-wait bound ``multi_page_settings`` configures by default.
_TIMEOUT = 600

_NEXT = PassAnswer.NEXT
_RESCAN = PassAnswer.RESCAN
_FINISH = PassAnswer.FINISH
_ABORT = PassAnswer.ABORT


def _uploading_paperless(uploads: list[bytes]) -> MagicMock:
    """
    Build a paperless-ngx client that accepts every upload and keeps its PDF.

    The PDF is read the moment it is uploaded, because it lives in the job
    workspace and that is deleted when ``run_pipeline`` returns.

    Args:
        uploads: Where each uploaded PDF's bytes are appended, in order.

    Returns:
        A client stand-in whose uploads reach the API and whose polls succeed.

    """
    paperless = MagicMock(spec=PaperlessClient)

    def _upload(
        pdf_path: Path, title: str, tags: object, correspondent: object
    ) -> UploadResult:
        del title, tags, correspondent
        uploads.append(pdf_path.read_bytes())
        return ApiDelivery(task_id=f"task-{len(uploads)}")

    paperless.upload_document.side_effect = _upload
    paperless.poll_task.return_value = TaskFiled(task={"status": "SUCCESS"})
    return paperless


@dataclass
class _Rig:
    """
    One multi-page run's settings, and everything it was seen to do.

    Attributes:
        settings: The settings the run uses.
        uploads: Every uploaded PDF, in order.
        events: Every status event the run reported, in order.
        thumbnails: Every thumbnail the run handed its observer.
        paperless: The client stand-in that records ``uploads``.

    """

    settings: Settings
    uploads: list[bytes] = field(default_factory=list)
    events: list[PipelineEvent] = field(default_factory=list)
    thumbnails: list[str] = field(default_factory=list)
    paperless: MagicMock = field(init=False)

    def __post_init__(self) -> None:
        """Build the recording client."""
        self.paperless = _uploading_paperless(self.uploads)

    def run(
        self,
        scanner: ScannerBackend,
        coordinator: PassCoordinator | None,
        *,
        profile: str = "default",
        multi_page: bool = True,
    ) -> ScanResult:
        """
        Run the pipeline once, as a multi-page scan unless told otherwise.

        The request always carries a flip coordinator as well, so a refusal
        on a manual-duplex profile can only be the multi-page one.

        Args:
            scanner: The scanner the run feeds from.
            coordinator: The pass coordinator the request carries, or None.
            profile: The profile to scan with.
            multi_page: Whether the request asks for a multi-page scan.

        Returns:
            How the run resolved.

        """
        return run_pipeline(
            scanner=scanner,
            paperless=self.paperless,
            settings=self.settings,
            request=PipelineRequest(
                profile_name=profile,
                title="Multi Page",
                job_id="job-multi-page",
                status_callback=self.events.append,
                thumbnail_callback=self.thumbnails.append,
                flip_coordinator=AlwaysContinueFlipCoordinator(),
                multi_page=multi_page,
                pass_coordinator=coordinator,
            ),
        )

    def spool_names(self) -> list[str]:
        """
        List the front-pass page files on the spool right now, by name.

        Returns:
            The file names, in the order the directory listing gave them.

        """
        return [path.name for path in self.settings.output.tmp_dir.rglob("a-*.png")]

    def kept_pdfs(self) -> list[Path]:
        """
        List the PDFs the guard kept in ``failed/``.

        Returns:
            Every kept PDF, sorted by name; empty when nothing was kept.

        """
        failed_dir = self.settings.output.failed_dir
        if not failed_dir.exists():
            return []
        return sorted(failed_dir.glob("*.pdf"))

    def kept_document(self) -> Path:
        """
        Return the one kept PDF that is not a pass in flight.

        Returns:
            The kept document's path.

        """
        (document,) = [pdf for pdf in self.kept_pdfs() if "partial" not in pdf.name]
        return document

    def kept_partials(self) -> list[Path]:
        """
        Return the kept PDFs of passes that were in flight.

        Returns:
            Every kept ``(partial)`` PDF, sorted by name.

        """
        return [pdf for pdf in self.kept_pdfs() if "partial" in pdf.name]


@pytest.fixture
def rig(tmp_path: Path) -> _Rig:
    """
    Return a multi-page rig over a flatbed profile with detection off.

    Returns:
        The rig.

    """
    return _Rig(multi_page_settings(tmp_path))


def _pages(scanner: DistinctPageScanner, indices: Sequence[int]) -> list[bytes]:
    """
    Return what a PDF holding exactly these spooled pages, in this order, embeds.

    Args:
        scanner: The scanner that spooled them.
        indices: The page indices, in document order.

    Returns:
        One compressed pixel stream per page, comparable with
        ``embedded_streams``.

    """
    return [png_idat(scanner.spooled[index]) for index in indices]


class _ReinitialiseSpy(DistinctPageScanner):
    """A distinct-page scanner that logs each restart and each pass, in order."""

    def __init__(self, *, passes: Sequence[Sequence[int]]) -> None:
        """
        Prepare the passes and an empty log.

        Args:
            passes: The page indices each successive call feeds.

        """
        super().__init__(passes=passes)
        self.log: list[str] = []

    @override
    def reinitialise(self) -> None:
        """Record a restart."""
        self.log.append("reinitialise")

    @override
    def scan_pages(
        self, device_id: str, settings: ScanSettings, sink: PageSink
    ) -> ScanBatch:
        """
        Record a pass, then feed it.

        Args:
            device_id: Passed through.
            settings: Passed through.
            sink: Passed through.

        Returns:
            The pass's batch.

        """
        self.log.append("scan")
        return super().scan_pages(device_id, settings, sink)


class _UnofferedAnswer(PassCoordinator):
    """A broken coordinator that answers every prompt with "skip the blanks"."""

    @override
    def ask(self, prompt: PassPrompt) -> PassAnswer:
        """
        Answer with something no next-pass prompt offers.

        Args:
            prompt: Ignored.

        Returns:
            Always ``PassAnswer.SKIP_BLANKS``.

        """
        del prompt
        return PassAnswer.SKIP_BLANKS


class _StopLandsAfterAnswer(ScriptedPassCoordinator):
    """
    A script whose answer to one prompt is claimed just before saneless stops.

    The prompt keeps that answer -- a stop never overrides a claimed one -- so
    the only thing that can keep the run from acting on it is ``stopping``.
    """

    def __init__(self, answers: Sequence[PassAnswer], *, stop_after: int) -> None:
        """
        Prepare the script, and the prompt whose answer the stop follows.

        Args:
            answers: The answers to give, one per question, in order.
            stop_after: The number of the prompt after whose answer the stop
                lands.

        """
        super().__init__(answers)
        self._stop_after = stop_after
        self._stopping = False

    @override
    def ask(self, prompt: PassPrompt) -> PassAnswer:
        """
        Answer from the script, and start stopping once the chosen prompt is.

        Args:
            prompt: The question the run is asking.

        Returns:
            The next answer in the script.

        """
        answer = super().ask(prompt)
        if prompt.number == self._stop_after:
            self._stopping = True
        return answer

    @property
    @override
    def stopping(self) -> bool:
        """
        Whether the stop has landed.

        Returns:
            True once the chosen prompt was answered.

        """
        return self._stopping


def _untouched_scanner() -> MagicMock:
    """
    Build a scanner that would be asked for its devices if the run got that far.

    Returns:
        A scanner stand-in listing no devices.

    """
    scanner = MagicMock(spec=ScannerBackend)
    scanner.get_devices.return_value = []
    return scanner


class TestMultiPageRefusals:
    """Requests a multi-page run cannot serve are refused before the device."""

    def test_a_manual_duplex_profile_is_refused(self, rig: _Rig) -> None:
        """
        Multi-page on a manual-duplex profile names the profile and the reason.

        The request carries both coordinators, so nothing but the multi-page
        refusal can stop it, and auto-detection is on, so a refusal that came
        after device resolution would have listed the devices.
        """
        rig.settings.scanner.device = ""
        scanner = _untouched_scanner()

        with pytest.raises(ConfigError) as raised:
            rig.run(scanner, ScriptedPassCoordinator([]), profile=DUPLEX_PROFILE)

        message = str(raised.value)
        assert f"'{DUPLEX_PROFILE}'" in message
        assert "manual duplex" in message
        assert "multi-page" in message.lower()
        scanner.get_devices.assert_not_called()
        scanner.scan_pages.assert_not_called()

    def test_a_request_with_no_pass_coordinator_is_refused(self, rig: _Rig) -> None:
        """Nothing could answer the first prompt, so nothing is scanned."""
        rig.settings.scanner.device = ""
        scanner = _untouched_scanner()

        with pytest.raises(ConfigError, match="coordinator"):
            rig.run(scanner, None)

        scanner.get_devices.assert_not_called()
        scanner.scan_pages.assert_not_called()

    def test_a_single_pass_request_never_asks_its_coordinator(self, rig: _Rig) -> None:
        """A coordinator on a single-pass request is ignored: the scan is one pass."""
        scanner = DistinctPageScanner(passes=((0,),))
        coordinator = ScriptedPassCoordinator([])

        result = rig.run(scanner, coordinator, multi_page=False)

        assert coordinator.prompts == []
        assert scanner.calls == 1
        assert result.outcome is ScanOutcome.SUCCESS
        assert result.pages_scanned == result.pages_uploaded == 1
        assert PipelineEvent.AWAITING_NEXT_PASS not in rig.events


class TestMultiPageLoop:
    """A run of passes becomes one document, asked about between every pass."""

    def test_four_passes_become_one_four_page_document(self, rig: _Rig) -> None:
        """Scan next three times, then Finish: four pages, in scan order, plain DONE."""
        scanner = DistinctPageScanner(passes=((0,), (1,), (2,), (3,)))
        coordinator = ScriptedPassCoordinator([_NEXT, _NEXT, _NEXT, _FINISH])

        result = rig.run(scanner, coordinator)

        assert result.outcome is ScanOutcome.SUCCESS
        assert result.warning is None
        assert result.pages_scanned == result.pages_uploaded == 4
        assert result.pages_removed == 0
        assert result.removed_positions == ()
        (document,) = rig.uploads
        assert embedded_streams(document) == _pages(scanner, [0, 1, 2, 3])

    def test_every_pass_is_followed_by_a_numbered_prompt(self, rig: _Rig) -> None:
        """Each prompt is the next number, counts the pages kept, and offers all four."""
        scanner = DistinctPageScanner(passes=((0,), (1,), (2,), (3,)))
        coordinator = ScriptedPassCoordinator([_NEXT, _NEXT, _NEXT, _FINISH])

        rig.run(scanner, coordinator)

        prompts = coordinator.prompts
        assert [prompt.number for prompt in prompts] == [1, 2, 3, 4]
        assert [prompt.pages_kept for prompt in prompts] == [1, 2, 3, 4]
        for prompt in prompts:
            assert prompt.wait is PassWait.NEXT_PASS
            assert prompt.offered == _EVERY_NEXT_PASS_ANSWER
            assert prompt.last_pass_pages == prompt.last_pass_kept == 1
            assert prompt.timeout_seconds == _TIMEOUT

    def test_the_wait_is_announced_before_each_question(self, rig: _Rig) -> None:
        """Every prompt is asked with AWAITING_NEXT_PASS already reported."""
        latest: list[PipelineEvent] = []

        def _note_latest(prompt: PassPrompt) -> None:
            del prompt
            latest.append(rig.events[-1])

        scanner = DistinctPageScanner(passes=((0,), (1,)))
        coordinator = ScriptedPassCoordinator([_NEXT, _FINISH], on_ask=_note_latest)

        rig.run(scanner, coordinator)

        assert latest == [PipelineEvent.AWAITING_NEXT_PASS] * 2
        assert rig.events == [
            PipelineEvent.SCANNING,
            PipelineEvent.AWAITING_NEXT_PASS,
            PipelineEvent.SCANNING,
            PipelineEvent.AWAITING_NEXT_PASS,
            PipelineEvent.ASSEMBLING,
            PipelineEvent.UPLOADING,
            PipelineEvent.DONE,
        ]

    def test_a_pass_of_several_sheets_is_one_prompt(self, rig: _Rig) -> None:
        """A three-sheet pass is asked about once, and counts all three."""
        scanner = DistinctPageScanner(passes=((0, 1, 2),))
        coordinator = ScriptedPassCoordinator([_FINISH])

        result = rig.run(scanner, coordinator)

        (prompt,) = coordinator.prompts
        assert prompt.pages_kept == 3
        assert prompt.last_pass_pages == 3
        assert result.pages_uploaded == 3
        (document,) = rig.uploads
        assert embedded_streams(document) == _pages(scanner, [0, 1, 2])

    def test_abort_cancels_and_keeps_nothing(self, rig: _Rig) -> None:
        """Abort is the operator's decision to stop: nothing uploaded, nothing kept."""
        scanner = DistinctPageScanner(passes=((0,), (1,)))
        coordinator = ScriptedPassCoordinator([_NEXT, _ABORT])

        with pytest.raises(ScanCancelledError):
            rig.run(scanner, coordinator)

        assert rig.kept_pdfs() == []
        rig.paperless.upload_document.assert_not_called()

    def test_a_broken_prompt_is_a_failure_that_keeps_the_pages(self, rig: _Rig) -> None:
        """An abort nobody chose fails the run, and the kept page is kept."""
        cause = RuntimeError("tty gone")
        scanner = DistinctPageScanner(passes=((0,),))
        coordinator = ScriptedPassCoordinator([_ABORT], cause=cause)

        with pytest.raises(ScanError) as raised:
            rig.run(scanner, coordinator)

        assert str(raised.value).startswith("Multi-page prompt failed")
        assert raised.value.__cause__ is cause
        assert not isinstance(raised.value, ScanCancelledError)
        (kept,) = rig.kept_pdfs()
        assert embedded_streams(kept) == _pages(scanner, [0])

    def test_a_broken_prompt_next_step_is_about_the_terminal(self, rig: _Rig) -> None:
        """The prompt broke, not the scanner: the next step says so."""
        scanner = DistinctPageScanner(passes=((0,),))
        coordinator = ScriptedPassCoordinator([_ABORT], cause=RuntimeError("tty gone"))

        with pytest.raises(ScanError) as raised:
            rig.run(scanner, coordinator)

        _assert_terminal_next_step(raised.value)

    def test_a_timeout_finishes_with_a_warning(self, rig: _Rig) -> None:
        """Nobody answered prompt 3: the three kept pages upload, warned."""
        scanner = DistinctPageScanner(passes=((0,), (1,), (2,)))
        coordinator = ScriptedPassCoordinator([_NEXT, _NEXT, PassAnswer.TIMED_OUT])

        result = rig.run(scanner, coordinator)

        assert result.outcome is ScanOutcome.SUCCESS
        assert result.warning == timeout_finish_warning(3, _TIMEOUT)
        assert result.pages_uploaded == 3
        (document,) = rig.uploads
        assert embedded_streams(document) == _pages(scanner, [0, 1, 2])

    def test_a_stop_at_a_prompt_keeps_every_page_and_uploads_nothing(
        self, rig: _Rig
    ) -> None:
        """A server stop while waiting keeps the accepted pages as one PDF."""
        scanner = DistinctPageScanner(passes=((0,), (1,)))
        coordinator = ScriptedPassCoordinator([_NEXT, PassAnswer.INTERRUPTED])

        with pytest.raises(ScanInterrupted):
            rig.run(scanner, coordinator)

        (kept,) = rig.kept_pdfs()
        assert embedded_streams(kept) == _pages(scanner, [0, 1])
        rig.paperless.upload_document.assert_not_called()

    def test_a_signal_mid_pass_keeps_the_document_and_the_pass(self, rig: _Rig) -> None:
        """Interrupted during pass 3: the document and pass 3's page are both kept."""
        scanner = DistinctPageScanner(
            passes=((0,), (1,), (2,)), fail_on={3: ScanInterrupted("signal")}
        )
        coordinator = ScriptedPassCoordinator([_NEXT, _NEXT])

        with pytest.raises(ScanInterrupted):
            rig.run(scanner, coordinator)

        assert embedded_streams(rig.kept_document()) == _pages(scanner, [0, 1])
        (partial,) = rig.kept_partials()
        assert embedded_streams(partial) == _pages(scanner, [2])
        rig.paperless.upload_document.assert_not_called()

    @pytest.mark.parametrize(
        ("kind", "message"),
        [
            (DiskSpaceError, "Insufficient disk space for page 1"),
            (SpoolError, "Could not write page 1"),
        ],
        ids=["disk_space", "spool_write"],
    )
    def test_a_full_disk_ends_the_job_and_keeps_the_document(
        self, rig: _Rig, kind: type[DiskSpaceError | SpoolError], message: str
    ) -> None:
        """A disk refusal is not the scanner's fault: the job ends, pass 1 kept."""
        refusal = kind(message)
        scanner = DistinctPageScanner(passes=((0,), (1,)), fail_on={2: refusal})
        coordinator = ScriptedPassCoordinator([_NEXT])

        with pytest.raises(kind) as raised:
            rig.run(scanner, coordinator)

        assert raised.value is refusal
        assert raised.value.__notes__
        assert embedded_streams(rig.kept_document()) == _pages(scanner, [0])
        assert len(coordinator.prompts) == 1

    def test_a_coding_error_ends_the_job_as_itself(self, rig: _Rig) -> None:
        """A bug mid-loop keeps its own type, and the accepted pass is kept."""
        bug = RuntimeError("bug")
        scanner = DistinctPageScanner(passes=((0,), (1,)), fail_on={2: bug})
        coordinator = ScriptedPassCoordinator([_NEXT])

        with pytest.raises(RuntimeError) as raised:
            rig.run(scanner, coordinator)

        assert raised.value is bug
        assert embedded_streams(rig.kept_document()) == _pages(scanner, [0])

    def test_every_pass_spools_under_its_own_label(self, rig: _Rig) -> None:
        """Pass N's pages are ``a-0000N-*``, and name order is scan order."""
        listed: list[list[str]] = []

        def _list_at_prompt_3(prompt: PassPrompt) -> None:
            if prompt.number == 3:
                listed.append(rig.spool_names())

        scanner = DistinctPageScanner(passes=((0,), (1,), (2,)))
        coordinator = ScriptedPassCoordinator(
            [_NEXT, _NEXT, _FINISH], on_ask=_list_at_prompt_3
        )

        rig.run(scanner, coordinator)

        (names,) = listed
        in_scan_order = ["a-00001-0001.png", "a-00002-0001.png", "a-00003-0001.png"]
        assert sorted(names) == in_scan_order

    def test_sane_restarts_before_every_pass_after_the_first(self, rig: _Rig) -> None:
        """Three passes, two restarts, each immediately before its pass."""
        scanner = _ReinitialiseSpy(passes=((0,), (1,), (2,)))
        coordinator = ScriptedPassCoordinator([_NEXT, _NEXT, _FINISH])

        rig.run(scanner, coordinator)

        assert scanner.log == [
            "scan",
            "reinitialise",
            "scan",
            "reinitialise",
            "scan",
        ]

    def test_manual_duplex_never_restarts_sane(self, rig: _Rig) -> None:
        """The flip wait's second pass is not a multi-page pass."""
        scanner = _ReinitialiseSpy(passes=((0, 1), (2, 3)))

        rig.run(scanner, None, profile=DUPLEX_PROFILE, multi_page=False)

        assert scanner.log == ["scan", "scan"]

    def test_the_thumbnail_is_the_first_page_only(self, rig: _Rig) -> None:
        """No later pass replaces the document's first page as its thumbnail."""
        scanner = DistinctPageScanner(passes=((0,), (1,), (2,)))
        coordinator = ScriptedPassCoordinator([_NEXT, _NEXT, _FINISH])

        rig.run(scanner, coordinator)

        assert len(rig.thumbnails) == 1

    def test_an_unreadable_sheet_is_still_reported(self, rig: _Rig) -> None:
        """A sheet the scanner skipped in one pass warns the finished document."""
        scanner = DistinctPageScanner(passes=((0,), (1,)), rejected=(1, 0))
        coordinator = ScriptedPassCoordinator([_NEXT, _FINISH])

        result = rig.run(scanner, coordinator)

        expected = _rejected_pages_warning(1)
        assert expected is not None
        assert result.warning is not None
        assert expected in result.warning

    def test_an_answer_the_prompt_did_not_offer_fails_and_keeps(
        self, rig: _Rig
    ) -> None:
        """A coordinator that answers off the menu fails the run; the page is kept."""
        scanner = DistinctPageScanner(passes=((0,),))

        with pytest.raises(ScanError) as raised:
            rig.run(scanner, _UnofferedAnswer())

        assert PassAnswer.SKIP_BLANKS.value in str(raised.value)
        (kept,) = rig.kept_pdfs()
        assert embedded_streams(kept) == _pages(scanner, [0])

    def test_rescan_replaces_the_last_pass(self, rig: _Rig) -> None:
        """Re-scan throws pass 2 away and scans again at once, with no prompt between."""
        listed: list[list[str]] = []

        def _list_at_prompt_3(prompt: PassPrompt) -> None:
            if prompt.number == 3:
                listed.append(rig.spool_names())

        scanner = DistinctPageScanner(passes=((0,), (1,), (2,)))
        coordinator = ScriptedPassCoordinator(
            [_NEXT, _RESCAN, _FINISH], on_ask=_list_at_prompt_3
        )

        result = rig.run(scanner, coordinator)

        assert scanner.calls == 3
        assert len(coordinator.prompts) == 3
        assert [prompt.pages_kept for prompt in coordinator.prompts] == [1, 2, 2]
        (names,) = listed
        assert "a-00002-0001.png" not in names
        assert [name for name in names if name.startswith("a-00002-")] == []
        assert result.pages_scanned == 2
        (document,) = rig.uploads
        assert embedded_streams(document) == _pages(scanner, [0, 2])

    def test_rescan_discards_every_sheet_of_the_last_pass(self, rig: _Rig) -> None:
        """Re-scan of a two-sheet pass throws both sheets away."""
        scanner = DistinctPageScanner(passes=((0, 1, 2), (3, 4), (5, 6)))
        coordinator = ScriptedPassCoordinator([_NEXT, _RESCAN, _FINISH])

        result = rig.run(scanner, coordinator)

        assert coordinator.prompts[1].last_pass_pages == 2
        assert result.pages_scanned == 5
        (document,) = rig.uploads
        assert embedded_streams(document) == _pages(scanner, [0, 1, 2, 5, 6])

    def test_a_stop_after_a_rescan_never_keeps_the_discarded_pass(
        self, rig: _Rig
    ) -> None:
        """The pages Re-scan threw away do not come back in ``failed/``."""
        scanner = DistinctPageScanner(passes=((0, 1, 2), (3, 4), (5, 6)))
        coordinator = ScriptedPassCoordinator([_NEXT, _RESCAN, PassAnswer.INTERRUPTED])

        with pytest.raises(ScanInterrupted):
            rig.run(scanner, coordinator)

        (kept,) = rig.kept_pdfs()
        assert embedded_streams(kept) == _pages(scanner, [0, 1, 2, 5, 6])


class TestMultiPageCap:
    """
    A document stops offering another pass once it holds the page cap.

    The cap is lowered for each test so a handful of pages reaches it; the
    pipeline reads it when it checks, so the lowered value is the one in force.
    """

    def test_the_pass_that_crosses_the_cap_is_kept_whole(
        self, rig: _Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cap 3, then a two-page pass at 2 kept: all four upload, warned, no prompt."""
        monkeypatch.setattr(pipeline_module, "MAX_DOCUMENT_PAGES", 3)
        scanner = DistinctPageScanner(passes=((0, 1), (2, 3)))
        coordinator = ScriptedPassCoordinator([_NEXT])

        result = rig.run(scanner, coordinator)

        assert len(coordinator.prompts) == 1
        assert result.outcome is ScanOutcome.SUCCESS
        assert result.warning == cap_finish_warning(4, 3)
        assert result.pages_uploaded == 4
        (document,) = rig.uploads
        assert embedded_streams(document) == _pages(scanner, [0, 1, 2, 3])

    def test_reaching_the_cap_exactly_finishes(
        self, rig: _Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Three kept at a cap of 3 is the cap reached: finished, warned."""
        monkeypatch.setattr(pipeline_module, "MAX_DOCUMENT_PAGES", 3)
        scanner = DistinctPageScanner(passes=((0, 1), (2,)))
        coordinator = ScriptedPassCoordinator([_NEXT])

        result = rig.run(scanner, coordinator)

        assert len(coordinator.prompts) == 1
        assert result.warning == cap_finish_warning(3, 3)
        assert result.pages_uploaded == 3

    def test_below_the_cap_the_operator_is_still_asked(
        self, rig: _Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two kept under a cap of 3: asked twice, and Finish is a plain DONE."""
        monkeypatch.setattr(pipeline_module, "MAX_DOCUMENT_PAGES", 3)
        scanner = DistinctPageScanner(passes=((0,), (1,)))
        coordinator = ScriptedPassCoordinator([_NEXT, _FINISH])

        result = rig.run(scanner, coordinator)

        assert len(coordinator.prompts) == 2
        assert result.warning is None
        assert result.pages_uploaded == 2

    def test_the_cap_warning_sits_beside_the_unreadable_sheet_warning(
        self, rig: _Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Neither reason for the warning hides the other."""
        monkeypatch.setattr(pipeline_module, "MAX_DOCUMENT_PAGES", 2)
        scanner = DistinctPageScanner(passes=((0,), (1,)), rejected=(1, 0))
        coordinator = ScriptedPassCoordinator([_NEXT])

        result = rig.run(scanner, coordinator)

        unreadable = _rejected_pages_warning(1)
        assert unreadable is not None
        assert result.warning is not None
        assert cap_finish_warning(2, 2) in result.warning
        assert unreadable in result.warning


# A jam part way through a later pass: the scanner's fault, not the operator's.
_JAM = "Document feeder jammed"


def _jams_on_pass_2() -> DistinctPageScanner:
    """
    Build a scanner whose second pass spools its page and then jams.

    Returns:
        A scanner feeding page 0, then page 1 before the jam, then page 2.

    """
    return DistinctPageScanner(passes=((0,), (1,), (2,)), fail_on={2: ScanError(_JAM)})


class _WedgedRestart(DistinctPageScanner):
    """A distinct-page scanner whose first restart before a later pass fails."""

    def __init__(self, *, passes: Sequence[Sequence[int]], message: str) -> None:
        """
        Prepare the passes and the restart's failure.

        Args:
            passes: The page indices each successive scan feeds.
            message: What the failing restart says.

        """
        super().__init__(passes=passes)
        self.message = message
        self.restarts = 0

    @override
    def reinitialise(self) -> None:
        """
        Fail the first restart, and let every later one through.

        Raises:
            ScanError: On the first call.

        """
        self.restarts += 1
        if self.restarts == 1:
            raise ScanError(self.message)


class TestMultiPageScannerFault:
    """A later pass that the scanner fails goes back to the operator."""

    def test_a_jam_on_a_later_pass_asks_the_operator(self, rig: _Rig) -> None:
        """
        The jammed pass is thrown away, and the operator decides what next.

        The failed pass's page is gone from the spool by the time the question
        is asked, and it is neither uploaded nor counted as scanned.
        """
        latest: list[PipelineEvent] = []
        listed: list[list[str]] = []

        def _at_prompt_2(prompt: PassPrompt) -> None:
            if prompt.number == 2:
                latest.append(rig.events[-1])
                listed.append(rig.spool_names())

        scanner = _jams_on_pass_2()
        coordinator = ScriptedPassCoordinator(
            [_NEXT, _NEXT, _FINISH], on_ask=_at_prompt_2
        )

        result = rig.run(scanner, coordinator)

        retry = coordinator.prompts[1]
        assert retry.wait is PassWait.RETRY
        assert retry.offered == frozenset({_NEXT, _FINISH, _ABORT})
        assert retry.pages_kept == 1
        assert retry.timeout_seconds == _TIMEOUT
        assert retry.error is not None
        assert _JAM in retry.error
        assert latest == [PipelineEvent.AWAITING_RETRY]
        (names,) = listed
        assert [name for name in names if name.startswith("a-00002-")] == []
        assert result.outcome is ScanOutcome.SUCCESS
        assert result.pages_scanned == 2
        (document,) = rig.uploads
        assert embedded_streams(document) == _pages(scanner, [0, 2])

    def test_finish_after_a_jam_is_a_plain_done(self, rig: _Rig) -> None:
        """Finish pressed at the retry prompt is the operator's choice: no warning."""
        scanner = _jams_on_pass_2()
        coordinator = ScriptedPassCoordinator([_NEXT, _FINISH])

        result = rig.run(scanner, coordinator)

        assert result.outcome is ScanOutcome.SUCCESS
        assert result.warning is None
        (document,) = rig.uploads
        assert embedded_streams(document) == _pages(scanner, [0])

    def test_a_timeout_after_a_jam_finishes_warned(self, rig: _Rig) -> None:
        """Nobody answered the retry prompt: the kept page uploads, warned."""
        scanner = _jams_on_pass_2()
        coordinator = ScriptedPassCoordinator([_NEXT, PassAnswer.TIMED_OUT])

        result = rig.run(scanner, coordinator)

        assert result.outcome is ScanOutcome.SUCCESS
        assert result.warning == timeout_finish_warning(1, _TIMEOUT)
        (document,) = rig.uploads
        assert embedded_streams(document) == _pages(scanner, [0])

    def test_abort_after_a_jam_keeps_nothing(self, rig: _Rig) -> None:
        """Abort at the retry prompt is a cancel, like Abort anywhere else."""
        coordinator = ScriptedPassCoordinator([_NEXT, _ABORT])

        with pytest.raises(ScanCancelledError):
            rig.run(_jams_on_pass_2(), coordinator)

        assert rig.kept_pdfs() == []
        rig.paperless.upload_document.assert_not_called()

    def test_a_stop_after_a_jam_keeps_only_the_accepted_pages(self, rig: _Rig) -> None:
        """The thrown-away pass never comes back as a ``(partial)`` in ``failed/``."""
        scanner = _jams_on_pass_2()
        coordinator = ScriptedPassCoordinator([_NEXT, PassAnswer.INTERRUPTED])

        with pytest.raises(ScanInterrupted):
            rig.run(scanner, coordinator)

        (kept,) = rig.kept_pdfs()
        assert "partial" not in kept.name
        assert embedded_streams(kept) == _pages(scanner, [0])

    def test_a_jam_on_the_first_pass_fails_the_job(self, rig: _Rig) -> None:
        """With nothing kept there is nothing to go back to: the failure stands."""
        jam = ScanError("jam")
        scanner = DistinctPageScanner(passes=((0,),), fail_on={1: jam})
        coordinator = ScriptedPassCoordinator([])

        with pytest.raises(ScanError) as raised:
            rig.run(scanner, coordinator)

        assert raised.value is jam
        assert coordinator.prompts == []
        (partial,) = rig.kept_partials()
        assert embedded_streams(partial) == _pages(scanner, [0])

    def test_a_jam_on_the_rescan_of_the_only_pass_fails_the_job(
        self, rig: _Rig
    ) -> None:
        """Re-scan of the only pass leaves nothing kept, so its jam ends the job."""
        jam = ScanError("jam")
        scanner = DistinctPageScanner(passes=((0,), (1,), (2,)), fail_on={2: jam})
        coordinator = ScriptedPassCoordinator([_RESCAN])

        with pytest.raises(ScanError) as raised:
            rig.run(scanner, coordinator)

        assert raised.value is jam
        assert len(coordinator.prompts) == 1
        rig.paperless.upload_document.assert_not_called()

    @pytest.mark.parametrize(
        ("kind", "message"),
        [
            (DiskSpaceError, "Insufficient disk space for page 1"),
            (SpoolError, "Could not write page 1"),
        ],
        ids=["disk_space", "spool_write"],
    )
    def test_a_full_disk_on_a_later_pass_is_not_a_scanner_fault(
        self, rig: _Rig, kind: type[DiskSpaceError | SpoolError], message: str
    ) -> None:
        """A disk refusal would fail again at once, so it ends the job."""
        refusal = kind(message)
        scanner = DistinctPageScanner(passes=((0,), (1,)), fail_on={2: refusal})
        coordinator = ScriptedPassCoordinator([_NEXT])

        with pytest.raises(kind) as raised:
            rig.run(scanner, coordinator)

        assert raised.value is refusal
        assert [prompt.wait for prompt in coordinator.prompts] == [PassWait.NEXT_PASS]
        assert embedded_streams(rig.kept_document()) == _pages(scanner, [0])

    def test_an_empty_feeder_on_a_later_pass_asks_the_operator(self, rig: _Rig) -> None:
        """Scan next pressed before the sheet was loaded is a retry, not a failure."""
        empty = FeederEmptyError("No paper detected in feeder")
        scanner = DistinctPageScanner(passes=((0,), (), (2,)), fail_on={2: empty})
        coordinator = ScriptedPassCoordinator([_NEXT, _NEXT, _FINISH])

        result = rig.run(scanner, coordinator)

        retry = coordinator.prompts[1]
        assert retry.wait is PassWait.RETRY
        assert retry.offered == frozenset({_NEXT, _FINISH, _ABORT})
        assert retry.error is not None
        assert "No paper detected in feeder" in retry.error
        assert result.pages_uploaded == 2
        (document,) = rig.uploads
        assert embedded_streams(document) == _pages(scanner, [0, 2])

    def test_an_empty_feeder_on_the_first_pass_fails_the_job(self, rig: _Rig) -> None:
        """With nothing kept, an empty feeder is the ordinary empty-feeder failure."""
        empty = FeederEmptyError("No paper detected in feeder")
        scanner = DistinctPageScanner(passes=((),), fail_on={1: empty})
        coordinator = ScriptedPassCoordinator([])

        with pytest.raises(FeederEmptyError) as raised:
            rig.run(scanner, coordinator)

        assert raised.value is empty
        assert coordinator.prompts == []

    @pytest.mark.parametrize(
        ("passes", "failure", "said"),
        [
            (((0,), (1,)), ListingCrashedError("died"), "died"),
            (((0,), ()), None, "No pages were scanned"),
        ],
        ids=["listing-crashed", "empty-pass"],
    )
    def test_other_scanner_faults_ask_the_operator(
        self,
        rig: _Rig,
        passes: tuple[tuple[int, ...], ...],
        failure: ScanError | None,
        said: str,
    ) -> None:
        """A crashed device listing and a pass that came back empty both return."""
        fail_on = {} if failure is None else {2: failure}
        scanner = DistinctPageScanner(passes=passes, fail_on=fail_on)
        coordinator = ScriptedPassCoordinator([_NEXT, _FINISH])

        result = rig.run(scanner, coordinator)

        retry = coordinator.prompts[1]
        assert retry.wait is PassWait.RETRY
        assert retry.error is not None
        assert said in retry.error
        assert result.pages_uploaded == 1

    def test_a_failed_restart_before_a_pass_asks_the_operator(self, rig: _Rig) -> None:
        """A restart that fails is the pass failing; the next try restarts again."""
        scanner = _WedgedRestart(passes=((0,), (1,)), message="wedged read")
        coordinator = ScriptedPassCoordinator([_NEXT, _NEXT, _FINISH])

        result = rig.run(scanner, coordinator)

        retry = coordinator.prompts[1]
        assert retry.wait is PassWait.RETRY
        assert retry.error is not None
        assert "wedged read" in retry.error
        assert scanner.restarts == 2
        (document,) = rig.uploads
        assert embedded_streams(document) == _pages(scanner, [0, 1])
        assert result.pages_scanned == 2

    def test_a_broken_retry_prompt_is_not_another_scanner_fault(
        self, rig: _Rig
    ) -> None:
        """A retry prompt that broke fails the run; it is never asked again."""
        cause = RuntimeError("tty gone")
        coordinator = ScriptedPassCoordinator([_NEXT, _ABORT], cause=cause)

        with pytest.raises(ScanError) as raised:
            rig.run(_jams_on_pass_2(), coordinator)

        assert str(raised.value).startswith("Multi-page prompt failed")
        assert raised.value.__cause__ is cause
        assert len(coordinator.prompts) == 2

    def test_a_broken_retry_prompt_next_step_is_about_the_terminal(
        self, rig: _Rig
    ) -> None:
        """After a jam, a broken retry prompt still points at the terminal."""
        coordinator = ScriptedPassCoordinator(
            [_NEXT, _ABORT], cause=RuntimeError("tty gone")
        )

        with pytest.raises(ScanError) as raised:
            rig.run(_jams_on_pass_2(), coordinator)

        _assert_terminal_next_step(raised.value)

    def test_the_failure_is_logged_once(
        self, rig: _Rig, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        The jam is logged at WARNING, once, as the exception's ``repr``.

        With its traceback: the operator worked around the fault, so this line
        is the only record of where it came from.
        """
        jam = ScanError(_JAM)
        scanner = DistinctPageScanner(passes=((0,), (1,)), fail_on={2: jam})
        coordinator = ScriptedPassCoordinator([_NEXT, _FINISH])
        caplog.set_level(logging.WARNING)

        rig.run(scanner, coordinator)

        logged = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING and repr(jam) in record.getMessage()
        ]
        assert len(logged) == 1
        (record,) = logged
        assert record.exc_info is not None
        assert record.exc_info[1] is jam

    def test_both_pass_log_lines_number_a_pass_the_same_way(
        self, rig: _Rig, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        A pass after a failed one is logged under the number it was scanned as.

        The failure line numbers passes as they were started, thrown-away ones
        included, so the accepted line must too, or "pass 3 failed" and "pass
        2: scanned" could name the same spool label.
        """
        scanner = _jams_on_pass_2()
        coordinator = ScriptedPassCoordinator([_NEXT, _NEXT, _FINISH])
        caplog.set_level(logging.INFO, logger="saneless.pipeline")

        rig.run(scanner, coordinator)

        messages = [record.getMessage() for record in caplog.records]
        assert any(m.startswith("Multi-page pass 1: scanned") for m in messages)
        assert any(m.startswith("Multi-page pass 2 failed") for m in messages)
        assert any(m.startswith("Multi-page pass 3: scanned") for m in messages)
        assert not any(m.startswith("Multi-page pass 2: scanned") for m in messages)


# The answers a blank-page prompt offers: throw the pass away, or take it
# without its blank pages, or with them.
_BLANK_ANSWERS = frozenset(
    {PassAnswer.RESCAN, PassAnswer.SKIP_BLANKS, PassAnswer.KEEP_BLANKS}
)

_SKIP = PassAnswer.SKIP_BLANKS
_KEEP = PassAnswer.KEEP_BLANKS


@pytest.fixture
def blank_rig(tmp_path: Path) -> _Rig:
    """
    Return a multi-page rig over a flatbed profile with detection on.

    Returns:
        The rig.

    """
    return _Rig(multi_page_settings(tmp_path, detection=True))


class TestMultiPageBlankPages:
    """
    The operator decides about blank pages, once per pass, inside the loop.

    A single-pass scan removes blank pages on its own; a multi-page scan asks
    instead, because the operator is standing there and a page kept on purpose
    must never be removed later.  Skipped pages are reported the way removed
    ones are, numbered over the document the operator built.
    """

    def test_a_blank_pass_asks_once_and_skip_leaves_it_out(
        self, blank_rig: _Rig
    ) -> None:
        """One blank page in pass 2: one blank prompt, and Skip leaves it out."""
        latest: list[PipelineEvent] = []

        def _note_at_prompt_2(prompt: PassPrompt) -> None:
            if prompt.number == 2:
                latest.append(blank_rig.events[-1])

        scanner = DistinctPageScanner(passes=((0,), (1,), (2,)), blank={1})
        coordinator = ScriptedPassCoordinator(
            [_NEXT, _SKIP, _NEXT, _FINISH], on_ask=_note_at_prompt_2
        )

        result = blank_rig.run(scanner, coordinator)

        blank = coordinator.prompts[1]
        assert blank.wait is PassWait.BLANK_DECISION
        assert blank.pass_pages == 1
        assert blank.blank_positions == (1,)
        assert blank.pages_kept == 1
        assert blank.offered == _BLANK_ANSWERS
        assert blank.timeout_seconds == _TIMEOUT
        assert latest == [PipelineEvent.AWAITING_BLANK_DECISION]
        after = coordinator.prompts[2]
        assert after.wait is PassWait.NEXT_PASS
        assert after.pages_kept == 1
        assert after.last_pass_pages == 1
        assert after.last_pass_kept == 0
        assert result.outcome is ScanOutcome.SUCCESS
        assert result.warning is None
        assert result.pages_scanned == 3
        assert result.pages_removed == 1
        assert result.removed_positions == (2,)
        assert result.pages_uploaded == 2
        (document,) = blank_rig.uploads
        assert embedded_streams(document) == _pages(scanner, [0, 2])

    def test_keep_adds_the_blank_page_and_it_is_never_judged_again(
        self, blank_rig: _Rig
    ) -> None:
        """Keep puts the blank page in the document, and nothing removes it later."""
        scanner = DistinctPageScanner(passes=((0,), (1,), (2,)), blank={1})
        coordinator = ScriptedPassCoordinator([_NEXT, _KEEP, _NEXT, _FINISH])

        result = blank_rig.run(scanner, coordinator)

        assert coordinator.prompts[2].last_pass_kept == 1
        assert result.pages_uploaded == 3
        assert result.pages_removed == 0
        assert result.removed_positions == ()
        (document,) = blank_rig.uploads
        assert embedded_streams(document) == _pages(scanner, [0, 1, 2])

    def test_rescan_at_the_blank_prompt_scans_again_at_once(
        self, blank_rig: _Rig
    ) -> None:
        """Re-scan throws the blank pass away and scans its replacement straight away."""
        listed: list[list[str]] = []

        def _list_at_prompt_3(prompt: PassPrompt) -> None:
            if prompt.number == 3:
                listed.append(blank_rig.spool_names())

        scanner = DistinctPageScanner(passes=((0,), (1,), (3,), (2,)), blank={1})
        coordinator = ScriptedPassCoordinator(
            [_NEXT, _RESCAN, _NEXT, _FINISH], on_ask=_list_at_prompt_3
        )

        result = blank_rig.run(scanner, coordinator)

        assert [prompt.wait for prompt in coordinator.prompts] == [
            PassWait.NEXT_PASS,
            PassWait.BLANK_DECISION,
            PassWait.NEXT_PASS,
            PassWait.NEXT_PASS,
        ]
        assert scanner.calls == 4
        (names,) = listed
        assert [name for name in names if name.startswith("a-00002-")] == []
        assert result.pages_scanned == 3
        assert result.removed_positions == ()
        (document,) = blank_rig.uploads
        assert embedded_streams(document) == _pages(scanner, [0, 3, 2])

    def test_a_pass_of_several_blanks_is_asked_about_once(
        self, blank_rig: _Rig
    ) -> None:
        """Two blank pages in one four-sheet pass: one prompt naming both."""
        scanner = DistinctPageScanner(passes=((0, 1, 2, 3),), blank={1, 3})
        coordinator = ScriptedPassCoordinator([_SKIP, _FINISH])

        result = blank_rig.run(scanner, coordinator)

        blanks = [
            prompt
            for prompt in coordinator.prompts
            if prompt.wait is PassWait.BLANK_DECISION
        ]
        (blank,) = blanks
        assert blank.pass_pages == 4
        assert blank.blank_positions == (2, 4)
        assert result.removed_positions == (2, 4)
        assert result.pages_uploaded == 2
        (document,) = blank_rig.uploads
        assert embedded_streams(document) == _pages(scanner, [0, 2])

    def test_a_pass_with_no_blank_page_is_not_asked_about(
        self, blank_rig: _Rig
    ) -> None:
        """Detection on, no blank page: only the next-pass question is asked."""
        scanner = DistinctPageScanner(passes=((0,), (1,)))
        coordinator = ScriptedPassCoordinator([_NEXT, _FINISH])

        result = blank_rig.run(scanner, coordinator)

        assert [prompt.wait for prompt in coordinator.prompts] == [
            PassWait.NEXT_PASS
        ] * 2
        assert PipelineEvent.AWAITING_BLANK_DECISION not in blank_rig.events
        assert result.pages_uploaded == 2

    def test_a_blank_prompt_timeout_skips_them_and_finishes_warned(
        self, blank_rig: _Rig
    ) -> None:
        """Nobody answered about the blank page: it is left out, and the rest uploads."""
        scanner = DistinctPageScanner(passes=((0,), (1,)), blank={1})
        coordinator = ScriptedPassCoordinator([_NEXT, PassAnswer.TIMED_OUT])

        result = blank_rig.run(scanner, coordinator)

        assert len(coordinator.prompts) == 2
        assert result.outcome is ScanOutcome.SUCCESS
        assert result.warning == blank_timeout_finish_warning(1, _TIMEOUT)
        assert result.pages_scanned == 2
        assert result.removed_positions == (2,)
        assert result.pages_uploaded == 1
        (document,) = blank_rig.uploads
        assert embedded_streams(document) == _pages(scanner, [0])

    def test_a_blank_prompt_timeout_with_nothing_kept_is_all_blank(
        self, blank_rig: _Rig
    ) -> None:
        """The only page looked blank and nobody answered: the all-blank failure."""
        scanner = DistinctPageScanner(passes=((0,),), blank={0})
        coordinator = ScriptedPassCoordinator([PassAnswer.TIMED_OUT])

        with pytest.raises(AllPagesBlankError) as raised:
            blank_rig.run(scanner, coordinator)

        assert classify_error(raised.value) is ErrorCategory.ALL_BLANK
        blank_rig.paperless.upload_document.assert_not_called()
        assert blank_rig.kept_partials() == []
        assert embedded_streams(blank_rig.kept_document()) == _pages(scanner, [0])

    def test_finish_is_withheld_after_a_skip_leaves_nothing(
        self, blank_rig: _Rig
    ) -> None:
        """
        Skipping the only page offers no Finish, and a timeout then is all-blank.

        A document of no pages can never be finished by a press, and a wait
        that runs out on one fails the way a scan of blank paper does.
        """
        scanner = DistinctPageScanner(passes=((0,),), blank={0})
        coordinator = ScriptedPassCoordinator([_SKIP, PassAnswer.TIMED_OUT])

        with pytest.raises(AllPagesBlankError) as raised:
            blank_rig.run(scanner, coordinator)

        after = coordinator.prompts[1]
        assert after.wait is PassWait.NEXT_PASS
        assert after.pages_kept == 0
        assert _FINISH not in after.offered
        assert classify_error(raised.value) is ErrorCategory.ALL_BLANK
        blank_rig.paperless.upload_document.assert_not_called()
        assert embedded_streams(blank_rig.kept_document()) == _pages(scanner, [0])

    def test_a_skip_to_nothing_can_still_go_on(self, blank_rig: _Rig) -> None:
        """Scan next after skipping the only page: the skipped page is still reported."""
        scanner = DistinctPageScanner(passes=((0,), (1,)), blank={0})
        coordinator = ScriptedPassCoordinator([_SKIP, _NEXT, _FINISH])

        result = blank_rig.run(scanner, coordinator)

        assert result.outcome is ScanOutcome.SUCCESS
        assert result.pages_scanned == 2
        assert result.removed_positions == (1,)
        assert result.pages_uploaded == 1
        (document,) = blank_rig.uploads
        assert embedded_streams(document) == _pages(scanner, [1])

    def test_detection_off_never_asks(self, rig: _Rig) -> None:
        """With detection off a blank page is just a page: no prompt, not removed."""
        scanner = DistinctPageScanner(passes=((0,), (1,), (2,)), blank={1})
        coordinator = ScriptedPassCoordinator([_NEXT, _NEXT, _FINISH])

        result = rig.run(scanner, coordinator)

        assert [prompt.wait for prompt in coordinator.prompts] == [
            PassWait.NEXT_PASS
        ] * 3
        assert result.pages_uploaded == 3
        assert result.removed_positions == ()
        (document,) = rig.uploads
        assert embedded_streams(document) == _pages(scanner, [0, 1, 2])

    def test_a_stop_at_the_blank_prompt_keeps_the_document_and_the_pass(
        self, blank_rig: _Rig
    ) -> None:
        """The undecided pass is kept as a pass in flight, beside the document."""
        scanner = DistinctPageScanner(passes=((0,), (1,)), blank={1})
        coordinator = ScriptedPassCoordinator([_NEXT, PassAnswer.INTERRUPTED])

        with pytest.raises(ScanInterrupted):
            blank_rig.run(scanner, coordinator)

        assert embedded_streams(blank_rig.kept_document()) == _pages(scanner, [0])
        (partial,) = blank_rig.kept_partials()
        assert embedded_streams(partial) == _pages(scanner, [1])
        blank_rig.paperless.upload_document.assert_not_called()

    def test_a_skipped_first_page_does_not_stay_the_preview(
        self, blank_rig: _Rig
    ) -> None:
        """
        The job's preview moves to the first page the document really holds.

        The preview is made as the pass's first page is spooled, before anyone
        has decided about it; once that page is skipped, the preview is made
        again from the first kept page.
        """
        scanner = DistinctPageScanner(passes=((0, 1),), blank={0})
        coordinator = ScriptedPassCoordinator([_SKIP, _FINISH])

        blank_rig.run(scanner, coordinator)

        with Image.open(io.BytesIO(scanner.spooled[1])) as kept_first:
            expected = generate_thumbnail(kept_first)
        assert len(blank_rig.thumbnails) == 2
        assert blank_rig.thumbnails[-1] == expected

    def test_rescan_takes_the_skipped_pages_away_with_the_pass(
        self, blank_rig: _Rig
    ) -> None:
        """A skipped page thrown away by a Re-scan is not reported as removed."""
        scanner = DistinctPageScanner(passes=((0,), (1, 2), (3,)), blank={2})
        coordinator = ScriptedPassCoordinator([_NEXT, _SKIP, _RESCAN, _FINISH])

        result = blank_rig.run(scanner, coordinator)

        assert coordinator.prompts[1].blank_positions == (2,)
        assert result.removed_positions == ()
        assert result.pages_removed == 0
        assert result.pages_scanned == 2
        (document,) = blank_rig.uploads
        assert embedded_streams(document) == _pages(scanner, [0, 3])

    def test_a_single_pass_scan_still_removes_blanks_by_itself(
        self, blank_rig: _Rig
    ) -> None:
        """Without the multi-page loop, blank pages are removed without a question."""
        scanner = DistinctPageScanner(passes=((0, 1, 2, 3),), blank={1, 3})
        coordinator = ScriptedPassCoordinator([])

        result = blank_rig.run(scanner, coordinator, multi_page=False)

        assert coordinator.prompts == []
        assert result.outcome is ScanOutcome.SUCCESS
        assert result.removed_positions == (2, 4)
        assert result.pages_scanned == 4
        assert result.pages_removed == 2
        assert result.pages_uploaded == 2
        assert result.warning is None
        (document,) = blank_rig.uploads
        assert embedded_streams(document) == _pages(scanner, [0, 2])


@dataclass(frozen=True)
class _StopCase:
    """
    One way an answer can ask for another pass just before saneless stops.

    Attributes:
        scanner: Builds the scanner the run feeds from.
        answers: The operator's answers, in order.
        detection: Whether the profile has empty-page detection on.
        passes: How many passes the run is expected to have scanned.

    """

    scanner: Callable[[], DistinctPageScanner]
    answers: tuple[PassAnswer, ...]
    detection: bool
    passes: int


_STOP_CASES = [
    pytest.param(
        _StopCase(
            scanner=lambda: DistinctPageScanner(passes=((0,), (1,))),
            answers=(_NEXT,),
            detection=False,
            passes=1,
        ),
        id="scan-next",
    ),
    pytest.param(
        _StopCase(
            scanner=lambda: DistinctPageScanner(passes=((0,), (1,), (2,))),
            answers=(_NEXT, _RESCAN),
            detection=False,
            passes=2,
        ),
        id="rescan-last",
    ),
    pytest.param(
        _StopCase(
            scanner=lambda: DistinctPageScanner(passes=((0,), (1,), (2,)), blank={1}),
            answers=(_NEXT, _RESCAN),
            detection=True,
            passes=2,
        ),
        id="rescan-at-the-blank-prompt",
    ),
    pytest.param(
        _StopCase(
            scanner=_jams_on_pass_2, answers=(_NEXT, _NEXT), detection=False, passes=2
        ),
        id="try-again-after-a-jam",
    ),
]


class TestMultiPageStopAfterAnAnswer:
    """A stop that lands after an answer was claimed starts no further pass."""

    @pytest.mark.parametrize("case", _STOP_CASES)
    def test_no_pass_starts_and_the_document_is_kept(
        self, tmp_path: Path, case: _StopCase
    ) -> None:
        """
        The claimed answer is honoured up to the pass it asks for, then the run ends.

        A pass started during a stop would outlast the bounded stop, and a run
        killed inside it never reaches the guard, so the accepted document
        would be lost; ending here keeps it as one PDF and uploads nothing.
        """
        rig = _Rig(multi_page_settings(tmp_path, detection=case.detection))
        scanner = case.scanner()
        coordinator = _StopLandsAfterAnswer(case.answers, stop_after=len(case.answers))

        with pytest.raises(ScanInterrupted):
            rig.run(scanner, coordinator)

        assert scanner.calls == case.passes
        (kept,) = rig.kept_pdfs()
        assert embedded_streams(kept) == _pages(scanner, [0])
        rig.paperless.upload_document.assert_not_called()


class _FactScanner(DistinctPageScanner):
    """
    A scanner whose passes also report what the backend measured about them.

    Keyed by the 1-based number of the ``scan_pages`` call, the way
    ``fail_on`` is: a substituted source, a per-pass cap reached, or both.
    """

    def __init__(
        self,
        *,
        passes: Sequence[Sequence[int]],
        substituted: Mapping[int, str] | None = None,
        caps: Mapping[int, PassCapReached] | None = None,
        blank: frozenset[int] = frozenset(),
    ) -> None:
        """
        Prepare the passes and the facts each one reports.

        Args:
            passes: The page indices each successive call feeds, in order.
            substituted: The requested source a call's Auto stood in for.
            caps: The per-pass cap a call reached.
            blank: The page indices to spool as blank paper.

        """
        super().__init__(passes=passes, blank=blank)
        self.substituted = dict(substituted or {})
        self.caps = dict(caps or {})

    @override
    def scan_pages(
        self, device_id: str, settings: ScanSettings, sink: PageSink
    ) -> ScanBatch:
        """
        Feed the next pass, then attach that call's facts to its batch.

        Args:
            device_id: Ignored.
            settings: Passed through.
            sink: The pipeline's sink.

        Returns:
            The pass's batch, carrying this call's facts.

        """
        batch = super().scan_pages(device_id, settings, sink)
        return dataclasses.replace(
            batch,
            substituted_source=self.substituted.get(self.calls),
            cap_reached=self.caps.get(self.calls),
        )


_AUTO_CAP = PassCapReached(50, 51, auto_source=True)


class TestMultiPagePassFacts:
    """
    A pass's backend facts finish the document warned, each worded once.

    A pass that reached its own cap left a sheet in the feeder, so the
    document finishes after it; a substitution is said once however many
    passes carried it; and a pass thrown away takes its facts with it.
    """

    def test_a_capped_pass_finishes_the_document_without_another_prompt(
        self, rig: _Rig
    ) -> None:
        """Pass 2 stops at the Auto cap: no next-pass prompt, and the pages upload."""
        scanner = _FactScanner(passes=((0,), (1, 2)), caps={2: _AUTO_CAP})
        coordinator = ScriptedPassCoordinator([_NEXT])

        result = rig.run(scanner, coordinator)

        assert len(coordinator.prompts) == 1
        assert result.outcome is ScanOutcome.SUCCESS
        assert result.warning == pass_cap_warning(3, 50, 51, auto_source=True)
        (document,) = rig.uploads
        assert embedded_streams(document) == _pages(scanner, [0, 1, 2])

    def test_a_failure_after_a_capped_pass_still_names_the_sheet_not_kept(
        self, rig: _Rig
    ) -> None:
        """A delivery that fails after the capped pass keeps its cap sentence."""
        scanner = _FactScanner(passes=((0,), (1, 2)), caps={2: _AUTO_CAP})
        coordinator = ScriptedPassCoordinator([_NEXT])
        rig.paperless.upload_document.side_effect = PaperlessError("Upload failed")

        with pytest.raises(PaperlessError) as excinfo:
            rig.run(scanner, coordinator)

        assert failure_text(excinfo.value).endswith(
            pass_cap_note(50, 51, auto_source=True)
        )

    def test_the_pass_cap_sentence_replaces_the_document_cap_sentence(
        self, rig: _Rig, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When both caps fire, the warning says to start a new document once."""
        monkeypatch.setattr(pipeline_module, "MAX_DOCUMENT_PAGES", 3)
        cap = PassCapReached(2, 3, auto_source=False)
        scanner = _FactScanner(passes=((0, 1), (2, 3)), caps={2: cap})
        coordinator = ScriptedPassCoordinator([_NEXT])

        result = rig.run(scanner, coordinator)

        assert result.warning is not None
        assert result.warning == pass_cap_warning(4, 2, 3, auto_source=False)
        assert cap_finish_warning(4, 3) not in result.warning
        assert result.pages_uploaded == 4

    def test_a_substitution_on_every_pass_is_said_once(self, rig: _Rig) -> None:
        """Two passes through a substituted Auto: one substitution sentence."""
        scanner = _FactScanner(
            passes=((0,), (1,)), substituted={1: "Flatbed", 2: "Flatbed"}
        )
        coordinator = ScriptedPassCoordinator([_NEXT, _FINISH])

        result = rig.run(scanner, coordinator)

        assert result.outcome is ScanOutcome.SUCCESS
        assert result.warning == substituted_source_warning("Flatbed")

    def test_a_capped_pass_thrown_away_at_the_blank_prompt_takes_its_cap(
        self, blank_rig: _Rig
    ) -> None:
        """Re-scan of a capped pass, then a clean pass and Finish: a plain DONE."""
        scanner = _FactScanner(
            passes=((0,), (1,), (2,)), blank=frozenset({1}), caps={2: _AUTO_CAP}
        )
        coordinator = ScriptedPassCoordinator([_NEXT, _RESCAN, _FINISH])

        result = blank_rig.run(scanner, coordinator)

        assert [prompt.wait for prompt in coordinator.prompts] == [
            PassWait.NEXT_PASS,
            PassWait.BLANK_DECISION,
            PassWait.NEXT_PASS,
        ]
        assert result.outcome is ScanOutcome.SUCCESS
        assert result.warning is None
        assert result.pages_uploaded == 2

    def test_a_substituted_pass_thrown_away_by_rescan_takes_its_fact(
        self, rig: _Rig
    ) -> None:
        """Re-scan of the one substituted pass leaves nothing to warn about."""
        scanner = _FactScanner(passes=((0,), (1,), (2,)), substituted={2: "Flatbed"})
        coordinator = ScriptedPassCoordinator([_NEXT, _RESCAN, _FINISH])

        result = rig.run(scanner, coordinator)

        assert result.warning is None
        assert result.pages_uploaded == 2

    def test_a_blank_prompt_timeout_keeps_the_pass_cap_sentence(
        self, blank_rig: _Rig
    ) -> None:
        """Nobody answered about a capped pass's blank page: both reasons are said."""
        scanner = _FactScanner(
            passes=((0,), (1, 2)), blank=frozenset({1}), caps={2: _AUTO_CAP}
        )
        coordinator = ScriptedPassCoordinator([_NEXT, PassAnswer.TIMED_OUT])

        result = blank_rig.run(scanner, coordinator)

        assert len(coordinator.prompts) == 2
        assert result.warning is not None
        assert blank_timeout_finish_warning(2, _TIMEOUT) in result.warning
        assert pass_cap_warning(2, 50, 51, auto_source=True) in result.warning
