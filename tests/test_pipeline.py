"""Tests for pipeline orchestration."""

from __future__ import annotations

import base64
import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image, ImageDraw

import saneless.pipeline as pipeline_module
import saneless.scanner.sane_backend as sane_backend_mod
from saneless.config import ProfileConfig
from saneless.exceptions import (
    ConfigError,
    FeederEmptyError,
    PaperlessError,
    PaperlessTimeoutError,
    ScanError,
)
from saneless.paperless import UploadResult
from saneless.pipeline import (
    FAILED_DIR_WARN_THRESHOLD,
    FlipAnswerSlot,
    FlipCoordinator,
    PipelineEvent,
    PipelineRequest,
    ScanResult,
    _check_disk_space,
    _interleave_duplex,
    _preserving,
    run_pipeline,
)
from saneless.scanner.base import DeviceInfo, ScannerBackend
from saneless.scanner.sane_backend import SaneBackend
from saneless.vocabulary import (
    ErrorCategory,
    FlipOutcome,
    JobState,
    ScanOutcome,
    classify_error,
)
from tests.conftest import AlwaysContinueFlipCoordinator, scan_batch
from tests.fake_sane import FakeSaneDev, FakeSaneModule

if TYPE_CHECKING:
    from collections.abc import Iterator

    from saneless.config import Settings


