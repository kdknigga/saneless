"""
Paperless-ngx REST API client with retry, polling, and connection test.

Uploads PDFs with metadata (title, tags, correspondent, created date),
polls the task endpoint with exponential backoff until terminal state,
and tests connections distinguishing unreachable, token_rejected, and
connected states.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

import httpx

from .exceptions import PaperlessError

__all__ = ["PaperlessClient"]

logger = logging.getLogger(__name__)


class PaperlessClient:
    """
    Client for the paperless-ngx REST API.

    Handles document uploads with metadata, task polling with
    exponential backoff, and connection testing. Supports retry
    on network errors and fallback to a consume directory.

    Args:
        url: Base URL of the paperless-ngx instance.
        token: API authentication token.
        consume_dir: Optional fallback directory for PDF upload failures.
        max_retries: Maximum number of upload retry attempts.
        _transport: Optional httpx transport for testing.

    """

    def __init__(
        self,
        url: str,
        token: str,
        consume_dir: str = "",
        max_retries: int = 3,
        _transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Initialize the paperless-ngx API client."""
        client_kwargs: dict = {
            "base_url": url.rstrip("/"),
            "headers": {"Authorization": f"Token {token}"},
            "timeout": 30.0,
        }
        if _transport is not None:
            client_kwargs["transport"] = _transport
        self._client = httpx.Client(**client_kwargs)
        self._consume_dir = consume_dir
        self._max_retries = max_retries

    def upload_document(
        self,
        pdf_path: Path,
        title: str,
        tags: list[int] | None = None,
        correspondent: int | None = None,
        created: str | None = None,
    ) -> str:
        """
        Upload a PDF document to paperless-ngx.

        Builds multipart form data with title and optional metadata.
        Tags are submitted as repeated form fields. Retries on network
        errors with exponential backoff; falls back to consume directory
        if configured and all retries are exhausted.

        Args:
            pdf_path: Path to the PDF file to upload.
            title: Document title.
            tags: Optional list of tag IDs to attach.
            correspondent: Optional correspondent ID.
            created: Optional creation date string (e.g. "2026-03-20").

        Returns:
            Task UUID string from paperless-ngx, or "fallback" if
            the file was copied to the consume directory.

        Raises:
            PaperlessError: If upload fails and no fallback is available,
                or if the server returns a 4xx error.

        """
        data: dict[str, str | list[str]] = {"title": title}
        if created is not None:
            data["created"] = created
        if correspondent is not None:
            data["correspondent"] = str(correspondent)
        if tags:
            data["tags"] = [str(tag_id) for tag_id in tags]

        last_error: Exception | None = None

        for attempt in range(self._max_retries):
            try:
                with pdf_path.open("rb") as f:
                    response = self._client.post(
                        "/api/documents/post_document/",
                        data=data,
                        files={"document": (pdf_path.name, f, "application/pdf")},
                    )
                response.raise_for_status()
                task_id = response.json()
                logger.info("Upload succeeded, task ID: %s", task_id)
                return str(task_id)

            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                last_error = exc
                logger.warning(
                    "Upload attempt %d/%d failed: %s",
                    attempt + 1,
                    self._max_retries,
                    exc,
                )
                if attempt < self._max_retries - 1:
                    time.sleep(2**attempt)

            except httpx.HTTPStatusError as exc:
                if 400 <= exc.response.status_code < 500:
                    msg = (
                        f"Paperless rejected upload: "
                        f"{exc.response.status_code} {exc.response.text}"
                    )
                    raise PaperlessError(msg) from exc
                last_error = exc
                logger.warning(
                    "Upload attempt %d/%d failed: %s",
                    attempt + 1,
                    self._max_retries,
                    exc,
                )
                if attempt < self._max_retries - 1:
                    time.sleep(2**attempt)

        # All retries exhausted
        if self._consume_dir:
            dest_dir = Path(self._consume_dir)
            if not dest_dir.exists():
                dest_dir.mkdir(parents=True, exist_ok=True)
                logger.warning("Created consume directory %s", dest_dir)
            dest = dest_dir / pdf_path.name
            shutil.copy2(pdf_path, dest)
            logger.warning("All retries exhausted. Copied PDF to %s", dest)
            return "fallback"

        msg = f"Upload failed after {self._max_retries} retries"
        raise PaperlessError(msg) from last_error

    def poll_task(self, task_id: str, timeout: int | float = 300) -> dict[str, object]:
        """
        Poll task endpoint until terminal state with exponential backoff.

        Handles the race condition where a task may not appear
        immediately after upload (Pitfall #8).

        Args:
            task_id: Task UUID returned from upload.
            timeout: Maximum seconds to wait for terminal state.

        Returns:
            Task dict with status field. Status is one of
            SUCCESS, FAILURE, or TIMEOUT.

        """
        delay = 0.5
        elapsed = 0.0

        while elapsed < timeout:
            response = self._client.get(
                "/api/tasks/",
                params={"task_id": task_id},
            )
            if response.status_code == 200:
                tasks = response.json()
                if isinstance(tasks, list) and tasks:
                    task = tasks[0]
                    status = task.get("status")
                    if status in ("SUCCESS", "FAILURE"):
                        logger.info("Task %s completed: %s", task_id, status)
                        return dict(task)

            time.sleep(delay)
            elapsed += delay
            delay = min(delay * 2, 30.0)

        logger.warning("Task %s timed out after %s seconds", task_id, timeout)
        return {"status": "TIMEOUT", "task_id": task_id}

    def test_connection(self) -> str:
        """
        Test paperless-ngx connection.

        Distinguishes three states: connected (API reachable and
        authenticated), token_rejected (API reachable but auth
        failed), and unreachable (network error).

        Returns:
            One of "connected", "token_rejected", or "unreachable".

        """
        try:
            response = self._client.get("/api/tags/", params={"page_size": 1})
            if response.status_code in (401, 403):
                return "token_rejected"
            return "connected"
        except httpx.ConnectError:
            return "unreachable"

    def get_tags(self) -> list[dict[str, object]]:
        """
        Fetch all tags from paperless-ngx.

        Returns:
            List of tag dicts with at least 'id' and 'name' keys.

        """
        response = self._client.get(
            "/api/tags/",
            params={"page_size": 1000},
        )
        response.raise_for_status()
        data = response.json()
        return data.get("results", []) if isinstance(data, dict) else data

    def get_correspondents(self) -> list[dict[str, object]]:
        """
        Fetch all correspondents from paperless-ngx.

        Returns:
            List of correspondent dicts with at least 'id' and 'name' keys.

        """
        response = self._client.get(
            "/api/correspondents/",
            params={"page_size": 1000},
        )
        response.raise_for_status()
        data = response.json()
        return data.get("results", []) if isinstance(data, dict) else data

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()
