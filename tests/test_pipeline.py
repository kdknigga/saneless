"""Tests for pipeline orchestration."""

from unittest.mock import MagicMock

import pytest

from saneless.exceptions import PaperlessError, ScanError
from saneless.pipeline import PipelineRequest, run_pipeline
from saneless.scanner.base import ScannerBackend


class TestRunPipeline:
    """Pipeline orchestration tests."""

    def test_run_pipeline_happy_path(
        self, mock_scanner, mock_paperless, default_settings, tmp_path
    ):
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

    def test_run_pipeline_scan_error(self, mock_paperless, default_settings, tmp_path):
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

    def test_run_pipeline_upload_error(self, mock_scanner, default_settings, tmp_path):
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
        self, mock_scanner, mock_paperless, default_settings, tmp_path
    ):
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

    def test_run_pipeline_temp_cleanup_on_error(self, default_settings, tmp_path):
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
        self, mock_scanner, mock_paperless, default_settings, tmp_path
    ):
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
        self, mock_scanner, mock_paperless, default_settings, tmp_path
    ):
        """Pipeline calls status_callback with correct status messages in order."""
        default_settings.output.tmp_dir = str(tmp_path)
        messages = []

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
