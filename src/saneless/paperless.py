"""
Paperless-ngx REST API client with retry, polling, and connection test.

Uploads PDFs with metadata (title, tags, correspondent) but no document
date, which paperless-ngx chooses itself; polls the task endpoint with
exponential backoff until a terminal state and raises when that state is
not success; and probes connections, reporting a ConnectionStatus outcome.
Every request names the highest supported API version the server has
announced (see ``PaperlessClient.api_version``).
"""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
import stat
import tempfile
import time
import traceback
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Final, assert_never

import httpx2

from .atomic_write import refused_mode_change
from .exceptions import (
    ConfigError,
    PaperlessError,
    PaperlessIncompatibleError,
    PaperlessTimeoutError,
    PaperlessTrustStoreError,
    PaperlessUncertainSendError,
    PaperlessUnconfirmedError,
    describe,
)
from .text_safety import neutralise_bounded, neutralise_controls
from .vocabulary import ConnectionStatus

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "ApiDelivery",
    "ConnectionProbe",
    "FolderDelivery",
    "PaperlessClient",
    "PaperlessTiming",
    "TaskDuplicate",
    "TaskFiled",
    "TaskOutcome",
    "UploadResult",
]

logger = logging.getLogger(__name__)

# paperless-ngx serves its own default API version to a client that names none,
# so every request names one; 2.16 to 2.20 allow up to 9, 3.x allows 9 and 10,
# and a version a server does not allow gets 406 Not Acceptable.  The highest
# allowed version is announced in ``X-Api-Version`` only on answers to an
# authenticated request, never on a 401 or the 406 itself.  So the first request
# speaks 9, and its answer says whether 10 may follow.
_SUPPORTED_API_VERSIONS: Final = (9, 10)
_ACCEPT_TEMPLATE: Final = "application/json; version={version}"
# The Accept header of a request that asked for the newest version.  A learned
# version goes stale when paperless-ngx is rolled back or another server answers
# at the same address, and the only sign is a 406 to exactly this header.
_NEWEST_ACCEPT: Final = _ACCEPT_TEMPLATE.format(version=_SUPPORTED_API_VERSIONS[-1])
# One to three ASCII digits, the whole value: a longer, signed, spaced or
# non-ASCII value is not a version and is never read.  ``[0-9]``, not ``\d``,
# which also matches digits from other scripts that ``int()`` accepts.
_API_VERSION_HEADER: Final = re.compile(r"[0-9]{1,3}")

# paperless-ngx's COMPLETE_STATUSES, normalised to the v9 uppercase spelling.
# REVOKED belongs here: it is an administrative cancellation that will never
# progress, so polling it to the deadline would report a misattributed timeout.
_TERMINAL_STATUSES = frozenset({"SUCCESS", "FAILURE", "REVOKED"})

# How much of an error response body reaches an error message, which the job
# store, the web status area and the terminal all show; a multi-megabyte body
# must not flood them.  The full body is logged at DEBUG instead.
_MAX_BODY_LINE_CHARS = 200

_EMPTY_BODY = "(empty response body)"

_NO_FAILURE_MESSAGE = "Paperless reported a failure but supplied no message"

# How many tags or correspondents to ask for per page; a typical install fits
# in one round trip, and larger collections take a few.
_METADATA_PAGE_SIZE: Final = 1000

# The most pages one metadata fetch asks for when the server gives no usable
# ``count``.  A server or proxy that ignores ``?page=`` but varies its answer
# would otherwise keep the fetch, and the metadata cache lock, busy forever.
_METADATA_MAX_PAGES: Final = 1000

# The most characters of a paperless task id that reach a log line or an error
# message.  The id is third-party text, and a misbehaving server or proxy can
# answer with megabytes or terminal escapes.
_TASK_ID_LOG_LIMIT: Final = 64

# How a duplicate refusal's text names the existing document, for a server whose
# answer carries no structured id.  ``[0-9]``, not ``\d``, which also matches
# digits from other scripts that ``int()`` accepts.
_DUPLICATE_OF: Final = re.compile(r"duplicate of ", re.IGNORECASE)
# paperless-ngx's own wording of a duplicate refusal.  A duplicate ends the job
# delivered and keeps no copy, so a failure whose text merely quotes "duplicate
# of" (a workflow error, document content) must not match.
_DUPLICATE_REFUSAL: Final = re.compile(
    r"It is a duplicate of |duplicate of document #[0-9]", re.IGNORECASE
)
_BRACKETED_ID: Final = re.compile(r"\(#([0-9]+)\)")
_DOCUMENT_ID: Final = re.compile(r"document #([0-9]+)", re.IGNORECASE)
# A document id: one to eighteen ASCII digits, the whole value, so a longer
# one never reaches ``int()`` or a message.
_DOCUMENT_ID_TEXT: Final = re.compile(r"[0-9]{1,18}")
# paperless-ngx 2.x appends this when the existing document is in the trash.
_TRASH_NOTE: Final = "existing document is in the trash"

# How long an upload keeps trying while each failure proves the request never
# reached paperless-ngx whole: long enough to ride out a restart.  After it the
# consume-folder fallback, if any, takes over.
_SEND_BUDGET_SECONDS: Final = 60.0

# The longest single wait between those attempts, so a server that comes back
# is found within a few seconds.
_MAX_BACKOFF_SECONDS: Final = 5.0

# The longest single wait between task polls.
_MAX_POLL_DELAY_SECONDS: Final = 5.0

# Shorter than the client-wide timeout, so a host that drops packets still gets
# several attempts within the send budget.
_UPLOAD_CONNECT_SECONDS: Final = 10.0

# The upload's read timeout: a floor, a rate per MiB of the PDF and a cap.
# paperless-ngx answers only after it has written the whole file to its scratch
# directory, so a large PDF on a slow disk answers late.  The cap matches
# paperless-ngx's default task timeout.
_UPLOAD_READ_FLOOR_SECONDS: Final = 30.0
_UPLOAD_READ_SECONDS_PER_MIB: Final = 1.0
_UPLOAD_READ_CAP_SECONDS: Final = 300.0

# The client-wide timeout: metadata fetches, the connection test, task polls,
# and the write and pool phases of an upload.
_CLIENT_TIMEOUT_SECONDS: Final = 30.0

# How every after-send failure ends, so the reader knows not to scan again
# before checking paperless-ngx.
_MAY_HAVE_REACHED: Final = "; it may have reached paperless-ngx"


def _upload_timeout(size_bytes: int) -> httpx2.Timeout:
    """
    Build the upload request's timeout for a PDF of ``size_bytes``.

    Args:
        size_bytes: The size of the PDF being uploaded.

    Returns:
        The upload connect timeout, a read timeout that grows with the size up
        to its cap, and the client-wide timeout for writes and the pool.

    """
    read = min(
        _UPLOAD_READ_CAP_SECONDS,
        _UPLOAD_READ_FLOOR_SECONDS + _UPLOAD_READ_SECONDS_PER_MIB * size_bytes / 2**20,
    )
    return httpx2.Timeout(
        _CLIENT_TIMEOUT_SECONDS, connect=_UPLOAD_CONNECT_SECONDS, read=read
    )


def _unreadable_pdf(pdf_path: Path, exc: OSError) -> PaperlessError:
    """
    Build the error for a PDF that cannot be read to upload it.

    Args:
        pdf_path: The PDF.
        exc: What reading it raised.

    Returns:
        The error, for the caller to chain to ``exc``.

    """
    return PaperlessError(
        f"Could not read the PDF {pdf_path} to upload it: "
        f"{exc.strerror or describe(exc)}"
    )


