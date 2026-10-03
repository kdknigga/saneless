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

    Handles document uploads with metadata, task polling with
    exponential backoff, and connection testing.  The construction and
    upload path is a module boundary: whatever goes wrong there leaves as a
    ``PaperlessError`` naming the configured base URL and the original text
    with the token struck out, chained to its cause unless some link of that
    cause's chain quotes the token (see ``_cause``) -- or, for a request that
    could not be sent at all, with fixed text and no cause: a ``ConfigError``
    when the configuration is at fault, else a ``PaperlessError``.  A URL or
    token the constructor refuses is a ``PaperlessError`` with fixed text and
    no cause.

    ``_retry_decision`` sorts every httpx2 error by whether the upload can
    have reached paperless-ngx, and upload failures fall into four groups:

    * **Retried before send**, with waits that double from 1 s up to 5 s, for
      the send budget (60 s unless ``timing`` says otherwise) in total: a
      refused or timed-out connection, no
      free connection in the pool, a proxy that refused the tunnel
      (ProxyError), and a body write that stalled (WriteTimeout).  None of
      them can have delivered the whole body.
    * **After send**, never resent and never copied: a failure while reading
      the answer -- ReadTimeout, ReadError, RemoteProtocolError (a proxy in
      front of paperless-ngx closing the connection), CloseError and the
      rest -- any 5xx, and a 200 whose body is not JSON or carries no task id.
      paperless-ngx may already hold the document, so each is a
      ``PaperlessUncertainSendError``.
    * **Fail fast**, with no further attempt: a 4xx rejection, any other
      non-2xx that is not a server error (a redirect, which names its target
      so ``paperless.url`` can be corrected), and any other ``httpx2.HTTPError``.
      A 406 is a server that does not accept API version 9 or 10, reported as
      a ``PaperlessIncompatibleError``; it refuses before it reads the upload,
      so nothing was stored.  A 406 to a request that asked for 10 is first
      asked once more with 9, since the server that announced 10 may have
      been rolled back.
    * **Not sendable**, with no further attempt: a ``paperless.url`` that is
      unset or has no usable scheme (``httpx2.UnsupportedProtocol``), and a
      request the transport refused to put on the wire
      (``httpx2.LocalProtocolError``, which is what h11 raises for a token
      with edge whitespace or a control character).  Both are TransportErrors
      that no retry can fix, and the second quotes the token in its text, so
      the message is fixed and never chained.  It is a ``ConfigError`` when
      the configured URL or token is at fault, and a ``PaperlessError``
      naming only the exception's class when they pass the load rules and so
      cannot be (see ``_unsendable_error``).

    When a consume directory is configured it is the fallback only once the
    before-send budget is spent: a scan must never be lost to an outage.
    Nothing else is copied.  An after-send failure may already be a document,
    so a copy could make it two; a 4xx, a 406, a redirect and a
    misconfiguration are final, and a misconfiguration would otherwise send
    every scan to the folder without its metadata.  The caller keeps the PDF
    instead.

    A request is resent only when it provably did not reach paperless-ngx, so
    one scan cannot become two documents through a retry.

    Args:
        url: Base URL of the paperless-ngx instance.
        token: API authentication token.  It is sent only in the
            ``Authorization`` header and never interpolated into a message;
            the client keeps it only to strike it out of library text it
            quotes.  It is the only credential sent: a ``url`` carrying a
            user name or password is refused, never sent in its place.
        consume_dir: Optional fallback directory for an upload whose
            before-send budget ran out; None disables the fallback copy.
        transport: The httpx2 transport requests go through, handed straight
            to ``httpx2.Client(transport=...)``. None uses httpx2's default
            network transport. This is the injection seam for tests (an
            ``httpx2.MockTransport``) and for custom transports.
        timing: The upload's send budget, the clock and sleep it runs on, and
            how its timeout is built; see ``PaperlessTiming``.

    Raises:
        PaperlessError: If ``url`` is not a valid URL (``httpx2.InvalidURL``)
            or carries a user name or password, or if ``token`` holds a
            character an HTTP header cannot carry -- each with fixed text and
            no chained cause, since the library's text can quote a password or
            the token -- or if the TLS trust store named by ``SSL_CERT_FILE``
            or ``SSL_CERT_DIR`` cannot be read.

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
        # The only form of the URL any message or log line may carry.  A user
        # name or password is refused below, but an invalid URL is still
        # shown, and it may hold one the parser could not read.
        self._display_url = _without_userinfo(base_url)
        # " at <url>" for a message, or nothing when no address is set: an
        # unset paperless.url would otherwise read "Paperless at : ...".
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
            # The highest API version the server has announced, or None until
            # an answer names one.  Set before the client exists, because its
            # hooks read it.
            self._server_max: int | None = None
            # Accept is not a default header: it names the version, which can
            # change once an answer announces a higher one, so the request
            # hook sets it on each request and the defaults are never mutated.
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
            # The parser's own text can quote part of a password: a "/" in
            # one makes httpx2 read what comes before it as the port, and
            # its complaint names that port.  Nothing of it is kept, not even
            # as the chained cause.
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
            # The TLS trust anchors are read while the client is being built,
            # so a SSL_CERT_FILE naming a path that does not exist arrives
            # here as FileNotFoundError, and one naming a directory -- which
            # is what a docker-compose bind mount leaves behind when the host
            # file is absent -- arrives as IsADirectoryError.  Neither may
            # leave this boundary as a raw traceback.  The offending path is
            # not recoverable from the exception, whose filename attribute is
            # None, so the message names the two variables that steer the
            # trust store and can be corrected.
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

        It is 10 once a server has announced 10 or higher, and 9 otherwise:
        before any answer, when no answer named a version, and when the
        latest usable announcement was 9.  A server announcing more than 10
        still allows 10, the highest this client knows; speaking a later
        version is deferred until saneless is written against it.

        Returns:
            One of the supported versions, 9 or 10.

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

        The response event hook, which runs for every answer, including one
        that later raises from ``raise_for_status``, and adds no request.
        Only a value of one to three ASCII digits naming at least 9 is kept;
        anything else, and an answer without the header, leaves what was
        learned as it is.  The value is stored as an int and never quoted.
        The web client is shared by several threads, and this is one
        attribute write, so racing answers from one server can only write the
        same value.

        A 406 to a request that asked for 10 forgets what was learned, so the
        next request asks for 9 again.  Without it a client that once saw 10
        announced -- by a paperless-ngx since rolled back to 2.x, or by another
        server at the same address -- would ask for 10 until the process
        restarted, and every request would be refused.

        Args:
            response: The answer just received.

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

        Builds multipart form data with title and optional metadata.
        Tags are submitted as repeated form fields.  No document date is
        sent: paperless-ngx dates the document itself.  A failure that proves
        the request never reached paperless-ngx whole is retried, with waits
        doubling from 1 s up to 5 s, until the send budget is spent; the
        consume directory, when one is configured, then gets the PDF instead.
        Every other failure ends the upload at once and copies nothing; see
        the class docstring for how each is reported.

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
            ConfigError: If the request cannot be sent from the configured
                ``paperless.url`` and ``paperless.token``: the URL is unset or
                has no usable scheme, or the transport refused a token or URL
                outside the load rules.  The message is fixed text starting
                ``Uploading to Paperless:`` and naming the setting, and the
                error carries no cause, so no traceback can print the refused
                header value.  Nothing is retried or copied.
            PaperlessUncertainSendError: If the upload may have reached
                paperless-ngx without a usable answer: a failure while reading
                the answer, a 5xx, or a 200 whose body is not JSON or is not a
                non-empty string task id.  A read timeout names the time
                allowed and the file size.  Nothing is resent or copied.
            PaperlessIncompatibleError: If paperless-ngx answers 406: it does
                not accept API version 9 or 10.  Fixed text, no cause, nothing
                retried or copied.
            PaperlessError: If the server rejects the upload with a 4xx
                (``Paperless rejected the upload (<status> <reason>): <line>``)
                or answers with a redirect (``Paperless redirected the upload
                (<status> <reason>) to <location>; check paperless.url``);
                if the send budget is spent and no consume directory is
                configured (``could not connect for <N>s``, or ``could not
                deliver the upload for <N>s`` when the last failure came after
                connecting); if the transport
                refuses to send a request built from a sendable URL and token
                (fixed text, no cause, nothing retried or copied); if any
                other httpx2 error occurs; if the PDF cannot be read; or if
                copying into the consume directory fails.  Any library or
                server text it quotes has the token struck out, and it is
                chained to its cause only when no link of the cause's chain
                quotes the token.

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
                        # A 4xx rejection and a redirect (1xx and 3xx too:
                        # raise_for_status refuses every non-2xx) would only be
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

        Returns:
            The task id: a non-empty string, returned as sent.

        Raises:
            PaperlessError: If the PDF cannot be opened.
            PaperlessUncertainSendError: If a 200 body is not JSON, or is not
                a non-empty string.  paperless-ngx answered 200, so the
                document may be stored; the message names what came back.

        Any ``httpx2.HTTPError`` from the request or from ``raise_for_status``
        propagates: ``upload_document`` decides which of those to retry.

        A 406 to a request that asked for API version 10 is asked once more:
        the response hook has forgotten the 10, so the second request asks for
        9 (see ``_refused_newest_version`` for why that cannot duplicate).

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
        # Only a non-empty string is a task id.  A number, a bool, an object
        # or null would otherwise become a string and be polled as one.
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

        Args:
            pdf_path: Path to the PDF file to upload.
            data: The multipart form fields.
            timeout: The request's timeout, scaled to the PDF's size.

        Returns:
            The answer, not yet checked.

        Raises:
            PaperlessError: If the PDF cannot be opened.

        """
        # Only the open is guarded: an OSError here is the PDF itself, while
        # the request below raises httpx2's own types, which upload_document
        # sorts into retries.
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

        Args:
            exc: The cause.

        Returns:
            The one-line reason, with the token replaced by ``***``.

        """
        return _one_line_reason(exc, self._token)

    def _cause(self, exc: BaseException) -> BaseException | None:
        """
        Return ``exc`` as a cause to chain to, or None when it would leak the token.

        A ``PaperlessError`` whose message has the token struck out would
        still print it if it were chained to library text quoting it: the
        worker logs a failed job with ``logger.exception`` and the CLI logs a
        failure with ``exc_info``, and both print every chained cause and
        context.  So the cause is kept only when its whole formatted
        traceback -- exactly what a log would print, every link of its own
        chain included -- is unchanged by ``_strike``.  Otherwise the error
        is raised ``from None``: its message already carries the struck
        reason, and no traceback can put the token back.

        Args:
            exc: The cause the caller would chain to.

        Returns:
            ``exc`` when no link of its chain quotes the token, else None.

        """
        rendered = "".join(traceback.format_exception(exc))
        return exc if _strike(rendered, self._token) == rendered else None

    def _unsendable_error(
        self, exc: httpx2.HTTPError, action: str
    ) -> ConfigError | PaperlessError:
        """
        Build the error for a request that could not be sent at all.

        The configuration is blamed only when it can be at fault: a URL with
        no usable scheme, or a token or URL outside the load rules, which a
        client built from loaded settings never has.  Any other refusal --
        h11 finding a body longer than its declared length, say -- is not
        something ``paperless.url`` or ``paperless.token`` can fix, so it is a
        ``PaperlessError`` that names only the exception's class.  Either way
        the text is fixed: h11's own text for a refused header value quotes
        it, and for the Authorization header that is the token.

        Args:
            exc: The error, used only for its type.  Its text is never
                included.
            action: What saneless was doing, e.g. ``Uploading to Paperless``.

        Returns:
            ``<action>: <fixed problem>``, one line, as a ``ConfigError`` when
            the configuration is at fault and a ``PaperlessError`` otherwise.

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

        Args:
            pdf_path: The PDF that could not be sent.
            exc: The last attempt's error.

        Returns:
            The consume-folder copy, when a consume directory is configured.

        Raises:
            PaperlessError: Naming the budget and the last error when there is
                no consume directory, or from the copy itself.

        """
        if self._consume_dir is not None:
            return self._fall_back_to_consume_dir(pdf_path, self._consume_dir)
        # Named by the last failure: a refused or timed-out connection never
        # connected, but a stalled body write did connect, and no free pooled
        # connection is not a connect failure either.
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

        Args:
            exc: The after-send error.
            size_bytes: The PDF's size.
            timeout: The timeout the request was sent with.

        Returns:
            One line naming the address and the struck reason, ending with the
            warning that the document may already be there.  A read timeout
            names the time allowed and the file size instead, since a larger
            file is given longer.

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
        place is one nothing watches: the copy would sit there unseen.

        Args:
            pdf_path: The PDF that could not be uploaded.
            dest_dir: The configured consume directory.

        Returns:
            A FolderDelivery naming the file the PDF was copied to.

        Raises:
            PaperlessError: If the directory does not exist, is not a
                directory, or cannot be examined -- each worded for its own
                cause -- or, chained to the OSError, if the copy fails.

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

        paperless-ngx watches the consume directory with inotify and acts on
        what appears there, so writing the bytes directly to the final name
        lets it pick up a half-written PDF.  Instead the bytes go to a hidden
        staging file, are flushed and fsynced, and only then take their final
        name in one ``rename(2)``.

        Three details are load-bearing:

        * The staging file is a unique hidden ``.part`` file created
          exclusively by ``mkstemp`` (``O_EXCL``, mode 0600), so
          nothing planted at a predictable name -- a symlink, or a leftover
          from a crash -- can be written through or collide with it.
          paperless-ngx 2.x logs it as an unknown file extension and 3.x
          skips it by extension; neither consumes it.
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
        # Created owner-only and exclusively, so under no umask can anyone
        # else write to it, even for an instant: the consume folder is often
        # shared with a group.
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
                # as; the consume folder's own mode decides who can reach it.
                # A chmod is not subject to the umask, and it is applied
                # before the first byte is written, so the document is never
                # in a file with any other mode, and never under its final
                # name with one. A filesystem without Unix modes refuses it,
                # and that must not fail a handoff that would otherwise save
                # the scan.
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

        Two wire shapes are tolerated. API v9 answers ``GET /api/tasks/``
        with a bare list of tasks whose ``status`` is uppercase and whose
        failure text is a flat ``result`` string.  API v10 paginates the
        list into ``{"count", "next", "previous", "results"}``, spells the
        status in lowercase, and moved the failure text into
        ``result_data["error_message"]``.  The client asks for v10 once the
        server has announced it and for v9 until then (see ``api_version``),
        so either shape can arrive.  In both, only the entry whose
        ``task_id`` equals ours is read; an answer holding only other tasks
        means ours is not visible yet.

        A 200 carrying no task of ours is not an error.  A task is not always
        visible immediately after the upload that created it, so an empty
        list (or an empty ``results``) means "ask again", and polling
        continues until the deadline.

        Nor does an answer that says "later" end the poll: a 5xx, which is
        usually a reverse proxy in front of a restarting paperless-ngx, or a
        429 rate limit.  Neither does a request-level error (a connection
        refused, a reset, a read timeout, a proxy closing the connection, a
        body that cannot be decoded).  Each is logged and remembered, and the
        poll backs off and asks again within the same deadline.  The waits
        double from 0.5 s up to 5 s, and the last is cut to what remains.
        An answered poll clears what was remembered.

        A 401, 403, 404 or 406, and any other non-200, ends the poll at once:
        a revoked token or a moved endpoint should be reported as what it is
        within a second, not as a timeout several minutes later.

        paperless-ngx accepted the upload before the poll began, so it holds
        the document.  Every failure from here on is therefore "received, not
        confirmed", never a plain failed upload that would invite a second
        scan of the same document.

        Args:
            task_id: Task UUID returned from upload.
            timeout: Maximum seconds to wait, measured on the client's clock
                (see ``PaperlessTiming``), which includes request time as
                well as sleep time. There is no default: callers pass
                ``output.paperless_task_timeout``, so the configured value is
                the only source.

        Returns:
            ``TaskFiled`` carrying the task when it reached SUCCESS, or
            ``TaskDuplicate`` when it ended FAILURE because paperless-ngx
            already holds the file: that is a document delivered, not lost,
            so it is an answer rather than an error.  The duplicate names the
            existing document when the answer does (see ``_duplicate_of``).

        Raises:
            PaperlessUnconfirmedError: If the task ends FAILURE for any other
                reason, carrying the message paperless-ngx supplied, or
                REVOKED, saying paperless-ngx cancelled it; if a poll is
                answered 406, in the
                incompatible-version wording; if a poll gets any other
                non-200 but a 5xx or 429, carrying the status code, the
                reason phrase and the body reduced to one bounded line by
                ``_render_error_body``; or if a 200 body is not JSON,
                chained to the ValueError.  The token is struck out of every
                piece of server text in the message.
            PaperlessTimeoutError: A ``PaperlessUnconfirmedError``, if the
                deadline passes before the task reaches a terminal status.
                The message names the task id so the task can be looked up in
                paperless-ngx directly, and, when the last poll got a 5xx, a
                429 or a request error rather than an answer, ends by naming
                it.  A request error is chained, unless its chain quotes the
                token, when there is no cause.

        """
        deadline = self._clock() + timeout
        delay = 0.5
        # What went wrong with the latest poll, on one line with the token
        # struck, and the request error to chain to when it was one.
        last_error: str | None = None
        last_cause: BaseException | None = None

        while True:
            try:
                response = self._client.get(
                    "/api/tasks/",
                    params={"task_id": task_id},
                )
            # RequestError rather than TransportError: DecodingError (a corrupt
            # compressed body) is a request-level failure that is not a
            # transport one, and it must neither escape this boundary as a raw
            # httpx2 type nor fail an upload Paperless already accepted.
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
                    # Paperless answered, so an earlier blip is no longer the
                    # story: a timeout after this names no stale error.
                    last_error = None
                    last_cause = None

            # Every path through the loop body reaches this check -- a
            # transport error included -- so the poll cannot outlive its
            # deadline.
            remaining = deadline - self._clock()
            if remaining <= 0:
                shown = _loggable_task_id(task_id)
                logger.warning("Task %r did not finish within %ss", shown, timeout)
                msg = f"Paperless task {shown} did not finish within {timeout}s"
                if last_error is None:
                    raise PaperlessTimeoutError(msg)
                msg = f"{msg}; last error: {last_error}"
                raise PaperlessTimeoutError(msg) from last_cause

            # Clamped so the poll never sleeps past its own deadline -- that
            # is what makes a sub-second timeout cost what it says it does
            # rather than the 0.5s first backoff.
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
            # The same fixed text as a refused upload; the 406 body only
            # restates the refusal.
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
            # Bounded like an error body: a failure result can embed a whole
            # OCR or consumer traceback, and this text becomes job.error and
            # the CLI line.
            failure = _bounded_line(full_failure) or _NO_FAILURE_MESSAGE
            msg = f"Paperless task {shown} ended {status}: {failure}"
            raise PaperlessUnconfirmedError(msg)
        return None

    def probe_connection(
        self, *, timeout: httpx2.Timeout | None = None
    ) -> ConnectionProbe:
        """
        Probe paperless-ngx and report which ``ConnectionStatus`` occurred.

        CONNECTED means a 2xx and nothing else.  A 404 says the API is not
        where the configured URL points -- a different thing to fix than a
        500, which says paperless-ngx itself is unwell, and both used to be
        reported as CONNECTED.  A 406 is INCOMPATIBLE: paperless-ngx refused
        the API version, so it is older than 2.16 or newer than this client
        knows.  It is not tried again with another version.

        A 3xx is REDIRECTED, as the upload path treats it: paperless.url
        points at an address paperless-ngx does not answer on, and following
        the redirect would only hide that.  Where it pointed is logged and
        kept on the result for ``doctor``, sanitised, and ``https_upgrade``
        says whether only the scheme changed.  A request the HTTP library
        would not send is MISCONFIGURED when the configuration can be at
        fault, by the rule ``_unsendable_error`` applies to an upload:
        nothing reached the network, so "could not reach" would send the
        operator to look for a fault that is not there.

        The classification is an ordered chain of integer comparisons rather
        than a ``match`` with ``assert_never``, for the same reason
        ``vocabulary.classify_error`` is an ``isinstance`` chain: the input
        is a range of integers, not a closed set of members, so exhaustive
        matching does not apply and a trailing fallback is the correct total
        answer.  ``assert_never`` governs the *message* lookup in
        ``vocabulary.connection_status_message``, which does dispatch on a
        closed set.

        The bound is a per-request override rather than a constructor
        argument, because callers want a shorter budget than an upload's.
        The status strip, ``saneless doctor`` and ``GET /api/paperless/test``
        all pass the short probe budget, so an unplugged host is discovered
        in about two seconds instead of thirty.  The ``except
        httpx2.TransportError`` arm below is the base class of
        ``ConnectTimeout`` and ``ReadTimeout`` as well as ``ConnectError``, so
        a budget that expires lands on UNREACHABLE.

        Args:
            timeout: The per-request budget to send, or None to use the
                client's own 30 s default.

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
        # Both refusals are TransportError subclasses, so they must be caught
        # before it.  Neither reached the network, and neither exception's
        # text is logged: h11's text for a refused header value quotes it,
        # and for the Authorization header that is the token.
        except httpx2.UnsupportedProtocol:
            logger.warning("Paperless connection test: %s", _URL_UNUSABLE)
            return ConnectionProbe(ConnectionStatus.MISCONFIGURED)
        except httpx2.LocalProtocolError:
            return self._refused_probe()
        except httpx2.TransportError:
            # The base class of ConnectError, ConnectTimeout and ReadTimeout.
            # Catching only ConnectError let the timeout siblings escape to
            # routes.py's blanket handler, which answers HTTP 500
            # {"status": "error"} -- none of the outcomes.
            logger.warning("Paperless is unreachable")
            return ConnectionProbe(ConnectionStatus.UNREACHABLE)
        return self._answer_probe(response)

    def _refused_probe(self) -> ConnectionProbe:
        """
        Classify a connection test h11 refused to send, and log it.

        The configuration is blamed only when it can be at fault, by the rule
        ``_unsendable_error`` applies to an upload: settings that pass the
        load rules are ones h11 sends, so a refusal of them has another cause
        and is reported as UNREACHABLE.  The exception's text is never
        logged, because h11's text for a refused header value quotes it.

        Returns:
            MISCONFIGURED when the configuration cannot be sent, otherwise
            UNREACHABLE.

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

        The target may keep the request's path, or go to the root of the
        configured address, which is where many proxies send every plain
        HTTP request.  httpx2 drops a scheme's default port, so ``http`` on
        80 and ``https`` on 443 compare as the same port.

        Args:
            sent: The URL the probe requested.
            target: The resolved redirect target.

        Returns:
            True when the scheme went from http to https and the host, port
            and path are otherwise the same.

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
            timeout: The per-request budget to send, or None to use the
                client's own 30 s default.

        Returns:
            A ConnectionStatus member.  It is a StrEnum, and its values are
            the public JSON contract documented in
            ``docs/reference/web-api.md`` -- ``web/routes.py`` serialises the
            return value straight into a response body, so the values may
            not be renamed without breaking existing clients.

        """
        return self.probe_connection(timeout=timeout).status

    def get_tags(
        self, *, timeout: float | httpx2.Timeout | None = None
    ) -> list[dict[str, object]]:
        """
        Fetch all tags from paperless-ngx.

        Args:
            timeout: The per-request budget, sent on every page request, or
                None to use the client's own 30 s default.  A float bounds
                every phase of the request alike; an ``httpx2.Timeout``
                carries separate connect and read budgets, so a host that
                does not answer at all is given up on sooner than a slow one.
                A check made before a scan starts passes a short one, so a
                paperless-ngx that is down is reported in seconds.

        Returns:
            List of tag dicts with at least 'id' and 'name' keys.

        Raises:
            ConfigError: If the request cannot be sent from the configured
                ``paperless.url`` and ``paperless.token``, with fixed text and
                no cause.  A request refused for another reason is a
                ``PaperlessError`` with fixed text and no cause.
            PaperlessIncompatibleError: If paperless-ngx answers 406: it does
                not accept API version 9 or 10.  Fixed text, no cause.
            PaperlessError: If any page request fails for any other
                ``httpx2.HTTPError`` (a non-2xx included) or a body is not JSON,
                naming the endpoint and base URL and chained to the cause
                unless the cause's chain quotes the token.

        """
        return self._fetch_collection("/api/tags/", "tags", timeout=timeout)

    def get_correspondents(
        self, *, timeout: float | httpx2.Timeout | None = None
    ) -> list[dict[str, object]]:
        """
        Fetch all correspondents from paperless-ngx.

        Args:
            timeout: The per-request budget, sent on every page request, or
                None to use the client's own 30 s default.  A float bounds
                every phase of the request alike; an ``httpx2.Timeout``
                carries separate connect and read budgets, so a host that
                does not answer at all is given up on sooner than a slow one.
                A check made before a scan starts passes a short one, so a
                paperless-ngx that is down is reported in seconds.

        Returns:
            List of correspondent dicts with at least 'id' and 'name' keys.

        Raises:
            ConfigError: If the request cannot be sent from the configured
                ``paperless.url`` and ``paperless.token``, with fixed text and
                no cause.  A request refused for another reason is a
                ``PaperlessError`` with fixed text and no cause.
            PaperlessIncompatibleError: If paperless-ngx answers 406: it does
                not accept API version 9 or 10.  Fixed text, no cause.
            PaperlessError: If any page request fails for any other
                ``httpx2.HTTPError`` (a non-2xx included) or a body is not JSON,
                naming the endpoint and base URL and chained to the cause
                unless the cause's chain quotes the token.

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

        Pages are requested by number, ``?page=N``, on the configured base
        URL until a page's ``next`` is null, a page comes back empty, or the
        items collected reach the server's own ``count``.  The ``next`` link
        itself is only read as "there is another page" and is never
        requested: a server behind a misconfigured proxy builds it
        from the wrong host or scheme, and following it would send the API
        token there.  A redirect is not followed either; it fails the fetch.
        Every paperless-ngx release this client supports paginates tags and
        correspondents, so each page must be an object whose ``results`` is
        a list of objects; a bare list is not a collection.

        The client does not take the server's word alone that it is making
        progress.  A proxy that drops the query string, or a server that
        ignores ``page``, answers page 1 to every request with a non-null
        ``next``; a page identical to the one before it therefore fails the
        fetch.  So does asking for more pages than ``count`` can need (one
        more than ``count`` over the size of the first page), or, with no
        usable ``count``, more than ``_METADATA_MAX_PAGES``.

        This is a module boundary: no httpx2 type and no raw ValueError leaves
        it.  A status error is rendered by ``_one_line_reason`` as status,
        reason and body, so the message stays one line, any library or server
        text has the token struck out by ``_reason``, and a cause is chained
        only through ``_cause``.

        Args:
            path: The collection endpoint, e.g. ``/api/tags/``.
            noun: What the collection holds, for the message.
            timeout: The per-request budget for every page -- seconds, or
                an ``httpx2.Timeout`` with separate connect and read
                budgets -- or None for the client's default.

        Returns:
            The ``results`` of every page in order.

        Raises:
            ConfigError: ``Could not fetch <noun> from Paperless at <url>:
                <fixed problem>``, with no cause, when the request cannot be
                sent from the configured URL and token.  With no URL set the
                `` at <url>`` is left out.  A request refused for any other
                reason is a ``PaperlessError`` of the same shape.
            PaperlessIncompatibleError: If paperless-ngx answers 406: it does
                not accept API version 9 or 10.  Fixed text, no cause.
            PaperlessError: ``Could not fetch <noun> from Paperless at <url>:
                <reason>``, chained to the httpx2 error or the ValueError
                unless its chain quotes the token, or
                unchained when a body or its items have the wrong shape, the
                server repeats a page, or it needs more pages than its
                ``count`` allows.

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

        A 406 to a request that asked for API version 10 is asked once more,
        with 9, as ``_post_document`` does.

        Args:
            path: The collection endpoint, e.g. ``/api/tags/``.
            page: The page number to ask for.
            prefix: The message prefix naming the collection and base URL.
            timeout: The request's budget -- seconds, or an
                ``httpx2.Timeout`` with separate connect and read budgets --
                or None for the client's default.

        Returns:
            The decoded body, whatever its shape.

        Raises:
            ConfigError: ``<prefix>: <fixed problem>``, with no cause, when
                ``_retry_decision`` finds the request could not be sent and
                ``_unsendable_error`` finds the configured URL or token at
                fault; the library's text, which can quote the token, is left
                out.  Otherwise the same fixed-text error is a
                ``PaperlessError``.
            PaperlessIncompatibleError: If paperless-ngx answers 406: it does
                not accept API version 9 or 10.  Fixed text, no cause.
            PaperlessError: ``<prefix>: <reason>``, with the token struck out
                of the reason, chained to any other httpx2 error or the
                ValueError unless that cause's chain quotes the token.

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
                # Asked once more, now for 9: the response hook has forgotten
                # the 10 a server that no longer allows it once announced.
                response = _get()
            response.raise_for_status()
        except httpx2.HTTPError as exc:
            decision = _retry_decision(exc)
            if decision is _RetryDecision.MISCONFIGURED:
                # from None for the reason upload_document gives: a chained
                # h11 error would print the refused token in a traceback.
                raise self._unsendable_error(exc, prefix) from None
            if decision is _RetryDecision.INCOMPATIBLE:
                # The same fixed text as a refused upload; the 406 body only
                # restates the refusal.
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
