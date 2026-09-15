"""
Paperless-ngx REST API client with retry, polling, and connection test.

Uploads PDFs with metadata (title, tags, correspondent, created date),
polls the task endpoint with exponential backoff until a terminal state
and raises when that state is not success, and probes connections,
reporting one of the five ConnectionStatus outcomes.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .exceptions import PaperlessError, PaperlessTimeoutError
from .vocabulary import ConnectionStatus

__all__ = ["PaperlessClient", "UploadResult"]

logger = logging.getLogger(__name__)

# The API version this client is written against.  paperless-ngx negotiates via
# ``Accept: application/json; version=N`` and serves its own current default --
# today 10 -- to a client that sends no header, so pinning turns a hidden
# assumption into an explicit contract.  Servers old enough to predate task
# versioning ignore the header.  An *invalid* version string makes paperless-ngx
# answer 406 Not Acceptable, which poll_task reports immediately as a hard
# failure, so the string has to be exactly right.
_API_VERSION_ACCEPT = "application/json; version=9"

# paperless-ngx's COMPLETE_STATUSES, normalised to the v9 uppercase spelling.
# REVOKED belongs here: it is an administrative cancellation that will never
# progress, so polling it to the deadline would report a misattributed timeout.
_TERMINAL_STATUSES = frozenset({"SUCCESS", "FAILURE", "REVOKED"})

# Upper bound on how much of an error response body is interpolated into an
# error message.  That message is recorded verbatim in the job store and
# rendered in the web status area and on the terminal (T-23-16, M-17), so an
# upstream returning a multi-megabyte body or an HTML error page must not be
# able to flood any of them.  The full body is logged at DEBUG instead.
_MAX_BODY_LINE_CHARS = 200

_EMPTY_BODY = "(empty response body)"

_NO_FAILURE_MESSAGE = "Paperless reported a failure but supplied no message"


def _extract_task(payload: object) -> dict[str, object] | None:
    """
    Return the single task from either paperless-ngx response shape.

    API v9 answers ``GET /api/tasks/`` with a bare list; v10 paginates it
    into ``{"count", "next", "previous", "results"}``.  This is the same
    both-shapes tolerance ``get_tags`` and ``get_correspondents`` already
    apply to their own endpoints.

    Args:
        payload: The decoded JSON body of a 200 response.

    Returns:
        The first task dict, or None when the response carried no task.
        None is **not** an error: a task is not always visible immediately
        after the upload that created it, so the caller must keep polling
        inside its deadline rather than raise.

    """
    if isinstance(payload, dict):
        payload = payload.get("results")
    if isinstance(payload, list) and payload:
        first = payload[0]
        if isinstance(first, dict):
            return {str(key): value for key, value in first.items()}
    return None


def _task_status(task: dict[str, object]) -> str:
    """
    Return the task status in the v9 uppercase spelling.

    API v10 spells the same statuses in lowercase (``success``, ``failure``,
    ``revoked``), so every comparison in this module is made against the
    normalised form.

    Args:
        task: A task dict from either API version.

    Returns:
        The uppercase status, or the empty string when absent.

    """
    return str(task.get("status", "")).upper()


def _failure_message(task: dict[str, object]) -> str:
    """
    Return the failure text paperless-ngx supplied, from whichever field has it.

    API v9 carries a flat ``result`` string; v10 moved it into
    ``result_data`` as ``error_message`` (with ``reason`` used for some
    rejections).  There is no single field that works for both versions.

    Args:
        task: A task dict whose status is FAILURE or REVOKED.

    Returns:
        A non-empty string.  This value is recorded as the job's error, so
        it must never be None or empty -- a failure with neither field still
        needs something a reader can act on.

    """
    result = task.get("result")
    if isinstance(result, str) and result:
        return result
    data = task.get("result_data")
    if isinstance(data, dict):
        for key in ("error_message", "reason"):
            message = data.get(key)
            if isinstance(message, str) and message:
                return message
    return _NO_FAILURE_MESSAGE


def _first_message(value: object) -> str | None:
    """
    Return the first message from a DRF error value.

    Args:
        value: A field's error value -- a string, or a list of strings.

    Returns:
        The string itself, the first string in a list, or None when the
        value carries no usable message.

    """
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, str) and value:
        return value
    return None


def _json_error_text(payload: object) -> str | None:
    """
    Pick the one message a reader needs out of a DRF error payload.

    The shapes Django REST Framework produces are, in order of preference:
    ``{"detail": "..."}`` (a string, or a list joined with spaces), a
    field-error dict ``{"field": ["msg", ...]}`` (including
    ``non_field_errors``) rendered as ``field: msg`` for its first field,
    and a bare top-level list whose first string is used.

    Args:
        payload: The decoded JSON body.

    Returns:
        The chosen text, or None when the payload matches no known shape
        and the caller should fall back to the raw body.

    """
    text: str | None = None
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str) and detail:
            text = detail
        elif isinstance(detail, list) and detail:
            text = " ".join(str(item) for item in detail)
        else:
            for key, value in payload.items():
                message = _first_message(value)
                if message is not None:
                    text = f"{key}: {message}"
                    break
    elif isinstance(payload, list):
        text = _first_message(payload)
    return text


def _render_error_body(response: httpx.Response) -> str:
    """
    Reduce a Paperless error response body to one bounded line.

    This is the single body renderer for the client (D-09): the upload 4xx
    and the task poll non-200 both use it.  A DRF JSON error is reduced to
    its ``detail``, else its first field error, else the first entry of a
    top-level list; anything else (an HTML proxy page, plain text) is used
    as it stands.  In every case the whitespace is collapsed, so no newline,
    tab or other whitespace control character from the upstream can forge an
    extra CLI line (T-28-24), and the text is cut to
    ``_MAX_BODY_LINE_CHARS`` characters plus an ellipsis, so the job store
    and the web status area cannot be flooded (T-23-16).  The full body is
    logged at DEBUG so it stays diagnosable.

    Args:
        response: The error response.

    Returns:
        A non-empty single line; ``(empty response body)`` when the body
        carried nothing.

    """
    logger.debug("Paperless error body (%s): %s", response.status_code, response.text)
    try:
        text = _json_error_text(response.json())
    except ValueError:
        text = None
    if text is None:
        text = response.text
    line = " ".join(text.split())
    if not line:
        line = _EMPTY_BODY
    elif len(line) > _MAX_BODY_LINE_CHARS:
        line = f"{line[:_MAX_BODY_LINE_CHARS]}…"
    return line


@dataclass
class UploadResult:
    """
    Where a document ended up when upload_document returned.

    The two destinations are mutually exclusive and the payload field for each
    is required, enforced in __post_init__.  A result that claimed API delivery
    while carrying no task UUID would be reported downstream as a
    consume-directory fallback -- the replacement for the old "fallback"
    sentinel must not be able to lie about itself the way the sentinel could.

    Attributes:
        delivered_to_api: True when paperless-ngx accepted the upload.
        task_uuid: Paperless task id. Present iff delivered_to_api is True.
        consume_dir_path: Where the PDF was copied instead. Present iff
            delivered_to_api is False.

    """

    delivered_to_api: bool
    task_uuid: str | None = None
    consume_dir_path: Path | None = None

    def __post_init__(self) -> None:
        """Reject the two contradictory combinations."""
        if self.delivered_to_api:
            if self.task_uuid is None:
                msg = "delivered_to_api=True requires a task_uuid"
                raise ValueError(msg)
            if self.consume_dir_path is not None:
                msg = "delivered_to_api=True cannot carry a consume_dir_path"
                raise ValueError(msg)
        else:
            if self.consume_dir_path is None:
                msg = "delivered_to_api=False requires a consume_dir_path"
                raise ValueError(msg)
            if self.task_uuid is not None:
                msg = "delivered_to_api=False cannot carry a task_uuid"
                raise ValueError(msg)


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
            "headers": {
                "Authorization": f"Token {token}",
                "Accept": _API_VERSION_ACCEPT,
            },
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
    ) -> UploadResult:
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
            An UploadResult. On success ``delivered_to_api`` is True and
            ``task_uuid`` carries the paperless-ngx task id. When the
            retries are exhausted and a consume directory is configured,
            ``delivered_to_api`` is False and ``consume_dir_path`` names
            the file the PDF was copied to.

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
                if task_id is None:
                    # A JSON null body would otherwise become the string
                    # "None" -- truthy, not None, and polled as a real task id.
                    msg = "Paperless accepted the upload but returned no task ID"
                    raise PaperlessError(msg)
                logger.info("Upload succeeded, task ID: %s", task_id)
                return UploadResult(delivered_to_api=True, task_uuid=str(task_id))

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
            self._deliver_to_consume_dir(pdf_path, dest_dir, dest)
            logger.warning("All retries exhausted. Copied PDF to %s", dest)
            return UploadResult(delivered_to_api=False, consume_dir_path=dest)

        msg = f"Upload failed after {self._max_retries} retries"
        raise PaperlessError(msg) from last_error

    @staticmethod
    def _deliver_to_consume_dir(pdf_path: Path, dest_dir: Path, dest: Path) -> None:
        """
        Hand a whole PDF to the consume directory, never a partial one.

        paperless-ngx watches the consume directory with inotify and acts on
        what appears there, so writing the bytes directly to the final name
        lets it pick up a half-written PDF.  Instead the bytes go to a hidden
        staging file, are flushed and fsynced, and only then take their final
        name in one ``rename(2)``.

        Two details are load-bearing:

        * The staging file is a **dotfile**, which the consumer skips
          outright.  Relying instead on it merely ignoring an unknown
          ``.part`` extension would trade log spam for atomicity.
        * The staging file lives **inside the consume directory**, not in
          the caller's temporary directory.  ``rename(2)`` is atomic only
          within one filesystem, and the consume directory is typically a
          Docker volume or a network share -- a rename across that boundary
          fails with EXDEV.
        * The rename is ``Path.replace``, which delegates to ``os.replace``
          and so is the atomic, unconditionally-overwriting one.
          ``Path.rename`` is not a substitute: it refuses an existing
          destination on Windows.  ``os.replace`` is not called directly
          only because ruff's PTH105 forbids it and this project does not
          permit per-line suppressions.

        The file is fsynced but the directory is not: the consume directory
        is a handoff, not a system of record, so paying for file durability
        is worth it while a directory fsync is not.

        Args:
            pdf_path: The PDF to hand over.
            dest_dir: The consume directory, which must already exist.
            dest: The final path inside dest_dir.

        Raises:
            OSError: Whatever the copy or the rename raised, re-raised after
                the staging file is removed so a failed handoff leaves no
                truncated remnant for a retry or the consumer to find.

        """
        staged = dest_dir / f".{pdf_path.name}.part"
        try:
            with staged.open("wb") as staged_file, pdf_path.open("rb") as source:
                shutil.copyfileobj(source, staged_file)
                staged_file.flush()
                os.fsync(staged_file.fileno())
            staged.replace(dest)
        except OSError:
            staged.unlink(missing_ok=True)
            raise

    def poll_task(self, task_id: str, timeout: int | float = 300) -> dict[str, object]:
        """
        Poll the task endpoint with exponential backoff until the task succeeds.

        Two wire shapes are tolerated. API v9 answers ``GET /api/tasks/``
        with a bare list of tasks whose ``status`` is uppercase and whose
        failure text is a flat ``result`` string.  API v10 paginates the
        list into ``{"count", "next", "previous", "results"}``, spells the
        status in lowercase, and moved the failure text into
        ``result_data["error_message"]``.  The client pins v9 in its
        ``Accept`` header, and reading both shapes anyway means it keeps
        working if that pin ever stops being honoured.

        A 200 carrying no task is not an error.  A task is not always
        visible immediately after the upload that created it, so an empty
        list (or an empty ``results``) means "ask again", and polling
        continues until the deadline.  A non-200 *is* an error and ends the
        poll at once: a revoked token or a moved endpoint should be reported
        as what it is within a second, not as a timeout several minutes
        later.

        Args:
            task_id: Task UUID returned from upload.
            timeout: Maximum seconds to wait, measured on a monotonic clock
                that includes request time as well as sleep time.

        Returns:
            The task dict, only when the task reached SUCCESS.

        Raises:
            PaperlessError: If the task ends FAILURE or REVOKED, carrying
                the message paperless-ngx supplied, or if any poll returns a
                non-200 response, carrying the status code, the reason
                phrase and the body reduced to one bounded line by
                ``_render_error_body`` (D-09).
            PaperlessTimeoutError: If the deadline passes before the task
                reaches a terminal status. The message names the task id so
                the task can be looked up in paperless-ngx directly.

        """
        deadline = time.monotonic() + timeout
        delay = 0.5

        while True:
            response = self._client.get(
                "/api/tasks/",
                params={"task_id": task_id},
            )
            if response.status_code != 200:
                msg = (
                    f"Paperless task poll failed ({response.status_code} "
                    f"{response.reason_phrase}): {_render_error_body(response)}"
                )
                raise PaperlessError(msg)

            task = _extract_task(response.json())
            if task is not None:
                status = _task_status(task)
                if status == "SUCCESS":
                    logger.info("Task %s completed: %s", task_id, status)
                    return task
                if status in _TERMINAL_STATUSES:
                    msg = (
                        f"Paperless task {task_id} ended {status}: "
                        f"{_failure_message(task)}"
                    )
                    raise PaperlessError(msg)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("Task %s did not finish within %ss", task_id, timeout)
                msg = f"Paperless task {task_id} did not finish within {timeout}s"
                raise PaperlessTimeoutError(msg)

            # Clamped so the poll never sleeps past its own deadline -- that
            # is what makes a sub-second timeout cost what it says it does
            # rather than the 0.5s first backoff.
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 30.0)

    def test_connection(self) -> ConnectionStatus:
        """
        Probe paperless-ngx and report which of five outcomes occurred.

        CONNECTED means a 2xx and nothing else.  A 404 says the API is not
        where the configured URL points -- a different thing to fix than a
        500, which says paperless-ngx itself is unwell, and both used to be
        reported as CONNECTED.

        The classification is an ordered chain of integer comparisons rather
        than a ``match`` with ``assert_never``, for the same reason
        ``vocabulary.classify_error`` is an ``isinstance`` chain: the input
        is a range of integers, not a closed set of members, so exhaustive
        matching does not apply and a trailing fallback is the correct total
        answer.  ``assert_never`` governs the *message* lookup in
        ``vocabulary.connection_status_message``, which does dispatch on a
        closed set.

        Returns:
            A ConnectionStatus member.  It is a StrEnum, and its values are
            the public JSON contract documented in
            ``docs/reference/web-api.md`` -- ``web/routes.py`` serialises the
            return value straight into a response body, so the values may
            not be renamed without breaking existing clients.

        """
        try:
            response = self._client.get("/api/tags/", params={"page_size": 1})
        except httpx.TransportError:
            # The base class of ConnectError, ConnectTimeout and ReadTimeout.
            # Catching only ConnectError let the timeout siblings escape to
            # routes.py's blanket handler, which answers HTTP 502
            # {"status": "error"} -- none of the five outcomes.
            logger.warning("Paperless is unreachable")
            return ConnectionStatus.UNREACHABLE

        if response.is_success:
            return ConnectionStatus.CONNECTED
        if response.status_code in (401, 403):
            return ConnectionStatus.TOKEN_REJECTED
        if response.status_code == 404:
            return ConnectionStatus.NOT_FOUND
        logger.warning("Unexpected paperless status %s", response.status_code)
        return ConnectionStatus.SERVER_ERROR

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