def _extract_task(payload: object, task_id: str) -> dict[str, object] | None:
    """
    Return the task with id ``task_id`` from either paperless-ngx response shape.

    API v9 answers with a bare list and v10 with a page whose ``results``
    holds it.  A proxy that drops ``?task_id=`` returns other tasks, so only
    an entry whose ``task_id`` equals ours is chosen.

    Returns:
        Our task, or None.  None is not an error: a new task is not always
        visible yet, so the caller keeps polling until its deadline.

    """
    if isinstance(payload, dict):
        payload = payload.get("results")
    if not isinstance(payload, list):
        return None
    for entry in payload:
        if isinstance(entry, dict) and entry.get("task_id") == task_id:
            return {str(key): value for key, value in entry.items()}
    return None


@dataclass(frozen=True, slots=True)
class _PollTransient:
    """
    A task poll answer that says "ask again later", not "this failed".

    A 5xx (usually a proxy in front of a restarting paperless-ngx) or a 429
    can clear within the poll's deadline.

    Attributes:
        description: The status and body on one bounded line, with the token
            struck, for the log and for the timeout message.

    """

    description: str


def _usable_count(page: dict[object, object]) -> int | None:
    """
    Return a paginated page's ``count``, or None when it cannot bound a fetch.

    Only a non-negative int that is not a bool is a count.
    """
    count = page.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    return count


def _metadata_items(value: object, prefix: str, page: int) -> list[dict[str, object]]:
    """
    Return one metadata page's items, checked to be a list of objects.

    A ``null`` would otherwise raise a TypeError out of the client, and a
    string or an object would be taken apart into characters or keys.

    Raises:
        PaperlessError: ``value`` is not a list of objects.

    """
    if not isinstance(value, list):
        msg = f"{prefix}: page {page} did not hold a list of objects"
        raise PaperlessError(msg)
    items: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            msg = f"{prefix}: page {page} did not hold a list of objects"
            raise PaperlessError(msg)
        items.append({str(key): field for key, field in item.items()})
    return items


def _task_status(task: dict[str, object]) -> str:
    """
    Return the task status in the v9 uppercase spelling, or "" when absent.

    API v10 spells the same statuses in lowercase.
    """
    return str(task.get("status", "")).upper()


def _failure_message(task: dict[str, object]) -> str:
    """
    Return the failure text paperless-ngx supplied, from whichever field has it.

    API v9 carries a flat ``result``; v10 has ``result_data.error_message``
    (or ``reason`` for some rejections).  Never empty: it becomes the job's
    error, which a reader must be able to act on.
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

    API v10 carries only ``result_data.duplicate_of``; earlier versions word
    it in ``message`` (``_DUPLICATE_REFUSAL``, matched ignoring case).  A
    duplicate keeps no copy of the scan, so a false match would lose it
    behind a job that reports success: only these exact shapes count.
    """
    data = task.get("result_data")
    if isinstance(data, dict) and _document_id(data.get("duplicate_of")) is not None:
        return True
    return _DUPLICATE_REFUSAL.search(message) is not None


