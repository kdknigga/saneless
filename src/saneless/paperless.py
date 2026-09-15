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
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .exceptions import PaperlessError, PaperlessTimeoutError, describe
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

_DUPLICATE_HINT = (
    "the document may already be in Paperless; check before scanning again"
)


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


def _is_duplicate_failure(task: dict[str, object], message: str) -> bool:
    """
    Say whether a failed task was paperless-ngx refusing a duplicate document.

    The shapes differ by version: API v9 says ``Not consuming: It is a
    duplicate of document #N``, paperless-ngx 2.x says ``... It is a Duplicate
    of <title> (#id).``, and API v10 carries only ``result_data`` =
    ``{"duplicate_of": N, "duplicate_in_trash": bool}`` with no message at all.

    D-10's accepted risk is why this is worth recognising: an upload retried
    after its response was lost usually makes current paperless-ngx store a
    silent second copy.  Only with ``CONSUMER_DELETE_DUPLICATES`` is the
    duplicate reported as a failure, and then the user is told the document
    may already be there, rather than being left to scan it again.

    Args:
        task: A task dict whose status is FAILURE or REVOKED.
        message: The failure text ``_failure_message`` chose for it.

    Returns:
        True when the text contains "duplicate of" in any case, or
        ``result_data`` has a ``duplicate_of`` key.

    """
    if "duplicate of" in message.casefold():
        return True
    data = task.get("result_data")
    return isinstance(data, dict) and "duplicate_of" in data


def _one_line_reason(exc: BaseException) -> str:
    """
    Describe a failure cause as one line for a ``PaperlessError`` message.

    A CLI or web message must be a single line (EXC-02), but httpx's own text
    for an ``HTTPStatusError`` is two: ``Server error '503 ...' for url
    '...'`` followed by ``For more information check: <mdn url>``.  A status
    error is therefore rendered as its status, reason phrase and the body
    reduced to one bounded line by ``_render_error_body``; anything else is
    ``describe``, which already collapses whitespace.

    Args:
        exc: The cause.

    Returns:
        A non-empty single line.

    """
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        return (
            f"{response.status_code} {response.reason_phrase}: "
            f"{_render_error_body(response)}"
        )
    return describe(exc)


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
    return _bounded_line(text) or _EMPTY_BODY


_USERINFO = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://)?.*@", re.DOTALL)
"""Everything up to the last ``@`` of a URL, keeping any ``scheme://`` prefix."""


def _without_userinfo(url: str) -> str:
    """
    Remove any ``user:password@`` from a URL before it is shown anywhere.

    Deliberately textual rather than parsed, so it also covers a URL httpx
    rejects or reads differently (no scheme, a bad port), and deliberately
    greedy: it cuts through the *last* ``@``, so a password holding a raw
    ``@`` or ``/`` cannot leave a fragment behind.  The cost is that a base
    URL whose path holds an ``@`` is shown shortened, which is display only;
    a leaked credential cannot be taken back (WR-08).

    Args:
        url: A configured or upstream-supplied URL.

    Returns:
        The URL with nothing left of its userinfo.

    """
    return _USERINFO.sub(r"\1", url, count=1)


def _bounded_line(text: str) -> str:
    """
    Collapse ``text`` to one line and cut it to ``_MAX_BODY_LINE_CHARS``.

    Text from Paperless -- an error body, a redirect target -- is recorded in
    the job store and printed on the terminal, so no newline in it may forge
    an extra line (T-28-24) and no length of it may flood either (T-23-16).

    Args:
        text: Upstream text of any shape.

    Returns:
        The text on one line, with an ellipsis when it was cut; empty when
        it held nothing but whitespace.

    """
    line = " ".join(text.split())
    if len(line) > _MAX_BODY_LINE_CHARS:
        line = f"{line[:_MAX_BODY_LINE_CHARS]}…"
    return line


