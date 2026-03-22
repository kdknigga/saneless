"""Tests for pipeline orchestration."""

from __future__ import annotations

import base64
import threading
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image, ImageDraw

from saneless.exceptions import PaperlessError, ScanError
from saneless.pipeline import (
    PipelineRequest,
    _interleave_duplex,
    _is_manual_duplex,
    run_pipeline,
)
from saneless.scanner.base import ScannerBackend

if TYPE_CHECKING:
    from pathlib import Path

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

        assert result is not None
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
        default_settings.output.tmp_dir = str(tmp_path)

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
        mock_paperless.upload_document.return_value = "task-uuid-123"
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
        """Pipeline calls status_callback with correct status messages in order."""
        default_settings.output.tmp_dir = str(tmp_path)
        messages: list[str] = []

        request = PipelineRequest(
            profile_name="default",
            title="Status Doc",
            status_callback=messages.append,
        )
        run_pipeline(
            scanner=mock_scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=request,
        )

        assert messages[0] == "Scanning..."
        assert messages[1] == "Assembling PDF..."
        assert messages[2] == "Uploading to paperless-ngx..."
        assert messages[3] == "Done: Status Doc"


def _make_content_image(color: str = "black") -> Image.Image:
    """Create an image with visible content (not empty)."""
    img = Image.new("RGB", (200, 300), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, 180, 280], fill=color)
    return img


def _make_empty_image() -> Image.Image:
    """Create a nearly-white image that should be detected as empty."""
    return Image.new("RGB", (200, 300), (254, 254, 254))


class TestIsManualDuplex:
    """Tests for _is_manual_duplex helper."""

    def test_adf_manual_duplex(self) -> None:
        """Source 'ADF Manual Duplex' is manual duplex."""
        assert _is_manual_duplex("ADF Manual Duplex") is True

    def test_manual_duplex_case_insensitive(self) -> None:
        """Case insensitive detection."""
        assert _is_manual_duplex("adf manual duplex") is True
        assert _is_manual_duplex("MANUAL DUPLEX") is True

    def test_flatbed_not_manual_duplex(self) -> None:
        """Flatbed is not manual duplex."""
        assert _is_manual_duplex("Flatbed") is False

    def test_adf_not_manual_duplex(self) -> None:
        """Plain ADF (no manual) is not manual duplex."""
        assert _is_manual_duplex("ADF") is False

    def test_hardware_duplex_not_manual(self) -> None:
        """Hardware duplex without 'manual' is not manual duplex."""
        assert _is_manual_duplex("ADF Duplex") is False


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
        mock_scanner.scan_pages.return_value = iter([_make_content_image()])

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
        mock_scanner.scan_pages.return_value = iter([_make_content_image()])

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
        scanner.scan_pages.return_value = iter(all_pages)

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
        scanner.scan_pages.return_value = iter([_make_content_image()])

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
        """All pages empty -> raises ScanError."""
        default_settings.output.tmp_dir = str(tmp_path)

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.return_value = iter(
            [_make_empty_image(), _make_empty_image()]
        )

        request = PipelineRequest(profile_name="default", title="All Empty Test")
        with pytest.raises(ScanError, match="All pages were detected as empty"):
            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )


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
        default_settings.profiles["default"].source = "ADF Manual Duplex"

        fronts = [_make_content_image(c) for c in ["red", "green", "blue"]]
        backs = [_make_content_image(c) for c in ["cyan", "magenta", "yellow"]]

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = [iter(fronts), iter(backs)]

        request = PipelineRequest(
            profile_name="default",
            title="Duplex Test",
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

    def test_manual_duplex_count_mismatch(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Pass A yields 3 pages, pass B yields 2 -> raises ScanError."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].source = "ADF Manual Duplex"

        fronts = [_make_content_image() for _ in range(3)]
        backs = [_make_content_image() for _ in range(2)]

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = [iter(fronts), iter(backs)]

        request = PipelineRequest(
            profile_name="default",
            title="Mismatch Test",
        )

        with pytest.raises(ScanError, match="Page count mismatch: 3 fronts, 2 backs"):
            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )

    def test_manual_duplex_empty_page_after_interleave(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Empty page detection runs on interleaved result, not individual passes."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].source = "ADF Manual Duplex"

        # 2 fronts: 1 content + 1 content, 2 backs: 1 empty + 1 content
        fronts = [_make_content_image("red"), _make_content_image("blue")]
        backs = [_make_empty_image(), _make_content_image("green")]

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = [iter(fronts), iter(backs)]

        request = PipelineRequest(
            profile_name="default",
            title="Duplex Filter Test",
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
        default_settings.profiles["default"].source = "ADF Manual Duplex"

        fronts = [_make_content_image()]
        backs = [_make_content_image()]

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = [iter(fronts), iter(backs)]

        thumb_results: list[str] = []
        request = PipelineRequest(
            profile_name="default",
            title="Duplex Thumb Test",
            thumbnail_callback=thumb_results.append,
        )

        run_pipeline(
            scanner=scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=request,
        )

        assert len(thumb_results) == 1
        assert len(thumb_results[0]) > 0

    def test_manual_duplex_flip_event_wait(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Pipeline waits on flip_event for manual duplex."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].source = "ADF Manual Duplex"

        fronts = [_make_content_image()]
        backs = [_make_content_image()]

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.side_effect = [iter(fronts), iter(backs)]

        flip_event = threading.Event()
        flip_event.set()  # Pre-set so it doesn't block

        request = PipelineRequest(
            profile_name="default",
            title="Flip Event Test",
            flip_event=flip_event,
        )

        # Should not block since event is pre-set
        run_pipeline(
            scanner=scanner,
            paperless=mock_paperless,
            settings=default_settings,
            request=request,
        )

    def test_manual_duplex_abort(
        self,
        mock_paperless: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Abort event set -> raises ScanError."""
        default_settings.output.tmp_dir = str(tmp_path)
        default_settings.profiles["default"].source = "ADF Manual Duplex"

        fronts = [_make_content_image()]

        scanner = MagicMock(spec=ScannerBackend)
        scanner.scan_pages.return_value = iter(fronts)

        flip_event = threading.Event()
        abort_event = threading.Event()
        flip_event.set()
        abort_event.set()

        request = PipelineRequest(
            profile_name="default",
            title="Abort Test",
            flip_event=flip_event,
            abort_event=abort_event,
        )

        with pytest.raises(ScanError, match="cancelled by user"):
            run_pipeline(
                scanner=scanner,
                paperless=mock_paperless,
                settings=default_settings,
                request=request,
            )


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
        scanner.scan_pages.return_value = iter([img])

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
        scanner.scan_pages.return_value = iter(all_pages)

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
        scanner.scan_pages.return_value = iter([_make_content_image()])

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