class TestRunPipeline:
    """Pipeline orchestration tests."""

    def test_run_pipeline_happy_path(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Full pipeline: scan -> assemble -> upload succeeds."""
        default_settings.output.tmp_dir = str(tmp_path)

        request = PipelineRequest(profile_name="default", title="Happy Path Doc")
        result = run_pipeline(
            scanner=mock_scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=request,
        )

        assert result.outcome is ScanOutcome.SUCCESS
        assert result.warning is None
        mock_scanner.scan_pages.assert_called_once()
        mock_paperless.upload_document.assert_called_once()

    def test_run_pipeline_scan_error(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Scanner raises ScanError -> pipeline raises ScanError."""
        default_settings.output.tmp_dir = str(tmp_path)

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = ScanError("Device not found")

        request = PipelineRequest(profile_name="default", title="Scan Error Doc")
        with pytest.raises(ScanError, match="Device not found"):
            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

    def test_run_pipeline_upload_error(
        self,
        mock_scanner: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Paperless raises PaperlessError -> pipeline raises PaperlessError."""
        # data_dir is pointed at tmp_path too: a failing delivery now preserves
        # the assembled PDF into <data_dir>/failed/, and a test must not write
        # that into the shared default outside pytest's own temp directory.
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.output.data_dir = str(tmp_path / "state")

        paperless = MagicMock()
        paperless.upload_document.side_effect = PaperlessError("Upload failed")

        request = PipelineRequest(profile_name="default", title="Upload Error Doc")
        with pytest.raises(PaperlessError, match="Upload failed"):
            run_pipeline(
                scanner=mock_scanner,
                paperless=paperless,
                settings=default_settings,
                request=request,
            )

    def test_run_pipeline_temp_cleanup(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """After successful run, tmp_dir has no leftover scan files."""
        default_settings.output.tmp_dir = str(tmp_path)

        request = PipelineRequest(profile_name="default", title="Cleanup Doc")
        run_pipeline(
            scanner=mock_scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=request,
        )

        # The TemporaryDirectory should be cleaned up
        remaining = list(tmp_path.iterdir())
        # Only the output PDF may remain, no tmp subdirs
        for item in remaining:
            assert not item.is_dir(), f"Leftover directory: {item}"

    def test_run_pipeline_temp_cleanup_on_error(
        self, default_settings: Settings, tmp_path: Path
    ) -> None:
        """After failed run, tmp_dir has no leftover scan files."""
        default_settings.output.tmp_dir = str(tmp_path)

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = ScanError("Boom")
        paperless = MagicMock()

        request = PipelineRequest(profile_name="default", title="Error Cleanup Doc")
        with pytest.raises(ScanError, match="Boom"):
            run_pipeline(
                scanner=scanner,
                paperless=paperless,
                settings=default_settings,
                request=request,
            )

        remaining = list(tmp_path.iterdir())
        for item in remaining:
            assert not item.is_dir(), f"Leftover directory: {item}"

    def test_run_pipeline_calls_poll(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """After upload, pipeline calls poll_task with returned UUID."""
        default_settings.output.tmp_dir = str(tmp_path)
        mock_paperless.upload_document.return_value = UploadResult(
            delivered_to_api=True, task_uuid="task-uuid-123"
        )
        mock_paperless.poll_task.return_value = {"status": "SUCCESS"}

        request = PipelineRequest(profile_name="default", title="Poll Doc")
        run_pipeline(
            scanner=mock_scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=request,
        )

        mock_paperless.poll_task.assert_called_once()
        call_args = mock_paperless.poll_task.call_args
        assert call_args[0][0] == "task-uuid-123"

    def test_run_pipeline_status_callback(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Pipeline calls status_callback with PipelineEvent enum values in order."""
        default_settings.output.tmp_dir = str(tmp_path)
        events: list[PipelineEvent] = []

        request = PipelineRequest(
            profile_name="default",
            title="Status Doc",
            status_callback=events.append,
        )
        run_pipeline(
            scanner=mock_scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=request,
        )

        assert events[0] is PipelineEvent.SCANNING
        assert events[1] is PipelineEvent.ASSEMBLING
        assert events[2] is PipelineEvent.UPLOADING
        assert events[3] is PipelineEvent.DONE


def _make_content_image(color: str = "black") -> Image.Image:
    """Create an image with visible content (not empty)."""
    img = Image.new("RGB", (200, 300), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, 180, 280], fill=color)
    return img


def _make_empty_image() -> Image.Image:
    """Create a nearly-white image that should be detected as empty."""
    return Image.new("RGB", (200, 300), (254, 254, 254))


class TestInterleave:
    """Unit tests for _interleave_duplex."""

    def test_interleave_basic(self) -> None:
        """Interleave 3 fronts + 3 backs correctly reverses backs."""
        fronts = [Image.new("RGB", (10, 10), c) for c in ["red", "green", "blue"]]
        backs = [Image.new("RGB", (10, 10), c) for c in ["cyan", "magenta", "yellow"]]
        result = _interleave_duplex(fronts, backs)
        assert len(result) == 6
        # Backs are reversed: yellow, magenta, cyan
        # Result: red, yellow, green, magenta, blue, cyan
        assert result[0] is fronts[0]
        assert result[1] is backs[2]  # reversed
        assert result[2] is fronts[1]
        assert result[3] is backs[1]  # reversed
        assert result[4] is fronts[2]
        assert result[5] is backs[0]  # reversed

    def test_interleave_single_page(self) -> None:
        """Single front + single back works."""
        f = [Image.new("RGB", (10, 10), "red")]
        b = [Image.new("RGB", (10, 10), "blue")]
        result = _interleave_duplex(f, b)
        assert len(result) == 2
        assert result[0] is f[0]
        assert result[1] is b[0]

    def test_interleave_count_mismatch_raises(self) -> None:
        """Mismatched front/back counts raise ScanError."""
        fronts = [Image.new("RGB", (10, 10)) for _ in range(3)]
        backs = [Image.new("RGB", (10, 10)) for _ in range(2)]
        with pytest.raises(ScanError, match="Page count mismatch: 3 fronts, 2 backs"):
            _interleave_duplex(fronts, backs)


class TestPipelineThumbnail:
    """Thumbnail generation in pipeline."""

    def test_thumbnail_callback_called_flatbed(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Flatbed scan calls thumbnail_callback with non-empty base64 string."""
        default_settings.output.tmp_dir = str(tmp_path)
        # Use a content image so it doesn't get filtered as empty
        mock_scanner.scan_pages.return_value = scan_batch([_make_content_image()])

        thumb_results: list[str] = []
        request = PipelineRequest(
            profile_name="default",
            title="Thumb Test",
            thumbnail_callback=thumb_results.append,
        )
        run_pipeline(
            scanner=mock_scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=request,
        )

        assert len(thumb_results) == 1
        assert len(thumb_results[0]) > 0
        # Should be valid base64
        base64.b64decode(thumb_results[0])

    def test_no_thumbnail_callback_ok(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Pipeline works without thumbnail_callback."""
        default_settings.output.tmp_dir = str(tmp_path)
        mock_scanner.scan_pages.return_value = scan_batch([_make_content_image()])

        request = PipelineRequest(
            profile_name="default",
            title="No Thumb Test",
        )
        # Should not raise
        run_pipeline(
            scanner=mock_scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=request,
        )


class TestPipelineEmptyPageFilter:
    """Empty page filtering in pipeline."""

    def test_empty_pages_filtered(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Pipeline with 5 pages (3 content + 2 empty) assembles PDF with only 3."""
        default_settings.output.tmp_dir = str(tmp_path)

        content_pages = [_make_content_image(c) for c in ["black", "red", "blue"]]
        empty_pages = [_make_empty_image(), _make_empty_image()]
        all_pages = [
            content_pages[0],
            empty_pages[0],
            content_pages[1],
            empty_pages[1],
            content_pages[2],
        ]

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.return_value = scan_batch(all_pages)

        request = PipelineRequest(profile_name="default", title="Filter Test")

        with patch("saneless.pipeline.assemble_pdf") as mock_assemble:
            mock_assemble.return_value = tmp_path / "output.pdf"
            (tmp_path / "output.pdf").write_bytes(b"%PDF-fake")

            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

            # assemble_pdf should receive only the 3 content pages
            called_images = mock_assemble.call_args[0][0]
            assert len(called_images) == 3

    def test_custom_thresholds_from_profile(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Custom thresholds from ProfileConfig are passed to filter_empty_pages."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].empty_page_mean_threshold = 200.0
        default_settings.profiles["default"].empty_page_stddev_threshold = 10.0

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.return_value = scan_batch([_make_content_image()])

        request = PipelineRequest(profile_name="default", title="Threshold Test")

        with patch("saneless.pipeline.filter_empty_pages", wraps=None) as mock_filter:
            mock_filter.return_value = [_make_content_image()]
            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )
            mock_filter.assert_called_once()
            _, kwargs = mock_filter.call_args
            assert kwargs["mean_threshold"] == 200.0
            assert kwargs["stddev_threshold"] == 10.0

    def test_all_pages_empty_raises(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        A non-empty batch that detection empties says "All pages were blank".

        EXC-03: the blank message is reserved for detection really removing
        every page, so it names what happened rather than an empty scan.
        """
        default_settings.output.tmp_dir = str(tmp_path)

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.return_value = scan_batch(
            [_make_empty_image(), _make_empty_image()]
        )

        request = PipelineRequest(profile_name="default", title="All Empty Test")
        with pytest.raises(ScanError, match=r"^All pages were blank$"):
            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )


class TestFlipAnswerSlot:
    """
    The one claim-once answer both flip coordinators compose (IN-02).

    Every wait here is bounded, and none sleeps: a wait on an answered slot
    returns because its event is already set, and a wait on an unanswered one
    is given ``0``.
    """

    def test_a_new_slot_is_unanswered(self) -> None:
        """Nothing has claimed a freshly built slot."""
        assert FlipAnswerSlot().answer is None

    def test_the_first_offer_claims_and_a_later_one_is_dropped(self) -> None:
        """D-16: the first offer is the answer, and a later offer changes nothing."""
        slot = FlipAnswerSlot()
        assert slot.offer(FlipOutcome.CONTINUED) is True
        assert slot.answer is FlipOutcome.CONTINUED
        assert slot.offer(FlipOutcome.ABORTED) is False
        assert slot.answer is FlipOutcome.CONTINUED

    def test_settle_claims_an_unanswered_slot(self) -> None:
        """Settling an unanswered slot makes the offered outcome the answer."""
        slot = FlipAnswerSlot()
        assert slot.settle(FlipOutcome.TIMED_OUT) is FlipOutcome.TIMED_OUT
        assert slot.answer is FlipOutcome.TIMED_OUT

    def test_settle_returns_the_answer_already_in_effect(self) -> None:
        """A settle that loses the race hands back the answer that beat it."""
        slot = FlipAnswerSlot()
        slot.offer(FlipOutcome.CONTINUED)
        assert slot.settle(FlipOutcome.TIMED_OUT) is FlipOutcome.CONTINUED
        assert slot.answer is FlipOutcome.CONTINUED

    def test_settle_wakes_a_waiter(self) -> None:
        """A settled slot's wait returns at once: settle sets the event too."""
        slot = FlipAnswerSlot()
        slot.settle(FlipOutcome.ABORTED)
        started = time.monotonic()
        slot.wait(5)
        assert time.monotonic() - started < 1

    def test_a_zero_wait_on_an_unanswered_slot_returns_promptly(self) -> None:
        """``wait(0)`` is a valid bound and returns without an answer."""
        slot = FlipAnswerSlot()
        started = time.monotonic()
        slot.wait(0)
        assert time.monotonic() - started < 1
        assert slot.answer is None

    def test_an_offer_wakes_a_waiter(self) -> None:
        """After a claiming offer, a long wait returns at once."""
        slot = FlipAnswerSlot()
        slot.offer(FlipOutcome.CONTINUED)
        started = time.monotonic()
        slot.wait(5)
        assert time.monotonic() - started < 1

    def test_racing_offers_claim_exactly_once(self) -> None:
        """
        Twenty threads offering at once yield exactly one claim.

        A barrier releases every thread together, so the offers genuinely
        contend for the lock; the answer left behind is the winner's outcome.
        """
        slot = FlipAnswerSlot()
        contenders = 20
        barrier = threading.Barrier(contenders, timeout=5)
        results: list[tuple[FlipOutcome, bool]] = []
        results_lock = threading.Lock()

        def _offer(outcome: FlipOutcome) -> None:
            """Wait for the others, then offer ``outcome`` and record the result."""
            barrier.wait()
            claimed = slot.offer(outcome)
            with results_lock:
                results.append((outcome, claimed))

        threads = [
            threading.Thread(
                target=_offer,
                args=(FlipOutcome.CONTINUED if i % 2 else FlipOutcome.ABORTED,),
            )
            for i in range(contenders)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert len(results) == contenders
        winners = [outcome for outcome, claimed in results if claimed]
        assert len(winners) == 1
        assert slot.answer is winners[0]

    def test_the_slot_is_public_api(self) -> None:
        """``FlipAnswerSlot`` is exported next to the coordinator contract."""
        assert "FlipAnswerSlot" in pipeline_module.__all__


class _FixedFlipCoordinator(FlipCoordinator):
    """
    A flip coordinator that resolves to one chosen outcome, and records the ask.

    Subclasses the ABC for the same reason as ``AlwaysContinueFlipCoordinator``:
    a contract change must reach this stub through the type checkers.
    """

    def __init__(
        self,
        outcome: FlipOutcome,
        events: list[PipelineEvent] | None = None,
    ) -> None:
        """
        Remember the outcome to answer with and the event log to snapshot.

        Args:
            outcome: What every wait resolves to.
            events: The status-callback log, snapshotted at each wait so a test
                can see which events preceded it.

        """
        self._outcome = outcome
        self._events = events if events is not None else []
        self.timeouts: list[float] = []
        self.events_at_wait: list[PipelineEvent] = []

    def wait_for_flip(self, timeout: float) -> FlipOutcome:
        """
        Record the timeout and the events so far, then answer at once.

        Args:
            timeout: The bound the pipeline asked for.

        Returns:
            The outcome this stub was built with.

        """
        self.timeouts.append(timeout)
        self.events_at_wait = list(self._events)
        return self._outcome


class TestZeroPages:
    """
    An empty batch is reported truthfully at the pipeline boundary (EXC-03, N-06).

    Before this check an empty batch read "All pages were detected as empty"
    with detection on, and leaked img2pdf's bare ``ValueError`` with it off or
    on an empty duplex half. The SANE backend never returns an empty batch --
    an empty feeder raises ``FeederEmptyError`` (Phase 24 D-03) -- so these
    tests drive the pipeline's contract check with a stubbed backend.
    """

    @pytest.mark.parametrize("detection", [True, False])
    def test_an_empty_simplex_batch_says_no_pages_were_scanned(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
        *,
        detection: bool,
    ) -> None:
        """
        An empty simplex batch raises "No pages were scanned", detection on or off.

        Assembly and upload are never reached, so img2pdf never sees an empty
        list (N-06).
        """
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].enable_empty_page_detection = detection

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.return_value = scan_batch([])

        request = PipelineRequest(profile_name="default", title="Zero Pages")
        with (
            patch("saneless.pipeline.assemble_pdf") as mock_assemble,
            pytest.raises(ScanError, match=r"^No pages were scanned$"),
        ):
            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

        mock_assemble.assert_not_called()
        mock_paperless.upload_document.assert_not_called()

    def test_an_empty_pass_a_fails_before_anyone_is_asked_to_flip(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """An empty manual-duplex pass A raises before the flip prompt (EXC-03)."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.return_value = scan_batch([])

        coordinator = _FixedFlipCoordinator(FlipOutcome.CONTINUED)
        request = PipelineRequest(
            profile_name="default",
            title="Empty Pass A",
            flip_coordinator=coordinator,
        )
        with pytest.raises(ScanError, match=r"^No pages were scanned$"):
            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

        assert coordinator.timeouts == []
        assert scanner.scan_pages.call_count == 1
        mock_paperless.upload_document.assert_not_called()

    def test_an_empty_pass_b_fails_before_any_half_is_assembled(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        An empty manual-duplex pass B raises before the count comparison (EXC-03).

        Without the check the counts differ and the mismatch recovery would try
        to assemble the empty back half.
        """
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = [
            scan_batch([_make_content_image()]),
            scan_batch([]),
        ]

        request = PipelineRequest(
            profile_name="default",
            title="Empty Pass B",
            flip_coordinator=_FixedFlipCoordinator(FlipOutcome.CONTINUED),
        )
        with (
            patch("saneless.pipeline.assemble_pdf") as mock_assemble,
            pytest.raises(ScanError, match=r"^No pages were scanned$"),
        ):
            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

        assert scanner.scan_pages.call_count == 2
        mock_assemble.assert_not_called()
        mock_paperless.upload_document.assert_not_called()

    def test_all_blank_pages_say_all_pages_were_blank(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Detection removing every page of a non-empty batch is "blank" (EXC-03)."""
        default_settings.output.tmp_dir = str(tmp_path)

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.return_value = scan_batch([_make_empty_image()])

        request = PipelineRequest(profile_name="default", title="All Blank")
        with (
            patch("saneless.pipeline.assemble_pdf") as mock_assemble,
            pytest.raises(ScanError, match=r"^All pages were blank$"),
        ):
            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

        mock_assemble.assert_not_called()

    def test_an_empty_feeder_keeps_its_own_message(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        ``FeederEmptyError`` propagates unchanged (Phase 24 D-03).

        The zero-page check runs on a returned batch, so it can never replace
        the backend's more specific feeder message.
        """
        default_settings.output.tmp_dir = str(tmp_path)

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = FeederEmptyError("No paper detected in feeder")

        request = PipelineRequest(profile_name="default", title="Feeder Empty")
        with pytest.raises(FeederEmptyError, match=r"^No paper detected in feeder$"):
            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

        mock_paperless.upload_document.assert_not_called()


class TestManualDuplex:
    """Manual duplex pipeline tests."""

    def test_manual_duplex_happy_path(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Manual duplex: 3 fronts + 3 backs -> 6 interleaved pages."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"

        fronts = [_make_content_image(c) for c in ["red", "green", "blue"]]
        backs = [_make_content_image(c) for c in ["cyan", "magenta", "yellow"]]

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = [scan_batch(fronts), scan_batch(backs)]

        request = PipelineRequest(
            profile_name="default",
            title="Duplex Test",
            flip_coordinator=AlwaysContinueFlipCoordinator(),
        )

        with patch("saneless.pipeline.assemble_pdf") as mock_assemble:
            mock_assemble.return_value = tmp_path / "output.pdf"
            (tmp_path / "output.pdf").write_bytes(b"%PDF-fake")

            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

            called_images = mock_assemble.call_args[0][0]
            assert len(called_images) == 6

    def test_duplex_mismatch_saves_partial_pdfs(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Pass A yields 3 pages, pass B yields 2 -> saves both as separate PDFs."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"

        fronts = [_make_content_image() for _ in range(3)]
        backs = [_make_content_image() for _ in range(2)]

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = [scan_batch(fronts), scan_batch(backs)]

        request = PipelineRequest(
            profile_name="default",
            title="Mismatch Test",
            flip_coordinator=AlwaysContinueFlipCoordinator(),
        )

        result = run_pipeline(
            scanner=scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=request,
        )

        # Both partial PDFs uploaded
        assert mock_paperless.upload_document.call_count == 2
        first_call = mock_paperless.upload_document.call_args_list[0]
        second_call = mock_paperless.upload_document.call_args_list[1]
        assert "(fronts)" in first_call[0][1]
        assert "(backs)" in second_call[0][1]

        # Returns SUCCESS with a warning, not an error
        assert result.outcome is ScanOutcome.SUCCESS
        assert result.warning is not None
        assert "Page count mismatch: 3 fronts, 2 backs" in result.warning
        # Both partial PDFs count as uploaded pages
        assert result.pages_scanned == 5
        assert result.pages_removed == 0
        assert result.pages_uploaded == 5

    def test_duplex_mismatch_reports_fallback_when_upload_falls_back(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        A mismatch whose partial PDFs only reached the consume dir is FALLBACK.

        The mismatch path uploads twice. If either upload fell back, the run did
        not reach paperless-ngx and must not claim SUCCESS -- that is precisely
        the lie ScanOutcome.FALLBACK exists to prevent (CTR-02).
        """
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"
        mock_paperless.upload_document.return_value = UploadResult(
            delivered_to_api=False,
            consume_dir_path=tmp_path / "consume" / "doc.pdf",
        )

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = [
            scan_batch([_make_content_image() for _ in range(3)]),
            scan_batch([_make_content_image() for _ in range(2)]),
        ]

        result = run_pipeline(
            scanner=scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=PipelineRequest(
                profile_name="default",
                title="Mismatch Fallback",
                flip_coordinator=AlwaysContinueFlipCoordinator(),
            ),
        )

        assert result.outcome is ScanOutcome.FALLBACK
        assert result.warning is not None
        assert "Page count mismatch" in result.warning
        mock_paperless.poll_task.assert_not_called()

    def test_duplex_mismatch_is_fallback_when_only_one_upload_falls_back(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """A partially-delivered mismatch is still FALLBACK, not SUCCESS (CTR-02)."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"
        mock_paperless.upload_document.side_effect = [
            UploadResult(delivered_to_api=True, task_uuid="fronts-task"),
            UploadResult(
                delivered_to_api=False,
                consume_dir_path=tmp_path / "consume" / "backs.pdf",
            ),
        ]

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = [
            scan_batch([_make_content_image() for _ in range(3)]),
            scan_batch([_make_content_image() for _ in range(2)]),
        ]

        result = run_pipeline(
            scanner=scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=PipelineRequest(
                profile_name="default",
                title="Half Delivered",
                flip_coordinator=AlwaysContinueFlipCoordinator(),
            ),
        )

        assert result.outcome is ScanOutcome.FALLBACK

    def test_duplex_match_still_interleaves_normally(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Matching front/back counts still interleave and upload single PDF."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"

        fronts = [_make_content_image("red"), _make_content_image("blue")]
        backs = [_make_content_image("green"), _make_content_image("yellow")]

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = [scan_batch(fronts), scan_batch(backs)]

        request = PipelineRequest(
            profile_name="default",
            title="Normal Duplex",
            flip_coordinator=AlwaysContinueFlipCoordinator(),
        )

        with patch("saneless.pipeline.assemble_pdf") as mock_assemble:
            mock_assemble.return_value = tmp_path / "output.pdf"
            (tmp_path / "output.pdf").write_bytes(b"%PDF-fake")

            result = run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

            # Normal path: single PDF uploaded
            mock_paperless.upload_document.assert_called_once()
            assert result.warning is None

    def test_manual_duplex_empty_page_after_interleave(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Empty page detection runs on interleaved result, not individual passes."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"

        # 2 fronts: 1 content + 1 content, 2 backs: 1 empty + 1 content
        fronts = [_make_content_image("red"), _make_content_image("blue")]
        backs = [_make_empty_image(), _make_content_image("green")]

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = [scan_batch(fronts), scan_batch(backs)]

        request = PipelineRequest(
            profile_name="default",
            title="Duplex Filter Test",
            flip_coordinator=AlwaysContinueFlipCoordinator(),
        )

        with patch("saneless.pipeline.assemble_pdf") as mock_assemble:
            mock_assemble.return_value = tmp_path / "output.pdf"
            (tmp_path / "output.pdf").write_bytes(b"%PDF-fake")

            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

            # 4 interleaved - 1 empty = 3 pages
            called_images = mock_assemble.call_args[0][0]
            assert len(called_images) == 3

    def test_manual_duplex_thumbnail_from_first_page(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Thumbnail generated from first front page in manual duplex."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"

        fronts = [_make_content_image()]
        backs = [_make_content_image()]

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = [scan_batch(fronts), scan_batch(backs)]

        thumb_results: list[str] = []
        request = PipelineRequest(
            profile_name="default",
            title="Duplex Thumb Test",
            thumbnail_callback=thumb_results.append,
            flip_coordinator=AlwaysContinueFlipCoordinator(),
        )

        run_pipeline(
            scanner=scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=request,
        )

        assert len(thumb_results) == 1
        assert len(thumb_results[0]) > 0

    def test_manual_duplex_waits_on_the_coordinator_with_the_configured_timeout(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        The flip wait is one bounded call to the coordinator (DPLX-04, DPLX-05).

        ``AWAITING_FLIP`` is announced before the wait and ``SCANNING_REVERSE``
        only after it, and the timeout handed over is the configured one.
        """
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.output.flip_timeout_seconds = 42
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = [
            scan_batch([_make_content_image()]),
            scan_batch([_make_content_image()]),
        ]

        events: list[PipelineEvent] = []
        coordinator = _FixedFlipCoordinator(FlipOutcome.CONTINUED, events)
        request = PipelineRequest(
            profile_name="default",
            title="Coordinator Wait Test",
            status_callback=events.append,
            flip_coordinator=coordinator,
        )

        run_pipeline(
            scanner=scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=request,
        )

        assert coordinator.timeouts == [42]
        assert coordinator.events_at_wait[-1] is PipelineEvent.AWAITING_FLIP
        assert PipelineEvent.SCANNING_REVERSE not in coordinator.events_at_wait
        assert PipelineEvent.SCANNING_REVERSE in events

    def test_manual_duplex_abort_at_the_flip_prompt_raises(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """ABORTED fails the run naming the flip prompt, before pass B (D-15)."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.return_value = scan_batch([_make_content_image()])

        request = PipelineRequest(
            profile_name="default",
            title="Abort Test",
            flip_coordinator=_FixedFlipCoordinator(FlipOutcome.ABORTED),
        )

        with pytest.raises(ScanError, match="flip prompt"):
            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

        assert scanner.scan_pages.call_count == 1
        mock_paperless.upload_document.assert_not_called()

    def test_manual_duplex_flip_wait_timeout_raises(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """TIMED_OUT fails the run naming the flip wait and its timeout (DPLX-05)."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.output.flip_timeout_seconds = 17
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.return_value = scan_batch([_make_content_image()])

        request = PipelineRequest(
            profile_name="default",
            title="Timeout Test",
            flip_coordinator=_FixedFlipCoordinator(FlipOutcome.TIMED_OUT),
        )

        with pytest.raises(ScanError, match="flip wait") as excinfo:
            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

        assert "17" in str(excinfo.value)
        assert scanner.scan_pages.call_count == 1
        mock_paperless.upload_document.assert_not_called()


def _two_pass_scanner() -> MagicMock:
    """
    Build a scanner mock whose ``scan_pages`` calls can be counted.

    It has two batches queued, so a simplex run makes one call and a manual
    duplex run makes two. ``get_devices`` reports one device, so an
    auto-detecting run can proceed if nothing stops it first.
    """
    scanner = MagicMock(spec=ScannerBackend)
    scanner.get_devices.return_value = [
        DeviceInfo(name="test:auto:001", vendor="V", model="M", device_type="t"),
    ]
    scanner.scan_pages.side_effect = [
        scan_batch([_make_content_image("red")]),
        scan_batch([_make_content_image("blue")]),
    ]
    return scanner


class TestDuplexStrategy:
    """
    ``profile.duplex`` alone chooses the scanning strategy (DPLX-01, DPLX-03).

    ``source`` is a pure SANE value: a source that merely reads like manual
    duplex must not start the two-pass flow, and a manual-duplex profile must
    not need a special source name.
    """

    def test_manual_duplex_without_a_coordinator_is_refused_before_the_scanner(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        No coordinator means no run, and the device is never contacted.

        The device is left empty so that ``_resolve_device`` would call
        ``get_devices``. The refusal has to come before that call, and so before
        the first ``scan_pages`` call too.
        """
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.scanner.device = ""
        default_settings.profiles["default"] = ProfileConfig(
            source="ADF Front", duplex="manual"
        )
        scanner = _two_pass_scanner()

        with pytest.raises(ConfigError) as excinfo:
            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=PipelineRequest(profile_name="default", title="No Coordinator"),
            )

        message = str(excinfo.value)
        assert "'default'" in message
        assert "flip coordinator" in message
        scanner.get_devices.assert_not_called()
        scanner.scan_pages.assert_not_called()
        mock_paperless.upload_document.assert_not_called()

    def test_a_manual_duplex_looking_source_on_a_simplex_profile_runs_simplex(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """An explicit ``duplex = "none"`` wins over a legacy-looking source."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"] = ProfileConfig(
            source="Manual Duplex", duplex="none"
        )
        scanner = _two_pass_scanner()

        result = run_pipeline(
            scanner=scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=PipelineRequest(profile_name="default", title="Looks Duplex"),
        )

        assert scanner.scan_pages.call_count == 1
        assert result.pages_scanned == 1

    def test_manual_duplex_needs_no_special_source_name(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """A ``duplex = "manual"`` profile on a plain feeder source scans twice."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"] = ProfileConfig(
            source="ADF Front", duplex="manual"
        )
        scanner = _two_pass_scanner()

        result = run_pipeline(
            scanner=scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=PipelineRequest(
                profile_name="default",
                title="Plain Feeder Duplex",
                flip_coordinator=AlwaysContinueFlipCoordinator(),
            ),
        )

        assert scanner.scan_pages.call_count == 2
        assert result.pages_scanned == 2


class TestManualDuplexOverTheSharedFake:
    """
    A manual duplex run driven through a real SaneBackend (SCNR-07, M-32).

    Every other pipeline test here hands ``run_pipeline`` a
    ``MagicMock(spec=ScannerBackend)``, which can only ever return what the test
    already told it to return.  A SANE-level defect on a duplex path -- a source
    never assigned to the device, a feeder never rewound, a second pass handing
    back the first pass's sheets -- cannot surface through a mock like that,
    which is M-32's actual complaint.  This test drives the pipeline through the
    real backend over the one shared fake, so such a defect can.

    The two passes share one device handle.  ``scan_pages`` opens and closes the
    device per pass and drains the feeder to its end, so the stack has to be
    reloaded between them -- which is precisely the physical act manual duplex
    asks the operator to perform.  The pipeline announces that moment with
    ``AWAITING_FLIP`` *before* it asks the flip coordinator, so the status
    callback is the honest place to do the reload: no second device, no thread,
    and the reload happens exactly when the operator's would.
    """

    def test_both_passes_run_through_the_backend_and_interleave(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Three fronts and three backs become six interleaved pages.

        The device is named the way consumer feeders really are: it reports
        ``"Automatic Document Feeder"``, with no plain ``"ADF"`` and no
        manual-duplex pseudo-source, while the profile says ``source = "ADF"``
        as the how-to teaches. Only resolving the feeder from the device's own
        list can make this pass (C-01, D-02).
        """
        default_settings.output.tmp_dir = str(tmp_path)
        profile = default_settings.profiles["default"]
        profile.source = "ADF"
        profile.duplex = "manual"
        # The fake carries the real device's list constraints, which reject an
        # unlisted value -- so the mode is the device's own spelling.
        profile.mode = "Color"

        dev = FakeSaneDev()
        dev.report_sources(["Flatbed", "Automatic Document Feeder"])
        fronts = [_make_content_image(c) for c in ["red", "green", "blue"]]
        backs = [_make_content_image(c) for c in ["cyan", "magenta", "yellow"]]
        dev.load_feeder(fronts)
        monkeypatch.setattr(sane_backend_mod, "sane", FakeSaneModule(device=dev))

        def _reload_the_stack(event: PipelineEvent) -> None:
            """Put the flipped stack back when the pipeline asks for it."""
            if event is PipelineEvent.AWAITING_FLIP:
                dev.load_feeder(backs)

        request = PipelineRequest(
            profile_name="default",
            title="Duplex over the shared fake",
            status_callback=_reload_the_stack,
            flip_coordinator=AlwaysContinueFlipCoordinator(),
        )

        with patch("saneless.pipeline.assemble_pdf") as mock_assemble:
            mock_assemble.return_value = tmp_path / "output.pdf"
            (tmp_path / "output.pdf").write_bytes(b"%PDF-fake")

            result = run_pipeline(
                scanner=SaneBackend(),
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

            assembled = mock_assemble.call_args[0][0]

        assert len(assembled) == 6
        assert result.outcome is ScanOutcome.SUCCESS
        # The backend really drove the device on both passes: the source was
        # assigned each time, and twelve feeder calls is six start/snap pairs.
        assert dev.assignments.count("source") == 2
        assert dev.calls.count("snap") == 6
        assert dev.source == "Automatic Document Feeder"

    def test_a_device_with_no_feeder_refuses_before_pass_a(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        A flatbed-plus-Auto device fails loudly instead of snapshotting twice.

        This is C-01's silent path: ``Auto`` on offer, ``auto_source_mode`` at
        its ``"flatbed"`` default, and manual duplex used to take one platen
        snapshot per pass and report a green Complete. Now no page is taken,
        the operator is never asked to flip, and nothing is uploaded.
        """
        default_settings.output.tmp_dir = str(tmp_path)
        profile = default_settings.profiles["default"]
        profile.source = "ADF"
        profile.duplex = "manual"
        profile.mode = "Color"

        dev = FakeSaneDev()
        dev.report_sources(["Flatbed", "Auto"])
        monkeypatch.setattr(sane_backend_mod, "sane", FakeSaneModule(device=dev))

        events: list[PipelineEvent] = []
        request = PipelineRequest(
            profile_name="default",
            title="No feeder",
            status_callback=events.append,
            flip_coordinator=AlwaysContinueFlipCoordinator(),
        )

        with pytest.raises(ScanError, match="feeder") as excinfo:
            run_pipeline(
                scanner=SaneBackend(),
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

        assert "'Auto'" in str(excinfo.value)
        assert dev.calls == []
        assert PipelineEvent.AWAITING_FLIP not in events
        mock_paperless.upload_document.assert_not_called()


class TestExifStripped:
    """EXIF stripping before PDF assembly."""

    def test_exif_stripped_before_pdf(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Images passed to assemble_pdf have no 'exif' key in .info."""
        default_settings.output.tmp_dir = str(tmp_path)

        img = _make_content_image()
        img.info["exif"] = b"fake-exif-data"

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.return_value = scan_batch([img])

        request = PipelineRequest(profile_name="default", title="EXIF Test")

        with patch("saneless.pipeline.assemble_pdf") as mock_assemble:
            mock_assemble.return_value = tmp_path / "output.pdf"
            (tmp_path / "output.pdf").write_bytes(b"%PDF-fake")

            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

            called_images = mock_assemble.call_args[0][0]
            for img in called_images:
                assert "exif" not in img.info


class TestEmptyPageDetectionToggle:
    """Empty page detection toggle gating in pipeline."""

    def test_empty_page_filter_skipped_when_disabled(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """When enable_empty_page_detection=False, all images kept (none filtered)."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].enable_empty_page_detection = False

        # Use images that would normally be filtered as empty
        empty_pages = [_make_empty_image(), _make_empty_image()]
        content_page = _make_content_image()
        all_pages = [content_page, *empty_pages]

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.return_value = scan_batch(all_pages)

        request = PipelineRequest(profile_name="default", title="Toggle Test")

        with patch("saneless.pipeline.assemble_pdf") as mock_assemble:
            mock_assemble.return_value = tmp_path / "output.pdf"
            (tmp_path / "output.pdf").write_bytes(b"%PDF-fake")

            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

            # All 3 images should be kept since detection is disabled
            called_images = mock_assemble.call_args[0][0]
            assert len(called_images) == 3


class TestFlatbedStillWorks:
    """Flatbed regression tests."""

    def test_flatbed_single_page_with_thumbnail(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Single-page flatbed scan produces correct PDF with thumbnail."""
        default_settings.output.tmp_dir = str(tmp_path)

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.return_value = scan_batch([_make_content_image()])

        thumb_results: list[str] = []
        request = PipelineRequest(
            profile_name="default",
            title="Flatbed Test",
            thumbnail_callback=thumb_results.append,
        )

        run_pipeline(
            scanner=scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=request,
        )

        assert len(thumb_results) == 1
        mock_paperless.upload_document.assert_called_once()


class TestDiskSpaceCheck:
    """Disk space pre-flight check tests."""

    def test_disk_space_check_passes_when_sufficient(self, tmp_path: Path) -> None:
        """No exception when free space exceeds minimum."""
        _check_disk_space(str(tmp_path), 1)

    def test_disk_space_check_fails_when_insufficient(self, tmp_path: Path) -> None:
        """Raises ScanError when free space below threshold."""
        with pytest.raises(ScanError, match="Insufficient disk space"):
            _check_disk_space(str(tmp_path), 999_999_999)


class TestPipelineEventEnum:
    """PipelineEvent StrEnum tests."""

    def test_pipeline_event_enum_members(self) -> None:
        """All 6 PipelineEvent members exist with correct string values."""
        assert PipelineEvent.SCANNING == "SCANNING"
        assert PipelineEvent.AWAITING_FLIP == "AWAITING_FLIP"
        assert PipelineEvent.SCANNING_REVERSE == "SCANNING_REVERSE"
        assert PipelineEvent.ASSEMBLING == "ASSEMBLING"
        assert PipelineEvent.UPLOADING == "UPLOADING"
        assert PipelineEvent.DONE == "DONE"
        assert len(PipelineEvent) == 6

    def test_pipeline_emits_enum_events(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """All status_callback values are PipelineEvent instances, not strings."""
        default_settings.output.tmp_dir = str(tmp_path)
        events: list[object] = []

        request = PipelineRequest(
            profile_name="default",
            title="Enum Check",
            status_callback=events.append,
        )
        run_pipeline(
            scanner=mock_scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=request,
        )

        assert len(events) > 0
        for event in events:
            assert isinstance(event, PipelineEvent)

    @pytest.mark.parametrize("event", list(PipelineEvent))
    def test_job_state_projection_is_total(self, event: PipelineEvent) -> None:
        """Every PipelineEvent projects to a JobState, with no None escape (DPLX-06)."""
        assert isinstance(event.job_state, JobState)

    def test_job_state_projection_mapping(self) -> None:
        """Each state-changing event names the state the worker persists (CTR-01)."""
        assert PipelineEvent.SCANNING.job_state is JobState.SCANNING
        assert PipelineEvent.AWAITING_FLIP.job_state is JobState.AWAITING_FLIP
        assert PipelineEvent.SCANNING_REVERSE.job_state is JobState.SCANNING_REVERSE
        assert PipelineEvent.ASSEMBLING.job_state is JobState.ASSEMBLING
        assert PipelineEvent.UPLOADING.job_state is JobState.UPLOADING
        assert PipelineEvent.DONE.job_state is JobState.DONE

    def test_scanning_reverse_projects_to_its_own_job_state(self) -> None:
        """
        Pass B persists SCANNING_REVERSE, so the job leaves AWAITING_FLIP (DPLX-06).

        While this projected to None the job stayed AWAITING_FLIP for the whole
        of pass B, leaving the flip prompt and its dead Abort on screen as pages fed.
        """
        assert PipelineEvent.SCANNING_REVERSE.job_state is JobState.SCANNING_REVERSE


class TestScanResultContract:
    """run_pipeline's typed ScanResult return (CTR-03)."""

    def test_success_outcome_and_page_counts(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Three pages scanned, one blank dropped, two uploaded."""
        default_settings.output.tmp_dir = str(tmp_path)

        all_pages = [
            _make_content_image("black"),
            _make_empty_image(),
            _make_content_image("red"),
        ]
        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.return_value = scan_batch(all_pages)

        request = PipelineRequest(profile_name="default", title="Counts Test")

        with patch("saneless.pipeline.assemble_pdf") as mock_assemble:
            mock_assemble.return_value = tmp_path / "output.pdf"
            (tmp_path / "output.pdf").write_bytes(b"%PDF-fake")

            result = run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

        assert isinstance(result, ScanResult)
        assert result.outcome is ScanOutcome.SUCCESS
        assert result.pages_scanned == 3
        assert result.pages_removed == 1
        assert result.pages_uploaded == 2
        assert result.warning is None

    def test_pages_removed_zero_when_detection_disabled(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """With empty-page detection off nothing is removed, blanks included."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].enable_empty_page_detection = False

        all_pages = [_make_content_image(), _make_empty_image(), _make_empty_image()]
        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.return_value = scan_batch(all_pages)

        request = PipelineRequest(profile_name="default", title="No Filter Test")

        with patch("saneless.pipeline.assemble_pdf") as mock_assemble:
            mock_assemble.return_value = tmp_path / "output.pdf"
            (tmp_path / "output.pdf").write_bytes(b"%PDF-fake")

            result = run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

        assert result.pages_scanned == 3
        assert result.pages_removed == 0
        assert result.pages_uploaded == 3

    def test_fallback_outcome_when_not_delivered_to_api(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """An upload that only reached the consume dir reports FALLBACK."""
        default_settings.output.tmp_dir = str(tmp_path)
        mock_paperless.upload_document.return_value = UploadResult(
            delivered_to_api=False,
            consume_dir_path=tmp_path / "consume" / "doc.pdf",
        )

        request = PipelineRequest(profile_name="default", title="Fallback Doc")
        result = run_pipeline(
            scanner=mock_scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=request,
        )

        assert result.outcome is ScanOutcome.FALLBACK
        # OUTC-02: the FALLBACK state says the document took the other route;
        # the warning says what that route did not do.  A state on its own
        # leaves the user to guess why their title and tags never appeared.
        assert result.warning is not None
        assert "consume directory" in result.warning
        assert str(tmp_path / "consume" / "doc.pdf") in result.warning
        assert "title, tags and correspondent" in result.warning
        mock_paperless.poll_task.assert_not_called()


def _isolate_dirs(settings: Settings, tmp_path: Path) -> Path:
    """
    Put ``tmp_dir`` and ``data_dir`` on separate subtrees, return the failed dir.

    They must not share a root. The temp-cleanup assertions walk ``tmp_dir``
    demanding that no directory survives a run, while ``failed/`` is a
    directory that is *meant* to survive -- so a ``data_dir`` nested under
    ``tmp_dir`` would make a correct preservation look like a leaked temporary
    directory. Separate subtrees also mirror the real deployment, where scratch
    space and durable state are different volumes.

    Args:
        settings: The settings object to repoint, mutated in place.
        tmp_path: pytest's per-test temporary directory.

    Returns:
        The ``failed/`` directory the preservation guard will move into. It
        does not exist yet; the guard is responsible for creating it.

    """
    settings.output.tmp_dir = str(tmp_path / "scratch")
    settings.output.data_dir = str(tmp_path / "state")
    return settings.output.failed_dir


def _one_page_scanner() -> MagicMock:
    """Return a scanner backend yielding a single page with content."""
    scanner = MagicMock(spec=ScannerBackend)
    scanner.scan_pages.return_value = scan_batch([_make_content_image()])
    return scanner


def _delivering_to_api(task_uuid: str = "task-uuid-1") -> MagicMock:
    """Return a paperless client whose upload reaches the API and polls clean."""
    paperless = MagicMock()
    paperless.upload_document.return_value = UploadResult(
        delivered_to_api=True, task_uuid=task_uuid
    )
    paperless.poll_task.return_value = None
    return paperless


class TestPreservation:
    """The preservation guard: a failed delivery must never destroy the scan."""

    def test_preserves_the_pdf_when_upload_raises(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """An exhausted upload with no consume dir leaves the PDF in failed/."""
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        paperless = MagicMock()
        paperless.upload_document.side_effect = PaperlessError("Upload failed")

        with pytest.raises(PaperlessError):
            run_pipeline(
                scanner=_one_page_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="Upload Raises", job_id="job-a"
                ),
            )

        preserved = list(failed_dir.glob("*.pdf"))
        assert len(preserved) == 1
        assert preserved[0].stat().st_size > 0

    def test_preserves_the_pdf_when_poll_reports_a_paperless_failure(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        A Paperless FAILURE after a successful upload still preserves the PDF.

        This is the case the whole phase is named after: a guard wrapped around
        only the upload would let this raise unwind the temporary directory and
        delete the finished scan.
        """
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        paperless = _delivering_to_api()
        paperless.poll_task.side_effect = PaperlessError("Paperless reported FAILURE")

        with pytest.raises(PaperlessError):
            run_pipeline(
                scanner=_one_page_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="Poll Fails", job_id="job-b"
                ),
            )

        paperless.poll_task.assert_called_once()
        assert len(list(failed_dir.glob("*.pdf"))) == 1

    def test_preserves_the_pdf_when_poll_times_out(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """D-10: a timeout is ambiguous, so the local copy is kept too."""
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        paperless = _delivering_to_api()
        paperless.poll_task.side_effect = PaperlessTimeoutError(
            "Timed out after 300s waiting for task task-uuid-1"
        )

        with pytest.raises(PaperlessTimeoutError) as excinfo:
            run_pipeline(
                scanner=_one_page_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="Poll Times Out", job_id="job-c"
                ),
            )

        assert len(list(failed_dir.glob("*.pdf"))) == 1
        # The subclass survives the re-raise, so a caller that narrows to a
        # timeout deliberately (D-11) still can.
        assert type(excinfo.value) is PaperlessTimeoutError

    def test_preserved_destination_is_named_in_the_raised_message(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """OUTC-04: the user must be told where the scan went."""
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        paperless = MagicMock()
        paperless.upload_document.side_effect = PaperlessError("Upload failed")

        with pytest.raises(PaperlessError) as excinfo:
            run_pipeline(
                scanner=_one_page_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="Named Path", job_id="job-d"
                ),
            )

        preserved = next(iter(failed_dir.glob("*.pdf")))
        message = str(excinfo.value)
        assert str(preserved) in message
        assert "Upload failed" in message

    def test_preserving_chains_the_original_exception(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """The original type and traceback survive on __cause__."""
        _isolate_dirs(default_settings, tmp_path)
        original = PaperlessError("Upload failed")
        paperless = MagicMock()
        paperless.upload_document.side_effect = original

        with pytest.raises(PaperlessError) as excinfo:
            run_pipeline(
                scanner=_one_page_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="Chained", job_id="job-e"
                ),
            )

        assert excinfo.value.__cause__ is original

    def test_preserved_paperless_failure_still_classifies_as_upload(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """The escaping exception needs no new classify_error arm."""
        _isolate_dirs(default_settings, tmp_path)
        paperless = _delivering_to_api()
        paperless.poll_task.side_effect = PaperlessError("Paperless reported FAILURE")

        with pytest.raises(PaperlessError) as excinfo:
            run_pipeline(
                scanner=_one_page_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="Classified", job_id="job-f"
                ),
            )

        assert classify_error(excinfo.value) is ErrorCategory.UPLOAD

    def test_preserving_maps_a_foreign_exception_to_a_paperless_error(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        A non-saneless exception in the delivery window is a delivery failure.

        The guard spans nothing but the upload and the poll, so from the job's
        point of view an arbitrary exception there is a delivery failure -- and
        rebuilding an arbitrary third-party exception from a single string is
        not safe, so the re-raise uses PaperlessError instead.
        """
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        paperless = MagicMock()
        paperless.upload_document.side_effect = RuntimeError("something odd")

        with pytest.raises(PaperlessError) as excinfo:
            run_pipeline(
                scanner=_one_page_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="Foreign", job_id="job-g"
                ),
            )

        assert len(list(failed_dir.glob("*.pdf"))) == 1
        assert isinstance(excinfo.value.__cause__, RuntimeError)
        assert "something odd" in str(excinfo.value)

    def test_preservation_creates_a_missing_failed_dir(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """data_dir may have vanished since startup validation."""
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        assert not failed_dir.exists()

        paperless = MagicMock()
        paperless.upload_document.side_effect = PaperlessError("Upload failed")

        with pytest.raises(PaperlessError):
            run_pipeline(
                scanner=_one_page_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="Makes Dir", job_id="job-h"
                ),
            )

        assert failed_dir.is_dir()

    def test_a_failed_preservation_reports_both_failures(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        Being told only "upload failed" while the scan was destroyed is a lie.

        ``failed_dir`` is made a regular file, so the guard's own mkdir raises
        and the move can never run.
        """
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        failed_dir.parent.mkdir(parents=True, exist_ok=True)
        failed_dir.write_text("a regular file where the directory should be")

        original = PaperlessError("Upload failed")
        paperless = MagicMock()
        paperless.upload_document.side_effect = original

        with pytest.raises(PaperlessError) as excinfo:
            run_pipeline(
                scanner=_one_page_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="Both Fail", job_id="job-i"
                ),
            )

        message = str(excinfo.value)
        assert "Upload failed" in message
        assert str(failed_dir) in message
        assert "NOT" in message
        assert excinfo.value.__cause__ is original

    def test_nothing_is_preserved_when_delivery_succeeds(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """A clean run must not create failed/ at all."""
        failed_dir = _isolate_dirs(default_settings, tmp_path)

        result = run_pipeline(
            scanner=_one_page_scanner(),
            paperless=_delivering_to_api(),
            settings=default_settings,
            request=PipelineRequest(
                profile_name="default", title="Clean Run", job_id="job-j"
            ),
        )

        assert result.outcome is ScanOutcome.SUCCESS
        assert not failed_dir.exists()

    def test_two_preserved_scans_sharing_a_title_are_two_files(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        Two jobs with the same title must not overwrite each other in failed/.

        ``shutil.move`` onto an explicit destination path overwrites silently,
        so this only holds because the job id is part of the file name.
        """
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        paperless = MagicMock()
        paperless.upload_document.side_effect = PaperlessError("Upload failed")

        for job_id in ("job-first", "job-second"):
            with pytest.raises(PaperlessError):
                run_pipeline(
                    scanner=_one_page_scanner(),
                    paperless=paperless,
                    settings=default_settings,
                    request=PipelineRequest(
                        profile_name="default",
                        title="Same Title Twice",
                        job_id=job_id,
                    ),
                )

        preserved = sorted(path.name for path in failed_dir.glob("*.pdf"))
        assert len(preserved) == 2
        assert preserved[0] != preserved[1]

    def test_only_the_pdf_is_preserved_no_page_images(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """D-07: assemble_pdf already deleted every page PNG before returning."""
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        paperless = MagicMock()
        paperless.upload_document.side_effect = PaperlessError("Upload failed")

        with pytest.raises(PaperlessError):
            run_pipeline(
                scanner=_one_page_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="Pdf Only", job_id="job-k"
                ),
            )

        assert sorted(path.suffix for path in failed_dir.iterdir()) == [".pdf"]

    def test_preservation_leaves_no_leftover_temp_directories(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """The TemporaryDirectory still unwinds; only the PDF escaped it."""
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        paperless = MagicMock()
        paperless.upload_document.side_effect = PaperlessError("Upload failed")

        with pytest.raises(PaperlessError):
            run_pipeline(
                scanner=_one_page_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default", title="No Leftovers", job_id="job-l"
                ),
            )

        scratch = tmp_path / "scratch"
        assert [item for item in scratch.iterdir() if item.is_dir()] == []
        assert len(list(failed_dir.glob("*.pdf"))) == 1


def _fill_failed_dir(failed_dir: Path, count: int) -> list[str]:
    """
    Pre-populate ``failed_dir`` with ``count`` preserved-looking PDFs.

    Args:
        failed_dir: The directory to fill; created if absent.
        count: How many files to write.

    Returns:
        The file names written, so a test can assert every one of them
        survived a later preservation.

    """
    failed_dir.mkdir(parents=True, exist_ok=True)
    names = [f"20260101-000000-old{index:02d}-doc.pdf" for index in range(count)]
    for name in names:
        (failed_dir / name).write_bytes(b"%PDF-old" * 64)
    return names


def _preserve_one_scan(settings: Settings, job_id: str) -> PaperlessError:
    """
    Drive one failing delivery through the pipeline and return what escaped.

    Args:
        settings: Settings already pointed at isolated directories.
        job_id: The job id, which is what makes the preserved name unique.

    Returns:
        The PaperlessError that escaped run_pipeline.

    """
    paperless = MagicMock()
    paperless.upload_document.side_effect = PaperlessError("Upload failed")
    with pytest.raises(PaperlessError) as excinfo:
        run_pipeline(
            scanner=_one_page_scanner(),
            paperless=paperless,
            settings=settings,
            request=PipelineRequest(
                profile_name="default", title="Growth Check", job_id=job_id
            ),
        )
    return excinfo.value


class TestFailedDirWarningFiresOncePerGuard:
    """
    One guard preserving two PDFs warns once, not once per file (WR-09).

    The duplex-mismatch recovery passes both halves under a single
    ``_preserving`` guard, because they are one document between them (D-08).
    Running the threshold check inside the per-file loop therefore emitted the
    same "N preserved scans have accumulated" WARNING twice, with different
    counts -- log noise on the one path already flagged as an anomaly, and a
    contradiction of the helper's own "one WARNING" docstring.
    """

    def _two_scans(self, tmp_path: Path) -> list[Path]:
        """Write two assembled-looking PDFs for one guard to preserve."""
        pdfs = [tmp_path / "fronts.pdf", tmp_path / "backs.pdf"]
        for pdf in pdfs:
            pdf.write_bytes(b"%PDF-new" * 64)
        return pdfs

    def _preserve_both(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> Path:
        """Fail one delivery holding two PDFs, and return the failed dir."""
        failed_dir = tmp_path / "failed"
        # One short of the threshold, so the per-file call crossed it on the
        # first move and again on the second -- two warnings for one failure.
        _fill_failed_dir(failed_dir, FAILED_DIR_WARN_THRESHOLD - 1)
        pdfs = self._two_scans(tmp_path)
        failure = PaperlessError("Upload failed")

        with (
            caplog.at_level(logging.WARNING, logger="saneless.pipeline"),
            pytest.raises(PaperlessError),
            _preserving(pdfs, failed_dir),
        ):
            raise failure

        return failed_dir

    def test_two_preserved_scans_emit_one_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """One failure is one anomaly, however many files it preserved."""
        failed_dir = self._preserve_both(tmp_path, caplog)

        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
            and str(failed_dir) in record.getMessage()
        ]
        assert len(warnings) == 1

    def test_the_single_warning_counts_both_preserved_scans(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The count reflects the finished state, not a mid-loop snapshot."""
        failed_dir = self._preserve_both(tmp_path, caplog)

        message = next(
            record.getMessage()
            for record in caplog.records
            if str(failed_dir) in record.getMessage()
        )
        assert str(FAILED_DIR_WARN_THRESHOLD + 1) in message


class TestFailedDirWarning:
    """Warn -- never prune -- when preserved scans accumulate (T-23-29)."""

    def test_failed_dir_warns_when_the_threshold_is_reached(
        self,
        default_settings: Settings,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Preserving into a crowded directory logs one WARNING."""
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        _fill_failed_dir(failed_dir, FAILED_DIR_WARN_THRESHOLD - 1)

        with caplog.at_level(logging.WARNING, logger="saneless.pipeline"):
            _preserve_one_scan(default_settings, "job-growth-1")

        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
            and str(failed_dir) in record.getMessage()
        ]
        assert len(warnings) == 1

    def test_failed_dir_warning_names_the_count_size_and_path(
        self,
        default_settings: Settings,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The operator needs all three facts to act on the warning."""
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        _fill_failed_dir(failed_dir, FAILED_DIR_WARN_THRESHOLD - 1)

        with caplog.at_level(logging.WARNING, logger="saneless.pipeline"):
            _preserve_one_scan(default_settings, "job-growth-2")

        message = next(
            record.getMessage()
            for record in caplog.records
            if str(failed_dir) in record.getMessage()
        )
        assert str(FAILED_DIR_WARN_THRESHOLD) in message
        assert "MiB" in message
        assert str(failed_dir) in message

    def test_failed_dir_warning_is_silent_below_the_threshold(
        self,
        default_settings: Settings,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """One preserved scan is not a problem and must not be announced."""
        failed_dir = _isolate_dirs(default_settings, tmp_path)

        with caplog.at_level(logging.WARNING, logger="saneless.pipeline"):
            _preserve_one_scan(default_settings, "job-growth-3")

        assert len(list(failed_dir.glob("*.pdf"))) == 1
        assert [
            record.getMessage()
            for record in caplog.records
            if "MiB" in record.getMessage()
        ] == []

    def test_failed_dir_warning_deletes_nothing(
        self,
        default_settings: Settings,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        The warning is the whole control: no prune, no sweep, no rotation.

        Deleting a preserved scan would be exactly the data loss the guard
        exists to prevent, so every pre-existing file must still be there.
        """
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        existing = _fill_failed_dir(failed_dir, FAILED_DIR_WARN_THRESHOLD - 1)

        with caplog.at_level(logging.WARNING, logger="saneless.pipeline"):
            _preserve_one_scan(default_settings, "job-growth-4")

        survivors = {path.name for path in failed_dir.glob("*.pdf")}
        assert set(existing) <= survivors
        assert len(survivors) == FAILED_DIR_WARN_THRESHOLD

    def test_failed_dir_warning_failure_cannot_mask_the_delivery_error(
        self,
        default_settings: Settings,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        An unreadable failed/ must not replace the real failure.

        The growth check runs inside the preservation guard's own exception
        handler, so a raise there would swap the delivery failure -- the
        message OUTC-04 requires the job to carry -- for a bookkeeping error.
        """
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        real_glob = Path.glob

        def exploding_glob(self: Path, pattern: str) -> Iterator[Path]:
            if self == failed_dir:
                msg = "failed/ became unreadable"
                raise OSError(msg)
            return real_glob(self, pattern)

        monkeypatch.setattr(Path, "glob", exploding_glob)

        escaped = _preserve_one_scan(default_settings, "job-growth-5")

        assert "Upload failed" in str(escaped)
        assert "preserved at" in str(escaped)
        # iterdir, not glob: the patch is still in force.
        assert len([path for path in failed_dir.iterdir() if path.is_file()]) == 1


def _mismatched_duplex_scanner(fronts: int = 3, backs: int = 2) -> MagicMock:
    """
    Return a scanner whose two manual-duplex passes disagree on page count.

    Args:
        fronts: Pages produced by pass A.
        backs: Pages produced by pass B.

    Returns:
        A ScannerBackend mock whose two scan_pages calls yield those pages.

    """
    scanner = MagicMock(spec=ScannerBackend)
    scanner.scan_pages.side_effect = [
        scan_batch([_make_content_image() for _ in range(fronts)]),
        scan_batch([_make_content_image() for _ in range(backs)]),
    ]
    return scanner


def _both_halves_delivered() -> MagicMock:
    """Return a paperless client that accepts both partial uploads."""
    paperless = MagicMock()
    paperless.upload_document.side_effect = [
        UploadResult(delivered_to_api=True, task_uuid="fronts-task"),
        UploadResult(delivered_to_api=True, task_uuid="backs-task"),
    ]
    paperless.poll_task.return_value = None
    return paperless


class TestDuplexMismatchDelivery:
    """D-08: the duplex-mismatch path gets the same honesty as the simplex one."""

    def test_duplex_mismatch_polls_both_halves(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Leaving either task unpolled is C-03 surviving in a corner."""
        _isolate_dirs(default_settings, tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"
        paperless = _both_halves_delivered()

        result = run_pipeline(
            scanner=_mismatched_duplex_scanner(),
            paperless=paperless,
            settings=default_settings,
            request=PipelineRequest(
                profile_name="default",
                title="Polled Twice",
                job_id="job-dx-1",
                flip_coordinator=AlwaysContinueFlipCoordinator(),
            ),
        )

        polled = [call.args[0] for call in paperless.poll_task.call_args_list]
        assert polled == ["fronts-task", "backs-task"]
        assert result.outcome is ScanOutcome.SUCCESS

    def test_duplex_mismatch_uses_the_configured_task_timeout(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Both halves honour output.paperless_task_timeout."""
        _isolate_dirs(default_settings, tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"
        default_settings.output.paperless_task_timeout = 17
        paperless = _both_halves_delivered()

        run_pipeline(
            scanner=_mismatched_duplex_scanner(),
            paperless=paperless,
            settings=default_settings,
            request=PipelineRequest(
                profile_name="default",
                title="Timeout Wired",
                job_id="job-dx-2",
                flip_coordinator=AlwaysContinueFlipCoordinator(),
            ),
        )

        timeouts = [
            call.kwargs["timeout"] for call in paperless.poll_task.call_args_list
        ]
        assert timeouts == [17, 17]

    def test_duplex_mismatch_preserves_both_halves_when_the_fronts_fail(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """
        A failure on one half must not cost the user the other half.

        The two partial PDFs are one document between them, so both go into
        failed/ even though only the fronts task reported FAILURE.
        """
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"
        paperless = _both_halves_delivered()
        paperless.poll_task.side_effect = PaperlessError("Paperless reported FAILURE")

        with pytest.raises(PaperlessError):
            run_pipeline(
                scanner=_mismatched_duplex_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default",
                    title="Fronts Fail",
                    job_id="job-dx-3",
                    flip_coordinator=AlwaysContinueFlipCoordinator(),
                ),
            )

        assert len(list(failed_dir.glob("*.pdf"))) == 2

    def test_duplex_mismatch_preserves_both_halves_when_the_backs_fail(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """A clean fronts poll followed by a failing backs poll still raises."""
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"
        paperless = _both_halves_delivered()
        paperless.poll_task.side_effect = [
            None,
            PaperlessError("Paperless reported FAILURE"),
        ]

        with pytest.raises(PaperlessError):
            run_pipeline(
                scanner=_mismatched_duplex_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default",
                    title="Backs Fail",
                    job_id="job-dx-4",
                    flip_coordinator=AlwaysContinueFlipCoordinator(),
                ),
            )

        assert paperless.poll_task.call_count == 2
        assert len(list(failed_dir.glob("*.pdf"))) == 2

    def test_duplex_mismatch_preserved_halves_have_distinct_names(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Two same-named halves would destroy the one this path exists to save."""
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"
        paperless = _both_halves_delivered()
        paperless.poll_task.side_effect = PaperlessError("Paperless reported FAILURE")

        with pytest.raises(PaperlessError):
            run_pipeline(
                scanner=_mismatched_duplex_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default",
                    title="Distinct Halves",
                    job_id="job-dx-5",
                    flip_coordinator=AlwaysContinueFlipCoordinator(),
                ),
            )

        names = sorted(path.name for path in failed_dir.glob("*.pdf"))
        assert len(names) == 2
        assert any("fronts" in name for name in names)
        assert any("backs" in name for name in names)

    def test_duplex_mismatch_preservation_failure_is_reported_too(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """An unusable failed/ must not silently swallow the duplex failure."""
        failed_dir = _isolate_dirs(default_settings, tmp_path)
        failed_dir.parent.mkdir(parents=True, exist_ok=True)
        failed_dir.write_text("a regular file where the directory should be")
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"
        paperless = _both_halves_delivered()
        paperless.upload_document.side_effect = PaperlessError("Upload failed")

        with pytest.raises(PaperlessError) as excinfo:
            run_pipeline(
                scanner=_mismatched_duplex_scanner(),
                paperless=paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default",
                    title="Duplex Both Fail",
                    job_id="job-dx-6",
                    flip_coordinator=AlwaysContinueFlipCoordinator(),
                ),
            )

        message = str(excinfo.value)
        assert "Upload failed" in message
        assert str(failed_dir) in message

    def test_duplex_mismatch_result_carries_outcome_warning_and_counts(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """OUTC-03: the worker needs an outcome, a warning and three counts."""
        _isolate_dirs(default_settings, tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"

        result = run_pipeline(
            scanner=_mismatched_duplex_scanner(fronts=4, backs=3),
            paperless=_both_halves_delivered(),
            settings=default_settings,
            request=PipelineRequest(
                profile_name="default",
                title="Counts And Warning",
                job_id="job-dx-7",
                flip_coordinator=AlwaysContinueFlipCoordinator(),
            ),
        )

        assert result.outcome is ScanOutcome.SUCCESS
        assert result.warning is not None
        assert "Page count mismatch: 4 fronts, 3 backs" in result.warning
        assert result.pages_scanned == 7
        assert result.pages_removed == 0
        assert result.pages_uploaded == 7

    def test_duplex_mismatch_is_fallback_not_success_when_a_half_falls_back(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """A consume-dir half is not a success, and carries no task to poll."""
        _isolate_dirs(default_settings, tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"
        paperless = MagicMock()
        paperless.upload_document.side_effect = [
            UploadResult(delivered_to_api=True, task_uuid="fronts-task"),
            UploadResult(
                delivered_to_api=False,
                consume_dir_path=tmp_path / "consume" / "backs.pdf",
            ),
        ]
        paperless.poll_task.return_value = None

        result = run_pipeline(
            scanner=_mismatched_duplex_scanner(),
            paperless=paperless,
            settings=default_settings,
            request=PipelineRequest(
                profile_name="default",
                title="Half Fallback",
                job_id="job-dx-8",
                flip_coordinator=AlwaysContinueFlipCoordinator(),
            ),
        )

        assert result.outcome is ScanOutcome.FALLBACK
        assert paperless.poll_task.call_count == 1


class TestTheDpiTheDeviceActuallyChose:
    """
    The PDF declares the resolution the scanner used, not the one asked for.

    Phase 23 made the profile's requested resolution authoritative for
    ``img2pdf.get_fixed_dpi_layout_fun``. SANE substitutes silently -- measured,
    5000 comes back as 1200 -- so a device that substitutes produced both a
    mis-cropped page and a MediaBox disagreeing with its own content, which
    re-opened part of OUTC-06 (T-24-22).
    """

    def test_the_pdf_is_assembled_at_the_resolution_the_device_chose(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """A profile asking 600 on a device that gives 300 assembles at 300."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].resolution = 600

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.return_value = scan_batch(
            [_make_content_image()], resolution=300
        )

        with patch("saneless.pipeline.assemble_pdf") as mock_assemble:
            mock_assemble.return_value = tmp_path / "output.pdf"
            (tmp_path / "output.pdf").write_bytes(b"%PDF-fake")

            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=PipelineRequest(profile_name="default", title="Clamped"),
            )

        assert mock_assemble.call_args.kwargs["dpi"] == 300

    def test_the_duplex_mismatch_recovery_also_uses_the_actual_dpi(
        self,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """The recovery path builds its two partial PDFs at the device's dpi."""
        _isolate_dirs(default_settings, tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"
        default_settings.profiles["default"].resolution = 600

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = [
            scan_batch([_make_content_image() for _ in range(3)], resolution=150),
            scan_batch([_make_content_image() for _ in range(2)], resolution=150),
        ]

        fronts_pdf = tmp_path / "fronts.pdf"
        backs_pdf = tmp_path / "backs.pdf"
        fronts_pdf.write_bytes(b"%PDF-fake")
        backs_pdf.write_bytes(b"%PDF-fake")

        with patch("saneless.pipeline.assemble_pdf") as mock_assemble:
            mock_assemble.side_effect = [fronts_pdf, backs_pdf]

            run_pipeline(
                scanner=scanner,
                paperless=_both_halves_delivered(),
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default",
                    title="Mismatch Dpi",
                    job_id="job-dpi-1",
                    flip_coordinator=AlwaysContinueFlipCoordinator(),
                ),
            )

        dpis = [call.kwargs["dpi"] for call in mock_assemble.call_args_list]
        assert dpis == [150, 150]

    def test_two_passes_disagreeing_on_resolution_say_so(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        Manual duplex must not silently pick one of two different resolutions.

        The two passes use identical settings on one device, so a disagreement
        means the device changed its mind mid-job. Pass A's value is used and
        the difference is logged rather than swallowed.
        """
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].source = "ADF"
        default_settings.profiles["default"].duplex = "manual"

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = [
            scan_batch([_make_content_image() for _ in range(2)], resolution=300),
            scan_batch([_make_content_image() for _ in range(2)], resolution=150),
        ]

        with (
            patch("saneless.pipeline.assemble_pdf") as mock_assemble,
            caplog.at_level(logging.WARNING, logger="saneless.pipeline"),
        ):
            mock_assemble.return_value = tmp_path / "output.pdf"
            (tmp_path / "output.pdf").write_bytes(b"%PDF-fake")

            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=PipelineRequest(
                    profile_name="default",
                    title="Two Dpis",
                    flip_coordinator=AlwaysContinueFlipCoordinator(),
                ),
            )

        messages = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        ]
        assert [m for m in messages if "300" in m and "150" in m]
        assert mock_assemble.call_args.kwargs["dpi"] == 300


class TestRejectedPagesAreNotBlankPages:
    """
    D-07: a sheet the scanner could not read is counted, and counted apart.

    ``pages_scanned`` is ``len(images)``, which already excludes a skipped
    sheet, so a ten-sheet stack with one unreadable page reported nine and
    nobody learned a page was lost (T-24-23). The count must not be folded into
    the blank-page total, which Phase 30 renders as pages removed for being
    blank (T-24-24).
    """

    def test_rejected_pages_are_reported_without_touching_the_blank_count(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Two integrity rejections and no blank pages: 0 removed, 2 reported."""
        default_settings.output.tmp_dir = str(tmp_path)

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.return_value = scan_batch(
            [_make_content_image() for _ in range(3)], rejected=2
        )

        with patch("saneless.pipeline.assemble_pdf") as mock_assemble:
            mock_assemble.return_value = tmp_path / "output.pdf"
            (tmp_path / "output.pdf").write_bytes(b"%PDF-fake")

            result = run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=PipelineRequest(profile_name="default", title="Two Rejected"),
            )

        assert result.pages_removed == 0
        assert result.warning is not None
        assert "2" in result.warning

    def test_a_clean_scan_reports_zero_for_both_counts(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """No rejections and no blank removals leaves nothing to warn about."""
        default_settings.output.tmp_dir = str(tmp_path)

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.return_value = scan_batch(
            [_make_content_image() for _ in range(3)]
        )

        with patch("saneless.pipeline.assemble_pdf") as mock_assemble:
            mock_assemble.return_value = tmp_path / "output.pdf"
            (tmp_path / "output.pdf").write_bytes(b"%PDF-fake")

            result = run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=PipelineRequest(profile_name="default", title="All Clean"),
            )

        assert result.pages_removed == 0
        assert result.warning is None