def _not_accepted_message(response: httpx.Response) -> str:
    """
    Say why a non-2xx upload response that is not a server error is final.

    A 4xx is Paperless rejecting the upload, with its own reason (D-09).  A
    redirect is almost always ``paperless.url`` pointing at the wrong address
    -- a plain ``http://`` URL behind a proxy that redirects to ``https://`` --
    and retrying it cannot help, so it names where it was sent instead (WR-03).
    Anything else (a 1xx, or a 3xx with no target) is reported by its status.

    Args:
        response: The upload response ``raise_for_status`` refused.

    Returns:
        A single line naming the status and what to check.

    """
    status = f"{response.status_code} {response.reason_phrase}"
    location = _bounded_line(_without_userinfo(response.headers.get("location", "")))
    if response.is_client_error:
        return (
            f"Paperless rejected the upload ({status}): {_render_error_body(response)}"
        )
    if response.is_redirect and location:
        return (
            f"Paperless redirected the upload ({status}) to {location}; "
            "check paperless.url"
        )
    return (
        f"Paperless did not accept the upload ({status}): "
        f"{_render_error_body(response)}"
    )


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
    exponential backoff, and connection testing.  The construction and
    upload path is a module boundary (EXC-01): whatever goes wrong there
    leaves as a ``PaperlessError`` naming the configured base URL and the
    original text, chained to its cause, and never carrying the token (D-08).

    Upload failures fall into two groups (D-10, M-17):

    * **Retried** with exponential backoff, for ``max_retries`` attempts in
      total: every transient ``httpx.TransportError`` -- ConnectError, the
      timeouts, ReadError, WriteError, RemoteProtocolError (a reverse proxy
      closing the connection), ProxyError -- and any 5xx response.
    * **Fail fast**, with no further attempt: a 4xx rejection, any other
      non-2xx that is not a server error (a redirect, which names its target
      so ``paperless.url`` can be corrected), a URL with no
      usable scheme (``httpx.UnsupportedProtocol``, which is a TransportError
      but will never succeed on a retry), any other ``httpx.HTTPError``, and
      a 200 whose body is not JSON.

    When a consume directory is configured it is the fallback both for
    exhausted retries and for ``UnsupportedProtocol``: no retry is not no
    fallback, and a scan must never be lost.  A 4xx or a redirect is final
    and is not copied.

    Accepted risk (D-10 amendment): a retry after a response that was lost
    in transit can make paperless-ngx v3, with its default settings, store a
    second copy of the document.  A duplicate is easy to delete; a lost scan
    is not.

    Args:
        url: Base URL of the paperless-ngx instance.
        token: API authentication token.  It is sent only in the
            ``Authorization`` header and never interpolated into a message.
            Any ``user:password@`` in ``url`` is sent as Basic auth, exactly
            as httpx would send it, and is stripped from every message and
            log line.
        consume_dir: Optional fallback directory for PDF upload failures.
        max_retries: Maximum number of upload attempts, including the first.
        _transport: Optional httpx transport for testing.

    Raises:
        PaperlessError: If ``url`` is not a valid URL (``httpx.InvalidURL``).

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
        base_url = url.rstrip("/")
        # The only form of the URL any message or log line may carry: a
        # paperless.url with user:password@ in it (Basic auth for a reverse
        # proxy) must not put that password in job.error, on the terminal or
        # in the log (WR-08, D-08).
        self._display_url = _without_userinfo(base_url)
        client_kwargs: dict = {
            "headers": {
                "Authorization": f"Token {token}",
                "Accept": _API_VERSION_ACCEPT,
            },
            "timeout": 30.0,
        }
        if _transport is not None:
            client_kwargs["transport"] = _transport
        try:
            parsed = httpx.URL(base_url)
            if parsed.userinfo:
                # The credentials travel as the Basic auth httpx would derive
                # from the URL anyway, so the request is unchanged, while the
                # base URL itself -- which httpx names in its own request log
                # line and exception text -- no longer carries them.
                client_kwargs["auth"] = httpx.BasicAuth(
                    parsed.username, parsed.password
                )
                base_url = str(parsed.copy_with(userinfo=b"")).rstrip("/")
            client_kwargs["base_url"] = base_url
            self._client = httpx.Client(**client_kwargs)
        except httpx.InvalidURL as exc:
            msg = f"Paperless URL {self._display_url} is not valid: {describe(exc)}"
            raise PaperlessError(msg) from exc
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
        Tags are submitted as repeated form fields.  Every transient
        transport failure and every 5xx is retried with exponential backoff
        for ``max_retries`` attempts; a 4xx, a redirect or any other non-2xx
        that is not a 5xx, an unusable URL scheme, any
        other httpx error and a non-JSON 200 end the attempts at once
        (D-10, M-17).  When the attempts end without delivery -- exhausted,
        or cut short by ``httpx.UnsupportedProtocol`` -- and a consume
        directory is configured, the PDF is copied there instead.  See the
        class docstring for the accepted duplicate-document risk of retrying.

        Args:
            pdf_path: Path to the PDF file to upload.
            title: Document title.
            tags: Optional list of tag IDs to attach.
            correspondent: Optional correspondent ID.
            created: Optional creation date string (e.g. "2026-03-20").

        Returns:
            An UploadResult. On success ``delivered_to_api`` is True and
            ``task_uuid`` carries the paperless-ngx task id. When the
            attempts end without delivery and a consume directory is
            configured, ``delivered_to_api`` is False and
            ``consume_dir_path`` names the file the PDF was copied to.

        Raises:
            PaperlessError: If the server rejects the upload with a 4xx
                (``Paperless rejected the upload (<status> <reason>): <line>``)
                or answers with a redirect (``Paperless redirected the upload
                (<status> <reason>) to <location>; check paperless.url``);
                if the attempts end without delivery and no consume directory
                is configured (``failed after N attempts``, or ``Could not
                reach Paperless`` for an unusable URL scheme); if any other
                httpx error occurs; if a 200 body is not JSON or carries no
                task id; or if copying into the consume directory fails.
                Every one is chained to its cause.

        """
        data = self._form_fields(title, tags, correspondent, created)
        last_error: httpx.HTTPError | None = None
        fast_fail = False

        # Clause order is load-bearing: HTTPStatusError and UnsupportedProtocol
        # are caught before the TransportError clause that retries (the latter
        # is itself a TransportError), and HTTPError comes last as the catch-all.
        for attempt in range(self._max_retries):
            try:
                task_id = self._post_document(pdf_path, data)
            except httpx.HTTPStatusError as exc:
                # Only a server error is transient.  A 4xx rejection and a
                # redirect (1xx and 3xx too: raise_for_status refuses every
                # non-2xx) would only be answered the same way again, so they
                # are final: no retry and no fallback (D-10, WR-03).
                if not exc.response.is_server_error:
                    msg = _not_accepted_message(exc.response)
                    raise PaperlessError(msg) from exc
                last_error = exc
                self._back_off(attempt, exc)
            except httpx.UnsupportedProtocol as exc:
                # Logged here because no _back_off runs for it: with a consume
                # directory the scan still ends FALLBACK, and this line is then
                # the only place the operator learns the URL is the problem
                # (WR-04).
                logger.warning(
                    "Paperless URL %s cannot be used (%s); not retrying",
                    self._display_url,
                    _one_line_reason(exc),
                )
                last_error = exc
                fast_fail = True
                break
            except httpx.TransportError as exc:
                last_error = exc
                self._back_off(attempt, exc)
            except httpx.HTTPError as exc:
                msg = (
                    f"Could not upload to Paperless at {self._display_url}: "
                    f"{describe(exc)}"
                )
                raise PaperlessError(msg) from exc
            else:
                logger.info("Upload succeeded, task ID: %s", task_id)
                return UploadResult(delivered_to_api=True, task_uuid=task_id)

        if self._consume_dir:
            return self._fall_back_to_consume_dir(pdf_path)

        reason = (
            "no attempt was made"
            if last_error is None
            else _one_line_reason(last_error)
        )
        if fast_fail:
            msg = f"Could not reach Paperless at {self._display_url}: {reason}"
        else:
            msg = (
                f"Upload to Paperless at {self._display_url} failed after "
                f"{self._max_retries} attempts: {reason}"
            )
        raise PaperlessError(msg) from last_error

    @staticmethod
    def _form_fields(
        title: str,
        tags: list[int] | None,
        correspondent: int | None,
        created: str | None,
    ) -> dict[str, str | list[str]]:
        """
        Build the multipart form fields for an upload.

        Args:
            title: Document title.
            tags: Optional tag IDs, submitted as repeated form fields.
            correspondent: Optional correspondent ID.
            created: Optional creation date string.

        Returns:
            The form fields, with absent metadata left out.

        """
        data: dict[str, str | list[str]] = {"title": title}
        if created is not None:
            data["created"] = created
        if correspondent is not None:
            data["correspondent"] = str(correspondent)
        if tags:
            data["tags"] = [str(tag_id) for tag_id in tags]
        return data

    def _post_document(self, pdf_path: Path, data: dict[str, str | list[str]]) -> str:
        """
        Make one upload attempt and return the task id Paperless assigned.

        Args:
            pdf_path: Path to the PDF file to upload.
            data: The multipart form fields.

        Returns:
            The task id, as a string.

        Raises:
            PaperlessError: If a 200 body is not JSON or is a JSON null.

        Any ``httpx.HTTPError`` from the request or from ``raise_for_status``
        propagates: ``upload_document`` decides which of those to retry.

        """
        with pdf_path.open("rb") as f:
            response = self._client.post(
                "/api/documents/post_document/",
                data=data,
                files={"document": (pdf_path.name, f, "application/pdf")},
            )
        response.raise_for_status()
        try:
            task_id = response.json()
        except ValueError as exc:
            msg = (
                f"Paperless at {self._display_url} returned a response that is "
                f"not JSON: {describe(exc)}"
            )
            raise PaperlessError(msg) from exc
        if task_id is None:
            # A JSON null body would otherwise become the string
            # "None" -- truthy, not None, and polled as a real task id.
            msg = "Paperless accepted the upload but returned no task ID"
            raise PaperlessError(msg)
        return str(task_id)

    def _back_off(self, attempt: int, exc: httpx.HTTPError) -> None:
        """
        Log a failed retryable attempt and sleep before the next one.

        No sleep follows the last attempt: nothing is waiting for it.

        Args:
            attempt: The zero-based attempt that just failed.
            exc: What it failed with.

        """
        # _one_line_reason, not describe: httpx's text for a status error spans
        # lines and names the full request URL (WR-03).
        logger.warning(
            "Upload attempt %d/%d failed: %s",
            attempt + 1,
            self._max_retries,
            _one_line_reason(exc),
        )
        if attempt < self._max_retries - 1:
            time.sleep(2**attempt)

    def _fall_back_to_consume_dir(self, pdf_path: Path) -> UploadResult:
        """
        Copy the PDF into the consume directory after the upload did not land.

        Args:
            pdf_path: The PDF that could not be uploaded.

        Returns:
            An UploadResult naming the file the PDF was copied to.

        Raises:
            PaperlessError: If the directory cannot be created or the copy
                fails, chained to the OSError (EXC-01).

        """
        dest_dir = Path(self._consume_dir)
        dest = dest_dir / pdf_path.name
        try:
            if not dest_dir.exists():
                dest_dir.mkdir(parents=True, exist_ok=True)
                logger.warning("Created consume directory %s", dest_dir)
            self._deliver_to_consume_dir(pdf_path, dest_dir, dest)
        except OSError as exc:
            msg = (
                f"Could not copy the PDF to the consume directory {dest_dir}: "
                f"{describe(exc)}"
            )
            raise PaperlessError(msg) from exc
        logger.warning("Upload failed; copied PDF to %s", dest)
        return UploadResult(delivered_to_api=False, consume_dir_path=dest)

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

        A request-level error while polling (a connection refused, a reset,
        a read timeout, a proxy closing the connection, a body that cannot be
        decoded) does *not* end the poll.  The upload has already been accepted, so failing the job now
        would invite the user to scan the document again and create a
        duplicate (D-11, M-17).  The error is logged and remembered, and the
        poll backs off and asks again within the same monotonic deadline.

        Args:
            task_id: Task UUID returned from upload.
            timeout: Maximum seconds to wait, measured on a monotonic clock
                that includes request time as well as sleep time.

        Returns:
            The task dict, only when the task reached SUCCESS.

        Raises:
            PaperlessError: If the task ends FAILURE or REVOKED, carrying
                the message paperless-ngx supplied (plus a check-before-
                rescanning hint when it was a duplicate, D-10); if any poll
                returns a non-200 response, carrying the status code, the
                reason phrase and the body reduced to one bounded line by
                ``_render_error_body`` (D-09); or if a 200 body is not JSON,
                chained to the ValueError (EXC-01).
            PaperlessTimeoutError: If the deadline passes before the task
                reaches a terminal status. The message names the task id so
                the task can be looked up in paperless-ngx directly, and,
                when the last poll failed with a request error rather than
                being answered, ends by naming that error and is chained to it.

        """
        deadline = time.monotonic() + timeout
        delay = 0.5
        # RequestError rather than TransportError: DecodingError (a corrupt
        # compressed body) is a request-level failure that is not a transport
        # one, and it must neither escape this boundary as a raw httpx type nor
        # fail an upload Paperless already accepted (WR-05, EXC-01, D-11).
        last_transport_error: httpx.RequestError | None = None

        while True:
            try:
                response = self._client.get(
                    "/api/tasks/",
                    params={"task_id": task_id},
                )
            except httpx.RequestError as exc:
                last_transport_error = exc
                logger.warning(
                    "Polling task %s failed, retrying until the deadline: %s",
                    task_id,
                    describe(exc),
                )
            else:
                # Paperless answered, so an earlier blip is no longer the story:
                # a timeout after this names no stale transport error (IN-02).
                last_transport_error = None
                task = self._finished_task(task_id, response)
                if task is not None:
                    return task

            # Every path through the loop body reaches this check -- a
            # transport error included -- so the poll cannot outlive its
            # deadline (T-28-38).
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("Task %s did not finish within %ss", task_id, timeout)
                msg = f"Paperless task {task_id} did not finish within {timeout}s"
                if last_transport_error is None:
                    raise PaperlessTimeoutError(msg)
                msg = f"{msg}; last error: {_one_line_reason(last_transport_error)}"
                raise PaperlessTimeoutError(msg) from last_transport_error

            # Clamped so the poll never sleeps past its own deadline -- that
            # is what makes a sub-second timeout cost what it says it does
            # rather than the 0.5s first backoff.
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 30.0)

    def _finished_task(
        self, task_id: str, response: httpx.Response
    ) -> dict[str, object] | None:
        """
        Read one task poll response.

        Args:
            task_id: The task being polled, for the messages.
            response: The response to ``GET /api/tasks/``.

        Returns:
            The task dict when it reached SUCCESS; None when it is not
            visible yet or has not reached a terminal status, so the caller
            keeps polling.

        Raises:
            PaperlessError: If the response is not a 200, if its body is not
                JSON, or if the task ended FAILURE or REVOKED.  Every message
                is one line (EXC-02).

        """
        if response.status_code != 200:
            msg = (
                f"Paperless task poll failed ({response.status_code} "
                f"{response.reason_phrase}): {_render_error_body(response)}"
            )
            raise PaperlessError(msg)

        try:
            payload = response.json()
        except ValueError as exc:
            msg = (
                f"Paperless at {self._display_url} returned a task response that is "
                f"not JSON: {_one_line_reason(exc)}"
            )
            raise PaperlessError(msg) from exc

        task = _extract_task(payload)
        if task is None:
            return None
        status = _task_status(task)
        if status == "SUCCESS":
            logger.info("Task %s completed: %s", task_id, status)
            return task
        if status in _TERMINAL_STATUSES:
            failure = " ".join(_failure_message(task).split())
            msg = f"Paperless task {task_id} ended {status}: {failure}"
            if _is_duplicate_failure(task, failure):
                msg = f"{msg}; {_DUPLICATE_HINT}"
            raise PaperlessError(msg)
        return None

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

        Raises:
            PaperlessError: If the request fails for any ``httpx.HTTPError``
                (a non-2xx included) or the body is not JSON, naming the
                endpoint and base URL and chained to the cause (D-11, EXC-01).

        """
        return self._fetch_collection("/api/tags/", "tags")

    def get_correspondents(self) -> list[dict[str, object]]:
        """
        Fetch all correspondents from paperless-ngx.

        Returns:
            List of correspondent dicts with at least 'id' and 'name' keys.

        Raises:
            PaperlessError: If the request fails for any ``httpx.HTTPError``
                (a non-2xx included) or the body is not JSON, naming the
                endpoint and base URL and chained to the cause (D-11, EXC-01).

        """
        return self._fetch_collection("/api/correspondents/", "correspondents")

    def _fetch_collection(self, path: str, noun: str) -> list[dict[str, object]]:
        """
        Fetch one metadata collection, tolerating both list response shapes.

        This is a module boundary (EXC-01): no httpx type and no raw
        ValueError leaves it.  A status error is rendered by
        ``_one_line_reason`` as status, reason and body, so the message stays
        one line (EXC-02).

        Args:
            path: The collection endpoint, e.g. ``/api/tags/``.
            noun: What the collection holds, for the message.

        Returns:
            The ``results`` of a paginated response, or a bare list as is.

        Raises:
            PaperlessError: ``Could not fetch <noun> from Paperless at <url>:
                <reason>``, chained to the httpx error or the ValueError.

        """
        prefix = f"Could not fetch {noun} from Paperless at {self._display_url}"
        try:
            response = self._client.get(path, params={"page_size": 1000})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            msg = f"{prefix}: {_one_line_reason(exc)}"
            raise PaperlessError(msg) from exc
        try:
            data = response.json()
        except ValueError as exc:
            msg = f"{prefix}: {_one_line_reason(exc)}"
            raise PaperlessError(msg) from exc
        return data.get("results", []) if isinstance(data, dict) else data

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()