def _document_id(value: object) -> int | None:
    """
    Read a document id from a field of a task payload.

    Only a positive int or a string of ASCII digits is taken, since the id
    reaches the job's warning.  A bool is refused: v10's
    ``duplicate_of: true`` is not document #1.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and _DOCUMENT_ID_TEXT.fullmatch(value):
        number = int(value)
    else:
        return None
    return number if 0 < number < 10**18 else None


def _duplicate_id_in_text(text: str) -> int | None:
    """
    Find the existing document's id in a duplicate refusal's text.

    In ``It is a duplicate of <title> (#N).`` the title can hold a ``(#5)``
    of its own, so the id is the last ``(#N)`` after "duplicate of", as
    paperless-ngx's greedy pattern takes it.  That pattern is not used here:
    its backtracking is quadratic on server text of any length.
    """
    start = _DUPLICATE_OF.search(text)
    if start is not None:
        ids = [found.group(1) for found in _BRACKETED_ID.finditer(text, start.end())]
        if ids:
            return _document_id(ids[-1])
    found = _DOCUMENT_ID.search(text)
    return _document_id(found.group(1)) if found is not None else None


def _duplicate_of(task: dict[str, object], full_failure: str) -> TaskDuplicate:
    """
    Say which document a duplicate refusal names, and whether it is in the trash.

    The first shape to name an id wins: v10's ``result_data``, v9 on 3.x's
    ``duplicate_documents``, v9 on 2.x's ``related_document`` (null when the
    document is in the trash, which the text then says), then the text.
    """
    data = task.get("result_data")
    flagged = isinstance(data, dict) and data.get("duplicate_in_trash") is True
    if isinstance(data, dict):
        document_id = _document_id(data.get("duplicate_of"))
        if document_id is not None:
            return TaskDuplicate(document_id, in_trash=flagged)
    documents = task.get("duplicate_documents")
    if isinstance(documents, list) and documents and isinstance(documents[0], dict):
        first = documents[0]
        document_id = _document_id(first.get("id"))
        if document_id is not None:
            return TaskDuplicate(
                document_id, in_trash=first.get("deleted_at") is not None
            )
    in_trash = flagged or _TRASH_NOTE in full_failure.casefold()
    document_id = _document_id(task.get("related_document"))
    if document_id is None:
        document_id = _duplicate_id_in_text(full_failure)
    return TaskDuplicate(document_id, in_trash=in_trash)


def _strike(text: str, token: str) -> str:
    """
    Strike the configured token out of text saneless did not write.

    h11 quotes a refused header value whole, and a proxy may quote the
    ``Authorization`` header, so all such text passes through here before it
    is cut to length, or a fragment of the token could survive the cut.  The
    stripped token is struck too, since a bytes repr no longer contains the
    raw form; a one-character token is left alone.
    """
    for secret in (token, token.strip()):
        if len(secret) >= 2:
            text = text.replace(secret, "***")
    return text


def _one_line_reason(exc: BaseException, token: str) -> str:
    """
    Describe a failure cause as one line for a ``PaperlessError`` message.

    httpx2's own text for an ``HTTPStatusError`` is two lines, so a status
    error is rendered from its status and body instead.
    """
    if isinstance(exc, httpx2.HTTPStatusError):
        response = exc.response
        return f"{_status_text(response, token)}: {_render_error_body(response, token)}"
    return _strike(describe(exc), token)


class _RetryDecision(Enum):
    """
    What the client does with a failed request.

    In memory only: never persisted and never shown.

    Attributes:
        BEFORE_SEND: Nothing can have arrived: no connection was made, or the
            body provably never finished leaving.  Back off and try again
            within the send budget; once it is spent, fall back to the consume
            directory when one is configured.
        AFTER_SEND: The body may have been read, so the document may already
            be stored.  Never sent again and never copied to the consume
            directory: either could make a second document of one scan.
        INCOMPATIBLE: A 406: this server does not accept the API version
            asked for.  It refuses before it reads the request, so nothing was
            stored; final, with no retry and no copy.
        REFUSED: The server answered with a non-2xx that is neither a 5xx nor
            a 406.  It would answer the same way again, so this is final: no
            retry and no fallback.
        MISCONFIGURED: The request could not be sent at all.  No retry can
            succeed and no copy is made.  ``PaperlessClient._unsendable_error``
            reports it as a configuration error when the configured
            ``paperless.url`` or ``paperless.token`` is what the transport
            refused, and as a request the HTTP library refused otherwise.
        UNEXPECTED: Any other httpx2 error.  Final.

    """

    BEFORE_SEND = auto()
    AFTER_SEND = auto()
    INCOMPATIBLE = auto()
    REFUSED = auto()
    MISCONFIGURED = auto()
    UNEXPECTED = auto()


def _retry_decision(exc: httpx2.HTTPError) -> _RetryDecision:
    """
    Classify an httpx2 error: the one place the client decides what to do.

    The question is whether the body can have reached paperless-ngx.  A
    refused connection, no pool slot, a refused tunnel and a stalled body
    write cannot have delivered it whole; any answer (a proxy's 5xx
    included) or failure while reading one may follow a stored document.
    The HTTP library reports a cut body write as a read failure, and
    anything unforeseen is UNEXPECTED rather than retried.
    """
    if isinstance(exc, httpx2.HTTPStatusError):
        return _status_decision(exc.response)
    # Both are TransportError subclasses, so this test must come before the
    # TransportError one: h11 refusing a header value, or a URL with no usable
    # scheme, fails the same way on every attempt.
    if isinstance(exc, httpx2.LocalProtocolError | httpx2.UnsupportedProtocol):
        return _RetryDecision.MISCONFIGURED
    if isinstance(
        exc,
        httpx2.ConnectError
        | httpx2.ConnectTimeout
        | httpx2.PoolTimeout
        | httpx2.ProxyError
        | httpx2.WriteTimeout,
    ):
        return _RetryDecision.BEFORE_SEND
    if isinstance(exc, httpx2.TransportError | httpx2.DecodingError):
        return _RetryDecision.AFTER_SEND
    return _RetryDecision.UNEXPECTED


def _refused_newest_version(response: httpx2.Response) -> bool:
    """
    Say whether ``response`` is a 406 to a request that asked for version 10.

    The announcement 10 was learned from no longer holds, so asking again
    with 9 can succeed.  A 406 is decided before the server reads anything
    else, so asking again cannot duplicate a document.
    """
    return (
        response.status_code == httpx2.codes.NOT_ACCEPTABLE
        and response.request.headers.get("Accept") == _NEWEST_ACCEPT
    )


def _status_decision(response: httpx2.Response) -> _RetryDecision:
    """
    Classify a non-2xx answer for ``_retry_decision``.

    Args:
        response: The response ``raise_for_status`` refused.

    Returns:
        INCOMPATIBLE for a 406, AFTER_SEND for a 5xx, else REFUSED.

    """
    if response.status_code == httpx2.codes.NOT_ACCEPTABLE:
        return _RetryDecision.INCOMPATIBLE
    if response.is_server_error:
        return _RetryDecision.AFTER_SEND
    return _RetryDecision.REFUSED


# The fixed problems a MISCONFIGURED error is reported as.  Fixed, never the
# exception's text: h11 refusing a header value quotes the value, which for the
# Authorization header is the token.
_URL_UNUSABLE: Final = (
    "paperless.url is not set, or has no http or https scheme; "
    "set it to the paperless-ngx address"
)
_REQUEST_UNSENDABLE: Final = (
    "the request could not be sent with the configured paperless.url and "
    "paperless.token; check both for spaces, line breaks or control characters"
)


def _is_visible_ascii(text: str) -> bool:
    """
    Say whether ``text`` is visible ASCII only, the rule the config load applies.

    A header value or URL made only of these characters is one h11 sends.
    """
    return all("!" <= char <= "~" for char in text)


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

    Prefers ``detail``, then the first field error as ``field: msg``, then
    the first string of a bare list.

    Returns:
        The text, or None when no shape matches and the raw body is used.

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


def _render_error_body(response: httpx2.Response, token: str) -> str:
    """
    Reduce a Paperless error response body to one bounded line.

    A DRF JSON error is reduced to its message; anything else is used as it
    stands.  The token is struck before the cut, and the full body, struck
    too, is logged at DEBUG with its controls written out as escapes.
    """
    logger.debug(
        "Paperless error body (%s): %s",
        response.status_code,
        neutralise_controls(_strike(response.text, token)),
    )
    try:
        text = _json_error_text(response.json())
    except ValueError:
        text = None
    if text is None:
        text = response.text
    return _bounded_line(_strike(text, token)) or _EMPTY_BODY


_USERINFO = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://)?.*@", re.DOTALL)
"""Everything up to the last ``@`` of a URL, keeping any ``scheme://`` prefix."""


def _without_userinfo(url: str) -> str:
    """
    Remove any ``user:password@`` from a URL before it is shown anywhere.

    Textual, so it also covers a URL httpx2 cannot parse, and greedy to the
    last ``@``, so a password holding ``@`` or ``/`` leaves no fragment.  A
    path holding ``@`` is shown shortened: display only, unlike a leak.
    """
    return _USERINFO.sub(r"\1", url, count=1)


def _bounded_line(text: str) -> str:
    """
    Collapse ``text`` to one line, tame its controls and cut it to length.

    Upstream text reaches the job store, the terminal and the log, so it must
    not forge a line, flood them or carry a live control character.  The cut
    comes last, so it bounds what is printed.
    """
    line = neutralise_controls(" ".join(text.split()))
    if len(line) > _MAX_BODY_LINE_CHARS:
        line = f"{line[:_MAX_BODY_LINE_CHARS]}…"
    return line


def _status_text(response: httpx2.Response, token: str) -> str:
    """
    Render a response's status code and reason phrase for a message.

    The reason phrase is upstream text: HTTP allows control characters in it,
    and a proxy can echo the ``Authorization`` header into it.
    """
    reason = _bounded_line(_strike(response.reason_phrase, token))
    return f"{response.status_code} {reason}".rstrip()


def _loggable_task_id(task_id: str) -> str:
    """
    Tame a paperless task id for a log line or an error message.

    The poll still sends the raw id back to paperless.
    """
    return neutralise_bounded(task_id, _TASK_ID_LOG_LIMIT)


def _not_accepted_message(response: httpx2.Response, token: str) -> str:
    """
    Say why a non-2xx upload response that is not a server error is final.

    A redirect is almost always ``paperless.url`` naming ``http://`` behind a
    proxy that redirects to ``https://``, so it names where it was sent.
    """
    status = _status_text(response, token)
    location = _bounded_line(
        _strike(_without_userinfo(response.headers.get("location", "")), token)
    )
    if response.is_client_error:
        return (
            f"Paperless rejected the upload ({status}): "
            f"{_render_error_body(response, token)}"
        )
    if response.is_redirect and location:
        return (
            f"Paperless redirected the upload ({status}) to {location}; "
            "check paperless.url"
        )
    return (
        f"Paperless did not accept the upload ({status}): "
        f"{_render_error_body(response, token)}"
    )


def _redirect_target(
    response: httpx2.Response, token: str
) -> tuple[str, httpx2.URL | None]:
    """
    Resolve and sanitise where a redirect answer points.

    The token is struck before the join as well as after, because the join
    percent-encodes some characters and would hide the token from the second
    strike.

    Returns:
        The target as it may be shown, empty when there is none, and the
        resolved URL, or None when there is none or it does not parse.

    """
    raw = response.headers.get("location", "").strip()
    if not raw:
        return "", None
    try:
        target = response.url.join(_strike(raw, token))
    except httpx2.InvalidURL:
        return _bounded_line(_strike(_without_userinfo(raw), token)), None
    return _bounded_line(_strike(_without_userinfo(str(target)), token)), target


@dataclass(frozen=True, slots=True)
class ConnectionProbe:
    """
    What a connection test found, with what only the operator should see.

    ``status`` is the public outcome: the web API serialises it, and the
    status strip shows its fixed message.  The other two fields describe a
    redirect, for the log and ``saneless doctor``.

    Attributes:
        status: The connection-test outcome.
        redirect_target: Where a redirect pointed, resolved against the
            request and sanitised: no userinfo, the token struck out, one
            line with no control characters, bounded in length.  None unless
            the status is REDIRECTED and the answer named a target.  It is
            upstream text, so it is never shown on the LAN-visible strip.
        https_upgrade: True only when the redirect went from ``http`` to
            ``https`` on the same host and port, to the same path or to the
            configured base path -- the case where the fix is to use
            ``https://`` in paperless.url.

    """

    status: ConnectionStatus
    redirect_target: str | None = None
    https_upgrade: bool = False


@dataclass(frozen=True, slots=True)
class ApiDelivery:
    """
    paperless-ngx accepted the upload and gave it this task id.

    Attributes:
        task_id: The paperless-ngx consume task to poll.

    """

    task_id: str


@dataclass(frozen=True, slots=True)
class FolderDelivery:
    """
    The PDF was copied into the consume folder at this path.

    Attributes:
        path: The copy paperless-ngx will find in its consume folder.

    """

    path: Path


# A plain union rather than a ``type`` statement, so ``isinstance`` accepts it.
UploadResult = ApiDelivery | FolderDelivery


@dataclass(frozen=True, slots=True)
class TaskFiled:
    """
    paperless-ngx finished the consume task and filed the document.

    Attributes:
        task: The SUCCESS task, as paperless-ngx answered it.

    """

    task: dict[str, object]


@dataclass(frozen=True, slots=True)
class TaskDuplicate:
    """
    paperless-ngx refused the upload because it already holds the file.

    The document is there, so this is not a failure: nothing was stored
    again, and this upload's title and tags were not applied to the copy
    paperless-ngx already had.

    Attributes:
        document_id: The existing document's id, or None when the answer did
            not name one.
        in_trash: Whether that document is in paperless-ngx's trash.

    """

    document_id: int | None
    in_trash: bool


# How a consume task that finished without failing ended.
TaskOutcome = TaskFiled | TaskDuplicate


@dataclass(frozen=True, slots=True, kw_only=True)
class PaperlessTiming:
    """
    How long the client keeps trying, and the clock it measures that on.

    The defaults are the production values; a test swaps in a fake clock.

    Attributes:
        send_budget: How many seconds an upload keeps retrying failures that
            prove nothing arrived, counted from its first attempt on
            ``clock``.  0 makes exactly one attempt.
        clock: The monotonic clock the send budget and the task poll's
            deadline are measured on.
        sleep: How the upload waits between those attempts, and the task
            poll between its polls.
        upload_timeout: Builds the upload request's timeout from the PDF's
            size in bytes.

    """

    send_budget: float = _SEND_BUDGET_SECONDS
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    upload_timeout: Callable[[int], httpx2.Timeout] = _upload_timeout


_DEFAULT_TIMING: Final = PaperlessTiming()


class PaperlessClient:
    """
    Client for the paperless-ngx REST API.

    Handles document uploads with metadata, task polling with exponential
    backoff, and connection testing.  Whatever goes wrong while the client is
    built or an upload runs leaves as a ``PaperlessError`` or ``ConfigError``
    with the token struck out of any text it quotes and no chained cause that
    quotes it (see ``_cause``).

    ``_retry_decision`` sorts every upload failure by whether the upload can
    have reached paperless-ngx.  Only a failure before send is retried, for
    the send budget, and only then may the consume directory take the PDF; an
    upload that may have arrived is never resent or copied, and a refusal, a
    406 or a misconfiguration is final.
    See docs/explanation/decisions/0009-no-resend-after-send.md.

    Args:
        url: Base URL of the paperless-ngx instance.  One carrying a user
            name or password is refused.
        token: API authentication token, sent only in the ``Authorization``
            header; it is kept only to strike it out of quoted text.
        consume_dir: Optional fallback directory for an upload whose
            before-send budget ran out; None disables the fallback copy.
        transport: The httpx2 transport requests go through; None uses
            httpx2's default.  Tests pass an ``httpx2.MockTransport``.
        timing: The upload's send budget, the clock and sleep it runs on, and
            how its timeout is built; see ``PaperlessTiming``.

    Raises:
        PaperlessError: If ``url`` is not a valid URL or carries a user name
            or password, or ``token`` holds a character an HTTP header cannot
            carry (each with fixed text and no cause, since the library's text
            can quote a secret), or the TLS trust store named by
            ``SSL_CERT_FILE`` or ``SSL_CERT_DIR`` cannot be read.

    """

    def __init__(
        self,
        url: str,
        token: str,
        consume_dir: Path | None = None,
        *,
        transport: httpx2.BaseTransport | None = None,
        timing: PaperlessTiming = _DEFAULT_TIMING,
    ) -> None:
        """Initialize the paperless-ngx API client."""
        base_url = url.rstrip("/")
        # The only form of the URL any message or log line may carry: an
        # invalid URL is still shown, and may hold userinfo the parser missed.
        self._display_url = _without_userinfo(base_url)
        self._at_url = f" at {self._display_url}" if self._display_url else ""
        # Whether the transport could be refusing the configured values at
        # all.  A set token and URL that pass the load rules are ones h11
        # sends, so a request refused while this is True has another cause.
        self._configuration_sendable = (
            bool(token) and _is_visible_ascii(token) and _is_visible_ascii(base_url)
        )
        try:
            parsed = httpx2.URL(base_url)
            if parsed.userinfo:
                # httpx2 would turn the userinfo into a Basic Authorization
                # header that replaces the token, and name the URL in its own
                # request log line.  Refused, with the credential-free form.
                msg = (
                    f"Paperless URL {self._display_url} carries a user name or "
                    "password; remove it from paperless.url and put the "
                    "paperless-ngx API token in paperless.token"
                )
                raise PaperlessError(msg) from None
            # Set before the client exists, because its hooks read it.
            self._server_max: int | None = None
            # Accept names the version, which can change between requests, so
            # the request hook sets it and the default headers never mutate.
            self._client = httpx2.Client(
                base_url=base_url,
                headers={"Authorization": f"Token {token}"},
                timeout=_CLIENT_TIMEOUT_SECONDS,
                transport=transport,
                event_hooks={
                    "request": [self._set_accept],
                    "response": [self._learn_api_version],
                },
            )
        except httpx2.InvalidURL:
            # The parser's text can quote part of a password (a "/" in one
            # makes httpx2 read what precedes it as the port), so nothing of
            # it is kept, not even as the cause.
            msg = f"Paperless URL {self._display_url} is not valid"
            raise PaperlessError(msg) from None
        except UnicodeEncodeError:
            # Only header values are encoded here and the token is the only
            # one, so it holds the character; the codec's text quotes it.
            msg = (
                "Paperless API token in paperless.token contains a character "
                "an HTTP header cannot carry"
            )
            raise PaperlessError(msg) from None
        except OSError as exc:
            # The trust anchors are read here, so an SSL_CERT_FILE naming a
            # missing path, or the directory a bind mount leaves when the host
            # file is absent, arrives as an OSError.  Its filename is None, so
            # the message names the two variables that steer the trust store.
            msg = (
                f"Could not build the TLS trust store for Paperless{self._at_url}: "
                f"{describe(exc)}; "
                "check SSL_CERT_FILE and SSL_CERT_DIR"
            )
            raise PaperlessTrustStoreError(
                msg,
                next_step=(
                    "Check that SSL_CERT_FILE names a readable CA bundle file "
                    "and SSL_CERT_DIR a readable directory, or unset them, "
                    "then try again."
                ),
            ) from exc
        self._consume_dir = consume_dir
        self._send_budget = timing.send_budget
        self._clock = timing.clock
        self._sleep = timing.sleep
        self._upload_timeout = timing.upload_timeout
        # Kept only to strike it out of third-party text; see _strike.
        self._token = token

    @property
    def api_version(self) -> int:
        """
        Name the API version the next request will ask for.

        The newest supported version once a server has announced it or a
        higher one, and the oldest otherwise.

        Returns:
            One of ``_SUPPORTED_API_VERSIONS``.

        """
        oldest, newest = _SUPPORTED_API_VERSIONS
        if self._server_max is not None and self._server_max >= newest:
            return newest
        return oldest

    def _set_accept(self, request: httpx2.Request) -> None:
        """
        Name the API version on one outgoing request.

        The request event hook.  The value is built from an int, never from
        anything a server sent.

        Args:
            request: The request about to be sent.

        """
        request.headers["Accept"] = _ACCEPT_TEMPLATE.format(version=self.api_version)

    def _learn_api_version(self, response: httpx2.Response) -> None:
        """
        Remember the highest API version an answer announces.

        The response event hook.  Threads share the client, but this is one
        attribute write, so racing answers from one server write the same
        value.  A 406 to the newest version forgets what was learned, or a
        client that saw it from a since rolled-back server would ask for it,
        and be refused, until restart.
        """
        if _refused_newest_version(response):
            self._server_max = None
            return
        value = response.headers.get("X-Api-Version")
        if value is None or not _API_VERSION_HEADER.fullmatch(value):
            return
        announced = int(value)
        if announced >= _SUPPORTED_API_VERSIONS[0]:
            self._server_max = announced

    def _incompatible_message(self) -> str:
        """
        Build the fixed text for a server that refused the API version.

        No server text is quoted: a 406 body only restates the refusal.

        Returns:
            The message naming the supported versions and the release needed.

        """
        return (
            f"Paperless{self._at_url} does not accept API version "
            "9 or 10; saneless needs paperless-ngx 2.16 or later"
        )

    def upload_document(
        self,
        pdf_path: Path,
        title: str,
        tags: list[int] | None = None,
        correspondent: int | None = None,
    ) -> UploadResult:
        """
        Upload a PDF document to paperless-ngx.

        No document date is sent: paperless-ngx dates the document itself.  A
        failure that proves the request never reached paperless-ngx whole is
        retried until the send budget is spent, and the consume directory, if
        any, then gets the PDF.  Every other failure ends the upload at once
        and copies nothing.

        Args:
            pdf_path: Path to the PDF file to upload.
            title: Document title.
            tags: Optional list of tag IDs to attach.
            correspondent: Optional correspondent ID.

        Returns:
            An ApiDelivery carrying the paperless-ngx task id on success.
            When the send budget is spent and a consume directory is
            configured, a FolderDelivery naming the file the PDF was copied
            to.

        Raises:
            ConfigError: If the configured ``paperless.url`` or
                ``paperless.token`` cannot be sent: fixed text naming the
                setting, with no cause, so no traceback prints the refused
                header value.
            PaperlessUncertainSendError: If the upload may have reached
                paperless-ngx without a usable answer: a failure while reading
                the answer, a 5xx, or a 200 that carries no task id.
            PaperlessIncompatibleError: If paperless-ngx answers 406: it
                accepts none of the supported API versions.
            PaperlessError: If the server rejects or redirects the upload; if
                the send budget is spent with no consume directory; if the
                transport refuses a request built from sendable settings; if
                any other httpx2 error occurs; or if the PDF cannot be read or
                copied into the consume directory.

        """
        data = self._form_fields(title, tags, correspondent)
        try:
            size_bytes = pdf_path.stat().st_size
        except OSError as exc:
            raise _unreadable_pdf(pdf_path, exc) from exc
        timeout = self._upload_timeout(size_bytes)
        deadline = self._clock() + self._send_budget
        attempt = 0
        while True:
            attempt += 1
            try:
                task_id = self._post_document(pdf_path, data, timeout)
            except httpx2.HTTPError as exc:
                match _retry_decision(exc):
                    case _RetryDecision.BEFORE_SEND:
                        remaining = deadline - self._clock()
                        if remaining <= 0:
                            return self._send_budget_spent(pdf_path, exc)
                        # _reason, not describe: library text may quote the
                        # token.
                        logger.warning(
                            "Upload attempt %d failed; retrying for up to %.0fs "
                            "more: %s",
                            attempt,
                            remaining,
                            self._reason(exc),
                        )
                        wait = min(2 ** (attempt - 1), _MAX_BACKOFF_SECONDS)
                        self._sleep(min(wait, remaining))
                    case _RetryDecision.AFTER_SEND:
                        # The body may have been read: a resend or a copy
                        # could make a second document of this scan.
                        msg = self._uncertain_send_message(exc, size_bytes, timeout)
                        raise PaperlessUncertainSendError(msg) from self._cause(exc)
                    case _RetryDecision.INCOMPATIBLE:
                        msg = self._incompatible_message()
                        raise PaperlessIncompatibleError(msg) from None
                    case _RetryDecision.REFUSED if isinstance(
                        exc, httpx2.HTTPStatusError
                    ):
                        # A non-2xx that is neither a 5xx nor a 406 would be
                        # answered the same way again: no retry, no fallback.
                        msg = _not_accepted_message(exc.response, self._token)
                        raise PaperlessError(msg) from self._cause(exc)
                    case _RetryDecision.MISCONFIGURED:
                        error = self._unsendable_error(exc, "Uploading to Paperless")
                        # from None, not from exc: the worker logs a failed job
                        # with its traceback, and a chained h11 error would
                        # print the refused header value -- the token -- there.
                        raise error from None
                    case _RetryDecision.UNEXPECTED | _RetryDecision.REFUSED:
                        # REFUSED lands here only for a non-status error, which
                        # _retry_decision never gives it; it is reported as
                        # unexpected rather than dropped.
                        msg = (
                            f"Could not upload to Paperless{self._at_url}: "
                            f"{self._reason(exc)}"
                        )
                        raise PaperlessError(msg) from self._cause(exc)
                    case unreachable:
                        assert_never(unreachable)
            else:
                logger.info("Upload succeeded, task ID: %r", _loggable_task_id(task_id))
                return ApiDelivery(task_id=task_id)

    @staticmethod
    def _form_fields(
        title: str,
        tags: list[int] | None,
        correspondent: int | None,
    ) -> dict[str, str | list[str]]:
        """
        Build the multipart form fields for an upload.

        Args:
            title: Document title.
            tags: Optional tag IDs, submitted as repeated form fields.
            correspondent: Optional correspondent ID.

        Returns:
            The form fields, with absent metadata left out.

        """
        data: dict[str, str | list[str]] = {"title": title}
        if correspondent is not None:
            data["correspondent"] = str(correspondent)
        if tags:
            data["tags"] = [str(tag_id) for tag_id in tags]
        return data

    def _post_document(
        self,
        pdf_path: Path,
        data: dict[str, str | list[str]],
        timeout: httpx2.Timeout,
    ) -> str:
        """
        Make one upload attempt and return the task id Paperless assigned.

        Args:
            pdf_path: Path to the PDF file to upload.
            data: The multipart form fields.
            timeout: The request's timeout, scaled to the PDF's size.

        Any ``httpx2.HTTPError`` propagates for ``upload_document`` to sort.
        A 406 to the newest API version is asked once more, with the oldest.

        Raises:
            PaperlessError: If the PDF cannot be opened.
            PaperlessUncertainSendError: If a 200 body is not a non-empty JSON
                string; the document may be stored.

        """
        response = self._send_document(pdf_path, data, timeout)
        if _refused_newest_version(response):
            response = self._send_document(pdf_path, data, timeout)
        response.raise_for_status()
        try:
            task_id = response.json()
        except ValueError as exc:
            msg = (
                f"Paperless{self._at_url} answered the upload with a body that is "
                f"not JSON: {self._answer_text(response)}{_MAY_HAVE_REACHED}"
            )
            raise PaperlessUncertainSendError(msg) from self._cause(exc)
        # A number, a bool, an object or null would otherwise be polled as text.
        if not isinstance(task_id, str) or not task_id.strip():
            msg = (
                f"Paperless{self._at_url} answered the upload with "
                f"{self._answer_text(response)}, which is not a task ID"
                f"{_MAY_HAVE_REACHED}"
            )
            raise PaperlessUncertainSendError(msg)
        return task_id

    def _send_document(
        self,
        pdf_path: Path,
        data: dict[str, str | list[str]],
        timeout: httpx2.Timeout,
    ) -> httpx2.Response:
        """
        Send the upload request once and return the answer, whatever its status.

        Raises:
            PaperlessError: If the PDF cannot be opened.

        """
        # Only the open is guarded: the request raises httpx2's own types,
        # which upload_document sorts into retries.
        try:
            pdf_file = pdf_path.open("rb")
        except OSError as exc:
            raise _unreadable_pdf(pdf_path, exc) from exc
        with pdf_file as f:
            return self._client.post(
                "/api/documents/post_document/",
                data=data,
                files={"document": (pdf_path.name, f, "application/pdf")},
                timeout=timeout,
            )

    def _answer_text(self, response: httpx2.Response) -> str:
        """
        Quote a 200 upload answer that is not a task id, safely.

        Args:
            response: The answer.

        Returns:
            Its body on one bounded line with the token struck out, or
            ``(empty response body)``.

        """
        return _bounded_line(_strike(response.text, self._token)) or _EMPTY_BODY

    def _reason(self, exc: BaseException) -> str:
        """
        Describe a failure cause in one line, with the configured token struck.

        Every place the client interpolates an exception's text goes through
        here, and so through ``_strike``.
        """
        return _one_line_reason(exc, self._token)

    def _cause(self, exc: BaseException) -> BaseException | None:
        """
        Return ``exc`` as a cause to chain to, or None when it would leak the token.

        The worker and the CLI log failures with their tracebacks, which print
        every chained cause.  So the cause is kept only when its whole
        formatted traceback, every link included, is unchanged by ``_strike``.
        """
        rendered = "".join(traceback.format_exception(exc))
        return exc if _strike(rendered, self._token) == rendered else None

    def _unsendable_error(
        self, exc: httpx2.HTTPError, action: str
    ) -> ConfigError | PaperlessError:
        """
        Build the error for a request that could not be sent at all.

        A ``ConfigError`` only when the configuration can be at fault (a URL
        with no usable scheme, or a value outside the load rules), else a
        ``PaperlessError`` naming the exception's class.  The text is fixed:
        h11's text for a refused Authorization header quotes the token.
        """
        if isinstance(exc, httpx2.UnsupportedProtocol):
            return ConfigError(f"{action}: {_URL_UNUSABLE}")
        if not self._configuration_sendable:
            return ConfigError(f"{action}: {_REQUEST_UNSENDABLE}")
        return PaperlessError(
            f"{action}: the request could not be sent: the HTTP library refused "
            f"it ({type(exc).__name__})"
        )

    def _send_budget_spent(
        self, pdf_path: Path, exc: httpx2.HTTPError
    ) -> FolderDelivery:
        """
        End an upload whose before-send budget ran out.

        Raises:
            PaperlessError: When there is no consume directory, or from the
                copy itself.

        """
        if self._consume_dir is not None:
            return self._fall_back_to_consume_dir(pdf_path, self._consume_dir)
        # A stalled body write and an empty pool did connect.
        failed = (
            "could not connect"
            if isinstance(
                exc, httpx2.ConnectError | httpx2.ConnectTimeout | httpx2.ProxyError
            )
            else "could not deliver the upload"
        )
        msg = (
            f"Upload to Paperless{self._at_url} {failed} for "
            f"{self._send_budget:.0f}s: {self._reason(exc)}"
        )
        raise PaperlessError(msg) from self._cause(exc)

    def _uncertain_send_message(
        self, exc: httpx2.HTTPError, size_bytes: int, timeout: httpx2.Timeout
    ) -> str:
        """
        Say why an upload may have reached paperless-ngx without an answer.

        A read timeout names the time allowed and the file size, since a
        larger file is given longer.
        """
        reason = self._reason(exc)
        if isinstance(exc, httpx2.ReadTimeout) and timeout.read is not None:
            return (
                f"Upload to Paperless{self._at_url} got no answer within "
                f"{timeout.read:.0f}s, the time allowed for a "
                f"{size_bytes / 1_000_000:.1f} MB PDF ({reason}){_MAY_HAVE_REACHED}"
            )
        return (
            f"Upload to Paperless{self._at_url} got no usable answer: "
            f"{reason}{_MAY_HAVE_REACHED}"
        )

    def _fall_back_to_consume_dir(
        self, pdf_path: Path, dest_dir: Path
    ) -> FolderDelivery:
        """
        Copy the PDF into the consume directory after the upload did not land.

        The directory is never created.  A missing one almost always means
        the paperless-ngx volume is not mounted, and a directory made in its
        place is one nothing watches.

        Raises:
            PaperlessError: If the directory is missing, is not a directory or
                cannot be examined, or if the copy fails.

        """
        try:
            mode = dest_dir.stat().st_mode
        except FileNotFoundError, NotADirectoryError:
            msg = (
                f"consume directory {dest_dir} does not exist — "
                "is the paperless-ngx volume mounted?"
            )
            raise PaperlessError(msg) from None
        except OSError as exc:
            # Permission refused on the path, or an I/O error: the directory
            # may well exist, so "not mounted" would be the wrong lead.
            msg = f"consume directory {dest_dir} cannot be read: {describe(exc)}"
            raise PaperlessError(msg) from exc
        if not stat.S_ISDIR(mode):
            msg = (
                f"consume directory {dest_dir} is not a directory; set "
                "paperless.consume_dir to the folder paperless-ngx consumes from"
            )
            raise PaperlessError(msg)
        dest = dest_dir / pdf_path.name
        try:
            self._deliver_to_consume_dir(pdf_path, dest_dir, dest)
        except OSError as exc:
            msg = (
                f"Could not copy the PDF to the consume directory {dest_dir}: "
                f"{describe(exc)}"
            )
            raise PaperlessError(msg) from exc
        logger.warning("Upload failed; copied PDF to %s", dest)
        return FolderDelivery(path=dest)

    @staticmethod
    def _deliver_to_consume_dir(pdf_path: Path, dest_dir: Path, dest: Path) -> None:
        """
        Hand a whole PDF to the consume directory, never a partial one.

        paperless-ngx acts on whatever appears in the consume directory, so
        the bytes go to a hidden staging file, are fsynced, and only then
        take their final name in one rename.  The directory is not fsynced:
        it is a handoff, not a system of record.

        * The staging ``.part`` file is made exclusively by ``mkstemp``, so
          nothing planted at a predictable name can be written through;
          paperless-ngx 2.x and 3.x both leave it alone.
        * It lives inside the consume directory, because a rename across
          filesystems (a Docker volume, a network share) fails with EXDEV.
        * The rename is ``Path.replace``: ``Path.rename`` refuses an existing
          destination on Windows.

        Raises:
            OSError: Whatever the copy or the rename raised, after the staging
                file is removed.

        """
        # Owner-only and exclusive under any umask: the consume folder is
        # often shared with a group.
        descriptor, staged_name = tempfile.mkstemp(
            dir=dest_dir, prefix=f".{pdf_path.name}.", suffix=".part"
        )
        staged = Path(staged_name)
        try:
            with (
                os.fdopen(descriptor, "wb") as staged_file,
                pdf_path.open("rb") as source,
            ):
                # 0644 so paperless-ngx can read the copy whatever UID it runs
                # as, set before the first byte is written.  A filesystem
                # without Unix modes refuses it, which must not fail the handoff.
                try:
                    os.fchmod(staged_file.fileno(), 0o644)
                except OSError as exc:
                    if not refused_mode_change(exc):
                        raise
                    logger.debug(
                        "Not setting the consume copy's mode: %s", exc.strerror
                    )
                shutil.copyfileobj(source, staged_file)
                staged_file.flush()
                os.fsync(staged_file.fileno())
            staged.replace(dest)
        except OSError:
            staged.unlink(missing_ok=True)
            raise

    def poll_task(self, task_id: str, *, timeout: float) -> TaskOutcome:
        """
        Poll the task endpoint with exponential backoff until the task finishes.

        A 200 without our task means it is not visible yet, and a 5xx, a 429
        or a request error may clear: each is retried until the deadline.
        Any other non-200 ends the poll at once, so a revoked token is
        reported as what it is rather than as a timeout.  paperless-ngx
        accepted the upload before the poll began, so every failure from
        here on is "received, not confirmed", never a failed upload that
        would invite a second scan.

        Args:
            task_id: Task UUID returned from upload.
            timeout: Maximum seconds to wait, request time included, measured
                on the client's clock.  There is no default: callers pass
                ``output.paperless_task_timeout``.

        Returns:
            ``TaskFiled`` when the task reached SUCCESS, or ``TaskDuplicate``
            when paperless-ngx refused it because it already holds the file:
            a document delivered, so an answer rather than an error.

        Raises:
            PaperlessUnconfirmedError: If the task ends FAILURE for any other
                reason or REVOKED, or a poll gets a non-200 that is not
                retried, or a 200 whose body is not JSON.  Server text in the
                message has the token struck out.
            PaperlessTimeoutError: A ``PaperlessUnconfirmedError``, if the
                deadline passes first.  The message names the task id, and the
                last retried error when there was one.

        """
        deadline = self._clock() + timeout
        delay = 0.5
        last_error: str | None = None
        last_cause: BaseException | None = None

        while True:
            try:
                response = self._client.get(
                    "/api/tasks/",
                    params={"task_id": task_id},
                )
            # RequestError rather than TransportError, so a DecodingError (a
            # corrupt compressed body) cannot fail an upload already accepted.
            except httpx2.RequestError as exc:
                last_error = self._reason(exc)
                last_cause = self._cause(exc)
                logger.warning(
                    "Polling task %r failed, retrying until the deadline: %s",
                    _loggable_task_id(task_id),
                    last_error,
                )
            else:
                answer = self._finished_task(task_id, response)
                if isinstance(answer, _PollTransient):
                    last_error = answer.description
                    last_cause = None
                    logger.warning(
                        "Polling task %r was answered %s; retrying until the deadline",
                        _loggable_task_id(task_id),
                        last_error,
                    )
                elif answer is not None:
                    return answer
                else:
                    # A timeout after an answer names no stale error.
                    last_error = None
                    last_cause = None

            # Every path through the loop reaches this check, so the poll
            # cannot outlive its deadline.
            remaining = deadline - self._clock()
            if remaining <= 0:
                shown = _loggable_task_id(task_id)
                logger.warning("Task %r did not finish within %ss", shown, timeout)
                msg = f"Paperless task {shown} did not finish within {timeout}s"
                if last_error is None:
                    raise PaperlessTimeoutError(msg)
                msg = f"{msg}; last error: {last_error}"
                raise PaperlessTimeoutError(msg) from last_cause

            # Clamped, so a sub-second timeout costs what it says.
            self._sleep(min(delay, remaining))
            delay = min(delay * 2, _MAX_POLL_DELAY_SECONDS)

    def _finished_task(
        self, task_id: str, response: httpx2.Response
    ) -> TaskOutcome | _PollTransient | None:
        """
        Read one task poll response.

        Args:
            task_id: The task being polled, for the selection and the
                messages.
            response: The response to ``GET /api/tasks/``.

        Returns:
            ``TaskFiled`` when the task reached SUCCESS; ``TaskDuplicate``
            when it ended FAILURE as a duplicate; a ``_PollTransient`` for a
            5xx or a 429, which may clear; None when the task is not visible
            yet or has not reached a terminal status.  The caller keeps
            polling on either of the last two.

        Raises:
            PaperlessUnconfirmedError: If the response is a 406 or any other
                non-200, if its body is not JSON, or if the task ended
                REVOKED or FAILURE other than as a duplicate.  Every message
                is one line.

        """
        shown = _loggable_task_id(task_id)
        if response.is_server_error or response.status_code == 429:
            return _PollTransient(
                f"{_status_text(response, self._token)}: "
                f"{_render_error_body(response, self._token)}"
            )
        if response.status_code == 406:
            # The 406 body only restates the refusal.
            msg = f"Could not confirm Paperless task {shown}: "
            raise PaperlessUnconfirmedError(msg + self._incompatible_message())
        if response.status_code != 200:
            msg = (
                f"Paperless task poll failed ({_status_text(response, self._token)}): "
                f"{_render_error_body(response, self._token)}"
            )
            raise PaperlessUnconfirmedError(msg)

        try:
            payload = response.json()
        except ValueError as exc:
            msg = (
                f"Paperless{self._at_url} returned a task response that is "
                f"not JSON: {self._reason(exc)}"
            )
            raise PaperlessUnconfirmedError(msg) from self._cause(exc)

        task = _extract_task(payload, task_id)
        if task is None:
            return None
        status = _task_status(task)
        if status == "SUCCESS":
            logger.info("Task %r completed: %s", shown, status)
            return TaskFiled(task=task)
        if status == "REVOKED":
            msg = (
                f"paperless-ngx cancelled task {shown} before it finished; "
                "the document was not filed"
            )
            raise PaperlessUnconfirmedError(msg)
        if status in _TERMINAL_STATUSES:
            full_failure = " ".join(
                _strike(_failure_message(task), self._token).split()
            )
            # The duplicate check and its id read the whole text, so a
            # duplicate named past the cut below is not lost.
            if _is_duplicate_failure(task, full_failure):
                duplicate = _duplicate_of(task, full_failure)
                logger.info(
                    "Task %r was refused as a duplicate of document %s",
                    shown,
                    duplicate.document_id,
                )
                return duplicate
            # A failure result can embed a whole OCR or consumer traceback.
            failure = _bounded_line(full_failure) or _NO_FAILURE_MESSAGE
            msg = f"Paperless task {shown} ended {status}: {failure}"
            raise PaperlessUnconfirmedError(msg)
        return None

    def probe_connection(
        self, *, timeout: httpx2.Timeout | None = None
    ) -> ConnectionProbe:
        """
        Probe paperless-ngx and report which ``ConnectionStatus`` occurred.

        CONNECTED means a 2xx and nothing else.  A 3xx is REDIRECTED and is
        not followed, since following it would hide a wrong paperless.url; a
        406 is INCOMPATIBLE and is not retried with another version.  A
        request the HTTP library would not send is MISCONFIGURED when the
        configuration can be at fault, since nothing reached the network.

        Args:
            timeout: The per-request budget, or None for the client-wide
                default.  Callers pass a short one, so an unplugged host is
                found in seconds.

        Returns:
            The outcome, with the redirect's target and kind when there was
            one.

        """
        try:
            response = self._client.get(
                "/api/tags/",
                params={"page_size": 1},
                timeout=timeout if timeout is not None else httpx2.USE_CLIENT_DEFAULT,
            )
        # Both refusals are TransportError subclasses, so they are caught
        # first.  Neither's text is logged: h11 quotes a refused header value,
        # which for the Authorization header is the token.
        except httpx2.UnsupportedProtocol:
            logger.warning("Paperless connection test: %s", _URL_UNUSABLE)
            return ConnectionProbe(ConnectionStatus.MISCONFIGURED)
        except httpx2.LocalProtocolError:
            return self._refused_probe()
        except httpx2.TransportError:
            # Also the base of ConnectTimeout and ReadTimeout, so an expired
            # budget is UNREACHABLE too.
            logger.warning("Paperless is unreachable")
            return ConnectionProbe(ConnectionStatus.UNREACHABLE)
        return self._answer_probe(response)

    def _refused_probe(self) -> ConnectionProbe:
        """
        Classify a connection test h11 refused to send, and log it.

        MISCONFIGURED only when the configuration can be at fault, by the
        rule ``_unsendable_error`` applies; otherwise UNREACHABLE.
        """
        if not self._configuration_sendable:
            logger.warning("Paperless connection test: %s", _REQUEST_UNSENDABLE)
            return ConnectionProbe(ConnectionStatus.MISCONFIGURED)
        logger.warning(
            "Paperless connection test: the HTTP library refused the "
            "request (LocalProtocolError)"
        )
        return ConnectionProbe(ConnectionStatus.UNREACHABLE)

    def _answer_probe(self, response: httpx2.Response) -> ConnectionProbe:
        """
        Classify the answer paperless-ngx, or something in front of it, gave.

        Args:
            response: The answer to the connection test.

        Returns:
            The outcome that answer means.

        """
        if response.is_redirect:
            return self._redirect_probe(response)
        if response.is_success:
            status = ConnectionStatus.CONNECTED
        elif response.status_code in (401, 403):
            status = ConnectionStatus.TOKEN_REJECTED
        elif response.status_code == 404:
            status = ConnectionStatus.NOT_FOUND
        elif response.status_code == 406:
            status = ConnectionStatus.INCOMPATIBLE
        else:
            logger.warning("Unexpected paperless status %s", response.status_code)
            status = ConnectionStatus.SERVER_ERROR
        return ConnectionProbe(status)

    def _redirect_probe(self, response: httpx2.Response) -> ConnectionProbe:
        """
        Describe a redirect answer to the connection test, and log it.

        Args:
            response: The 3xx answer to the connection test.

        Returns:
            A REDIRECTED probe with the sanitised target, if the answer named
            one, and whether only the scheme changed.

        """
        status = _status_text(response, self._token)
        shown, target = _redirect_target(response, self._token)
        if not shown:
            logger.warning(
                "Paperless answered %s with no redirect target; check paperless.url",
                status,
            )
            return ConnectionProbe(ConnectionStatus.REDIRECTED)
        logger.warning(
            "Paperless answered %s from a different address: %s; check paperless.url",
            status,
            shown,
        )
        return ConnectionProbe(
            ConnectionStatus.REDIRECTED,
            redirect_target=shown,
            https_upgrade=(
                target is not None and self._is_https_upgrade(response.url, target)
            ),
        )

    def _is_https_upgrade(self, sent: httpx2.URL, target: httpx2.URL) -> bool:
        """
        Report whether a redirect changed only ``http`` to ``https``.

        The target may keep the request's path or go to the configured base
        path, where many proxies send every plain HTTP request.  httpx2 drops
        a scheme's default port, so 80 and 443 compare as the same port.
        """
        return (
            sent.scheme == "http"
            and target.scheme == "https"
            and sent.host == target.host
            and sent.port == target.port
            and target.path in {sent.path, self._client.base_url.path}
        )

    def test_connection(
        self, *, timeout: httpx2.Timeout | None = None
    ) -> ConnectionStatus:
        """
        Probe paperless-ngx and report only the outcome.

        ``probe_connection`` with the redirect details dropped, for the web
        API, which serialises the outcome and nothing else.

        Args:
            timeout: The per-request budget, or None for the client-wide
                default.

        Returns:
            A ConnectionStatus member.  Its values are the public JSON
            contract in ``docs/reference/web-api.md`` and may not be renamed.

        """
        return self.probe_connection(timeout=timeout).status

    def get_tags(
        self, *, timeout: float | httpx2.Timeout | None = None
    ) -> list[dict[str, object]]:
        """
        Fetch all tags from paperless-ngx.

        Args:
            timeout: The budget for every page request, in seconds or as an
                ``httpx2.Timeout``, or None for the client-wide default.

        Returns:
            List of tag dicts with at least 'id' and 'name' keys.

        Raises:
            ConfigError: If the configured ``paperless.url`` or
                ``paperless.token`` cannot be sent; fixed text, no cause.
            PaperlessIncompatibleError: If paperless-ngx answers 406: it
                accepts none of the supported API versions.
            PaperlessError: If any page request fails otherwise or a body has
                the wrong shape.

        """
        return self._fetch_collection("/api/tags/", "tags", timeout=timeout)

    def get_correspondents(
        self, *, timeout: float | httpx2.Timeout | None = None
    ) -> list[dict[str, object]]:
        """
        Fetch all correspondents from paperless-ngx.

        Args:
            timeout: The budget for every page request, in seconds or as an
                ``httpx2.Timeout``, or None for the client-wide default.

        Returns:
            List of correspondent dicts with at least 'id' and 'name' keys.

        Raises:
            ConfigError: If the configured ``paperless.url`` or
                ``paperless.token`` cannot be sent; fixed text, no cause.
            PaperlessIncompatibleError: If paperless-ngx answers 406: it
                accepts none of the supported API versions.
            PaperlessError: If any page request fails otherwise or a body has
                the wrong shape.

        """
        return self._fetch_collection(
            "/api/correspondents/", "correspondents", timeout=timeout
        )

    def _fetch_collection(
        self,
        path: str,
        noun: str,
        *,
        timeout: float | httpx2.Timeout | None = None,
    ) -> list[dict[str, object]]:
        """
        Fetch every page of one metadata collection.

        Pages are requested by number on the configured base URL.  The
        ``next`` link is read only as "there is another page" and never
        requested: behind a misconfigured proxy it names the wrong host, and
        following it would send the token there.

        A proxy or server that ignores ``page`` answers page 1 every time
        with a non-null ``next``, so a repeated page fails the fetch, as does
        asking for more pages than ``count`` (or ``_METADATA_MAX_PAGES``)
        allows.

        Raises:
            ConfigError: If the configured URL or token cannot be sent.
            PaperlessIncompatibleError: If paperless-ngx answers 406.
            PaperlessError: If a request fails otherwise, a body has the
                wrong shape, or the server does not make progress.

        """
        prefix = f"Could not fetch {noun} from Paperless{self._at_url}"
        results: list[dict[str, object]] = []
        previous: object = None
        page_limit = _METADATA_MAX_PAGES
        page = 1
        while True:
            data = self._fetch_page(path, page, prefix, timeout=timeout)
            if not isinstance(data, dict):
                msg = f"{prefix}: the response was not an object"
                raise PaperlessError(msg)
            batch = _metadata_items(data.get("results"), prefix, page)
            if not batch:
                return results
            if batch == previous:
                msg = (
                    f"{prefix}: page {page} repeated page {page - 1}; the server "
                    "or a proxy in front of it may be ignoring the page parameter"
                )
                raise PaperlessError(msg)
            results.extend(batch)
            if not data.get("next"):
                return results
            count = _usable_count(data)
            if count is not None:
                if len(results) >= count:
                    return results
                if page == 1:
                    page_limit = min(page_limit, math.ceil(count / len(batch)) + 1)
            if page >= page_limit:
                msg = (
                    f"{prefix}: the server still named a next page after "
                    f"{page} pages; it may be ignoring the page parameter"
                )
                raise PaperlessError(msg)
            previous = batch
            page += 1

    def _fetch_page(
        self,
        path: str,
        page: int,
        prefix: str,
        *,
        timeout: float | httpx2.Timeout | None = None,
    ) -> object:
        """
        Request one metadata page and return its decoded JSON body.

        A 406 to the newest API version is asked once more, as
        ``_post_document`` does.

        Raises:
            ConfigError: If the configured URL or token cannot be sent.
            PaperlessIncompatibleError: If paperless-ngx answers 406.
            PaperlessError: If the request fails otherwise or the body is not
                JSON.

        """

        def _get() -> httpx2.Response:
            return self._client.get(
                path,
                params={"page": page, "page_size": _METADATA_PAGE_SIZE},
                timeout=timeout if timeout is not None else httpx2.USE_CLIENT_DEFAULT,
            )

        try:
            response = _get()
            if _refused_newest_version(response):
                # The response hook has forgotten the stale version.
                response = _get()
            response.raise_for_status()
        except httpx2.HTTPError as exc:
            decision = _retry_decision(exc)
            if decision is _RetryDecision.MISCONFIGURED:
                # A chained h11 error would print the refused token.
                raise self._unsendable_error(exc, prefix) from None
            if decision is _RetryDecision.INCOMPATIBLE:
                # The 406 body only restates the refusal.
                raise PaperlessIncompatibleError(self._incompatible_message()) from None
            msg = f"{prefix}: {self._reason(exc)}"
            raise PaperlessError(msg) from self._cause(exc)
        try:
            return response.json()
        except ValueError as exc:
            msg = f"{prefix}: {self._reason(exc)}"
            raise PaperlessError(msg) from self._cause(exc)

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()
