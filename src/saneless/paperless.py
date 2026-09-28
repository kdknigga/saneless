"""
Paperless-ngx REST API client with retry, polling, and connection test.

Uploads PDFs with metadata (title, tags, correspondent) but no document
date, which paperless-ngx chooses itself; polls the task endpoint with
exponential backoff until a terminal state and raises when that state is
not success; and probes connections, reporting one of the six
ConnectionStatus outcomes.  Every request names API version 9 or 10, the
highest the server has announced (see ``PaperlessClient.api_version``).
"""

from __future__ import annotations

import logging
import math
import os
import re
import shutil
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
    PaperlessUncertainSendError,
    describe,
)
from .text_safety import neutralise_bounded, neutralise_controls
from .vocabulary import ConnectionStatus

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "ApiDelivery",
    "FolderDelivery",
    "PaperlessClient",
    "PaperlessTiming",
    "UploadResult",
]

logger = logging.getLogger(__name__)

# The API versions this client speaks.  paperless-ngx negotiates via
# ``Accept: application/json; version=N`` and serves its own current default to
# a client that sends none, so every request names one, turning a hidden
# assumption into an explicit contract.  paperless-ngx 2.16 through 2.20 allow
# versions up to 9, and 3.x allows 9 and 10.  A server outside that range --
# anything before 2.16 allows only up to 7 -- answers a version it does not allow
# with 406 Not Acceptable.  The highest version a server allows is announced in
# ``X-Api-Version``, but only on responses to an authenticated request: never on
# a 401, and never on the 406 itself.  So the first request speaks 9, which every
# supported server allows, and its answer says whether 10 may follow.
_SUPPORTED_API_VERSIONS: Final = (9, 10)
_ACCEPT_TEMPLATE: Final = "application/json; version={version}"
# One to three ASCII digits, the whole value: a longer, signed, spaced or
# non-ASCII value is not a version and is never read.  ``[0-9]``, not ``\d``,
# which also matches digits from other scripts that ``int()`` accepts.
_API_VERSION_HEADER: Final = re.compile(r"[0-9]{1,3}")

# paperless-ngx's COMPLETE_STATUSES, normalised to the v9 uppercase spelling.
# REVOKED belongs here: it is an administrative cancellation that will never
# progress, so polling it to the deadline would report a misattributed timeout.
_TERMINAL_STATUSES = frozenset({"SUCCESS", "FAILURE", "REVOKED"})

# Upper bound on how much of an error response body is interpolated into an
# error message.  That message is recorded verbatim in the job store and
# rendered in the web status area and on the terminal, so an
# upstream returning a multi-megabyte body or an HTML error page must not be
# able to flood any of them.  The full body is logged at DEBUG instead.
_MAX_BODY_LINE_CHARS = 200

_EMPTY_BODY = "(empty response body)"

_NO_FAILURE_MESSAGE = "Paperless reported a failure but supplied no message"

# How many tags or correspondents to ask for per page.  A typical install fits
# in a single round trip at this size, and paperless-ngx accepts pages up to
# 100000, so larger collections take a few requests rather than being cut off.
_METADATA_PAGE_SIZE: Final = 1000

# The most pages one metadata fetch asks for when the server gives no usable
# ``count`` to bound it: a million items at the page size above.  A server or
# proxy that ignores ``?page=`` but varies its answer would otherwise keep the
# fetch, and the metadata cache lock it runs under, busy forever.
_METADATA_MAX_PAGES: Final = 1000

# The most characters of a paperless task id that reach a log line or an error
# message. The id is third-party text: paperless-ngx issues UUIDs, but nothing
# stops a misbehaving server or proxy from answering with megabytes or with
# terminal escape sequences, and the message becomes job.error.
_TASK_ID_LOG_LIMIT: Final = 64

_DUPLICATE_HINT = (
    "the document may already be in Paperless; check before scanning again"
)

# How long an upload keeps trying while each failure proves the request never
# reached paperless-ngx whole.  Long enough to ride out a paperless-ngx or
# container restart; after it the consume-folder fallback, if any, takes over.
_SEND_BUDGET_SECONDS: Final = 60.0

# The longest single wait between those attempts.  The waits double from 1 s up
# to this, so a server that comes back is found within a few seconds of it.
_MAX_BACKOFF_SECONDS: Final = 5.0

# The upload's connect timeout.  Shorter than the client-wide 30 s, so a host
# that drops packets still gets several attempts within the send budget.
_UPLOAD_CONNECT_SECONDS: Final = 10.0

# The upload's read timeout: a floor, a rate per MiB of the PDF and a cap.
# paperless-ngx answers the upload only after it has parsed the body, read the
# file into memory and written it to its scratch directory, so a large PDF on a
# slow disk takes longer to answer.  The cap matches paperless-ngx's default
# task timeout.
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
        A 10 s connect timeout and a read timeout of 30 s plus 1 s per MiB,
        capped at 300 s; writes and the pool keep the client-wide 30 s.

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


def _usable_count(page: dict[object, object]) -> int | None:
    """
    Return a paginated page's ``count``, or None when it cannot bound a fetch.

    Only a non-negative integer is a count.  A missing or null value, a
    string, a negative number and a bool (which Python treats as an int)
    are all ignored.

    Args:
        page: The decoded body of one paginated metadata page.

    Returns:
        The total number of items the server says the collection holds, or
        None.

    """
    count = page.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return None
    return count


def _metadata_items(value: object, prefix: str, page: int) -> list[dict[str, object]]:
    """
    Return one metadata page's items, checked to be a list of objects.

    A ``results`` of ``null`` would otherwise raise a TypeError out of the
    client, and a string or an object would be taken apart into its
    characters or keys.

    Args:
        value: The page's items: a bare-list body or a page's ``results``.
        prefix: The message prefix naming the collection and base URL.
        page: The page number, for the message.

    Returns:
        The items, each an object.

    Raises:
        PaperlessError: ``<prefix>: page <N> did not hold a list of
            objects``.

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

    The accepted risk of retrying an upload, set out on ``PaperlessClient``,
    is why this is worth recognising: an upload retried after its response
    was lost usually makes current paperless-ngx store a silent second copy.
    Only with ``CONSUMER_DELETE_DUPLICATES`` is the duplicate reported as a
    failure, and then the user is told the document may already be there,
    rather than being left to scan it again.

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


def _strike(text: str, token: str) -> str:
    """
    Strike the configured token out of text saneless did not write.

    Library text and server text are not under saneless's control.  h11
    refusing a header value quotes it whole, and a reverse proxy or auth
    gateway may answer a refused request by quoting the ``Authorization``
    header.  So every piece of such text the client shows or logs -- an
    exception's text, a response body at any status, a task's failure text,
    a redirect target -- goes through here first, before it is cut to length,
    so no fragment of the token can survive the cut.

    The token is struck both as configured and stripped, since a bytes repr
    spells out edge whitespace and control characters and so no longer
    contains the raw form.  The replacement is unanchored: a credential is
    struck wherever it appears, and over-redacting a diagnostic line costs
    nothing.  A token shorter than two characters is left alone, because
    striking a single character would mangle the text and hide no secret.

    Args:
        text: Third-party text of any shape.
        token: The configured ``paperless.token``.

    Returns:
        The text with every occurrence of the token replaced by ``***``.

    """
    for secret in (token, token.strip()):
        if len(secret) >= 2:
            text = text.replace(secret, "***")
    return text


def _one_line_reason(exc: BaseException, token: str) -> str:
    """
    Describe a failure cause as one line for a ``PaperlessError`` message.

    A CLI or web message must be a single line, but httpx2's own text
    for an ``HTTPStatusError`` is two: ``Server error '503 ...' for url
    '...'`` followed by ``For more information check: <mdn url>``.  A status
    error is therefore rendered as its status, reason phrase and the body
    reduced to one bounded line by ``_render_error_body``; anything else is
    ``describe``, which already collapses whitespace.

    Args:
        exc: The cause.
        token: The configured token, struck out of the text by ``_strike``.

    Returns:
        A non-empty single line with the token struck out.

    """
    if isinstance(exc, httpx2.HTTPStatusError):
        response = exc.response
        return f"{_status_text(response)}: {_render_error_body(response, token)}"
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

    Every upload and metadata-fetch error goes through here, so a change of
    policy for those is a change to this function alone.  An upload is not
    idempotent, so the question it answers is whether the body can have
    reached paperless-ngx:

    * A refused or timed-out connection, no free pool slot, and a proxy that
      refused the tunnel all fail before a byte of the request is sent.  A
      body write that stalls past its timeout leaves the body incomplete, and
      paperless-ngx cannot build a document from a truncated upload.  These
      are safe to send again.
    * Any answer at all -- a 5xx included, since a proxy's 502 or 504 can
      follow a body paperless-ngx read in full -- and any failure while
      reading the answer may come after the body arrived.  The HTTP library
      does not report a failed body write: it goes on to read whatever the
      server says, so a cut connection shows up as one of these.  None of
      them is sent again.
    * A 406 is paperless-ngx refusing the API version before it looks at the
      request, so nothing was stored.

    Two paths decide without it, because neither retries, copies or raises
    anything a policy could change.  ``test_connection`` reports every
    transport error as UNREACHABLE; the Paperless check reports an unset
    ``paperless.url`` before it probes.  ``poll_task`` runs only after an
    upload was accepted, and keeps asking through any request error until its
    deadline, since failing then would invite a rescan and a duplicate.

    A chain of ``isinstance`` tests on exception types, with a total fallback:
    a new httpx2 subclass lands in the nearest arm above it, and anything
    unforeseen is UNEXPECTED rather than retried.

    Args:
        exc: The error a request raised.

    Returns:
        The decision for it.

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

    ``paperless.url`` and ``paperless.token`` must pass it to load at all, and
    a header value or URL made only of these characters is one h11 sends.

    Args:
        text: A configured value.

    Returns:
        True when every character is in ``!`` to ``~``.

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


def _render_error_body(response: httpx2.Response, token: str) -> str:
    """
    Reduce a Paperless error response body to one bounded line.

    This is the single body renderer for the client: the upload 4xx and 5xx,
    the task poll non-200 and the metadata fetch all use it.  A DRF JSON error
    is reduced to its ``detail``, else its first field error, else the first
    entry of a top-level list; anything else (an HTML proxy page, plain text)
    is used as it stands.  In every case the token is struck out first, then
    the whitespace is collapsed, so no newline, tab or other whitespace
    control character from the upstream can forge an extra CLI line, and the
    text is cut to ``_MAX_BODY_LINE_CHARS`` characters plus an ellipsis, so
    the job store and the web status area cannot be flooded.  The full body,
    with the token struck out too, is logged at DEBUG so it stays diagnosable.

    Args:
        response: The error response.
        token: The configured token, struck out of the body by ``_strike``.

    Returns:
        A non-empty single line; ``(empty response body)`` when the body
        carried nothing.

    """
    # Each control character is written out rather than dropped, so the body
    # stays whole for diagnosis without reaching a terminal as live escapes.
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

    Deliberately textual rather than parsed, so it also covers a URL httpx2
    rejects or reads differently (no scheme, a bad port), and deliberately
    greedy: it cuts through the *last* ``@``, so a password holding a raw
    ``@`` or ``/`` cannot leave a fragment behind.  The cost is that a base
    URL whose path holds an ``@`` is shown shortened, which is display only;
    a leaked credential cannot be taken back.

    Args:
        url: A configured or upstream-supplied URL.

    Returns:
        The URL with nothing left of its userinfo.

    """
    return _USERINFO.sub(r"\1", url, count=1)


def _bounded_line(text: str) -> str:
    """
    Collapse ``text`` to one line, tame its controls and cut it to length.

    Text from Paperless -- an error body, a redirect target, a task's failure
    -- is recorded in the job store, printed on the terminal and logged, so
    no newline in it may forge an extra line, no length of it may flood
    either, and no control character may reach a terminal live.  ESC, BEL,
    NUL and most C1 controls are not whitespace, so each is shown as its
    escape instead.  The cut comes last, at ``_MAX_BODY_LINE_CHARS``, so it
    bounds what is printed.

    Args:
        text: Upstream text of any shape.

    Returns:
        The text on one line with no control character in it, with an
        ellipsis when it was cut; empty when it held nothing but whitespace.

    """
    line = neutralise_controls(" ".join(text.split()))
    if len(line) > _MAX_BODY_LINE_CHARS:
        line = f"{line[:_MAX_BODY_LINE_CHARS]}…"
    return line


def _status_text(response: httpx2.Response) -> str:
    """
    Render a response's status code and reason phrase for a message.

    The reason phrase is upstream text like the body: HTTP allows any control
    character in it but NUL, CR and LF, so it goes through ``_bounded_line``
    before it reaches a message that is printed and logged.

    Args:
        response: The response whose status line is reported.

    Returns:
        ``"<code> <reason>"``, or only the code when the reason is empty.

    """
    return f"{response.status_code} {_bounded_line(response.reason_phrase)}".rstrip()


def _loggable_task_id(task_id: str) -> str:
    """
    Tame a paperless task id for a log line or an error message.

    Only what saneless prints is changed: the raw id is still what the poll
    sends back to paperless.

    Args:
        task_id: The id paperless answered the upload with.

    Returns:
        The id with control characters shown as escapes, cut to
        ``_TASK_ID_LOG_LIMIT`` characters.

    """
    return neutralise_bounded(task_id, _TASK_ID_LOG_LIMIT)


def _not_accepted_message(response: httpx2.Response, token: str) -> str:
    """
    Say why a non-2xx upload response that is not a server error is final.

    A 4xx is Paperless rejecting the upload, with its own reason.  A
    redirect is almost always ``paperless.url`` pointing at the wrong address
    -- a plain ``http://`` URL behind a proxy that redirects to ``https://`` --
    and retrying it cannot help, so it names where it was sent instead.
    Anything else (a 1xx, or a 3xx with no target) is reported by its status.

    Args:
        response: The upload response ``raise_for_status`` refused.
        token: The configured token, struck out of the body and the
            redirect target by ``_strike``.

    Returns:
        A single line naming the status and what to check.

    """
    status = _status_text(response)
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
# Each class carries only its own destination, so no result can claim both, or
# claim one without saying where; callers dispatch with ``match``.
UploadResult = ApiDelivery | FolderDelivery


@dataclass(frozen=True, slots=True, kw_only=True)
class PaperlessTiming:
    """
    How long the client keeps trying, and the clock it measures that on.

    One argument rather than four, so the client's constructor stays within
    ruff's five-parameter limit.  The defaults are the production values;
    a test replaces ``clock`` and ``sleep`` with a fake clock, so no wait is
    real and a 60 s budget runs in no time.

    Attributes:
        send_budget: How many seconds an upload keeps retrying failures that
            prove nothing arrived, counted from its first attempt on
            ``clock``.  0 makes exactly one attempt.
        clock: The monotonic clock the send budget is measured on.
        sleep: How the upload waits between those attempts.
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
      so nothing was stored.
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
            raise PaperlessError(msg) from exc
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

        Args:
            response: The answer just received.

        """
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
                configured (``could not connect for <N>s``); if the transport
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

        """
        # Only the open is guarded: an OSError here is the PDF itself, while
        # the request below raises httpx2's own types, which upload_document
        # sorts into retries.
        try:
            pdf_file = pdf_path.open("rb")
        except OSError as exc:
            raise _unreadable_pdf(pdf_path, exc) from exc
        with pdf_file as f:
            response = self._client.post(
                "/api/documents/post_document/",
                data=data,
                files={"document": (pdf_path.name, f, "application/pdf")},
                timeout=timeout,
            )
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
        msg = (
            f"Upload to Paperless{self._at_url} could not connect for "
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
            PaperlessError: If the directory does not exist (or is not a
                directory), or, chained to the OSError, if the copy fails.

        """
        if not dest_dir.is_dir():
            msg = (
                f"consume directory {dest_dir} does not exist — "
                "is the paperless-ngx volume mounted?"
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

    def poll_task(self, task_id: str, *, timeout: float) -> dict[str, object]:
        """
        Poll the task endpoint with exponential backoff until the task succeeds.

        Two wire shapes are tolerated. API v9 answers ``GET /api/tasks/``
        with a bare list of tasks whose ``status`` is uppercase and whose
        failure text is a flat ``result`` string.  API v10 paginates the
        list into ``{"count", "next", "previous", "results"}``, spells the
        status in lowercase, and moved the failure text into
        ``result_data["error_message"]``.  The client asks for v10 once the
        server has announced it and for v9 until then (see ``api_version``),
        so either shape can arrive.

        A 200 carrying no task is not an error.  A task is not always
        visible immediately after the upload that created it, so an empty
        list (or an empty ``results``) means "ask again", and polling
        continues until the deadline.  A non-200 *is* an error and ends the
        poll at once: a revoked token or a moved endpoint should be reported
        as what it is within a second, not as a timeout several minutes
        later.

        A request-level error while polling (a connection refused, a reset,
        a read timeout, a proxy closing the connection, a body that cannot be
        decoded) does *not* end the poll.  The upload has already been
        accepted, so failing the job now would invite the user to scan the
        document again and create a duplicate.  The error is logged and
        remembered, and the poll backs off and asks again within the same
        monotonic deadline.

        Args:
            task_id: Task UUID returned from upload.
            timeout: Maximum seconds to wait, measured on a monotonic clock
                that includes request time as well as sleep time. There is
                no default: callers pass ``output.paperless_task_timeout``,
                so the configured value is the only source.

        Returns:
            The task dict, only when the task reached SUCCESS.

        Raises:
            PaperlessError: If the task ends FAILURE or REVOKED, carrying
                the message paperless-ngx supplied (plus a check-before-
                rescanning hint when it was a duplicate); if any poll
                returns a non-200 response, carrying the status code, the
                reason phrase and the body reduced to one bounded line by
                ``_render_error_body``; or if a 200 body is not JSON,
                chained to the ValueError.  The token is struck out of every
                piece of server text in the message.
            PaperlessTimeoutError: If the deadline passes before the task
                reaches a terminal status. The message names the task id so
                the task can be looked up in paperless-ngx directly, and,
                when the last poll failed with a request error rather than
                being answered, ends by naming that error and is chained to it
                -- unless that error's chain quotes the token, when it carries
                no cause.

        """
        deadline = time.monotonic() + timeout
        delay = 0.5
        # RequestError rather than TransportError: DecodingError (a corrupt
        # compressed body) is a request-level failure that is not a transport
        # one, and it must neither escape this boundary as a raw httpx2 type nor
        # fail an upload Paperless already accepted.
        last_transport_error: httpx2.RequestError | None = None

        while True:
            try:
                response = self._client.get(
                    "/api/tasks/",
                    params={"task_id": task_id},
                )
            except httpx2.RequestError as exc:
                last_transport_error = exc
                logger.warning(
                    "Polling task %r failed, retrying until the deadline: %s",
                    _loggable_task_id(task_id),
                    self._reason(exc),
                )
            else:
                # Paperless answered, so an earlier blip is no longer the story:
                # a timeout after this names no stale transport error.
                last_transport_error = None
                task = self._finished_task(task_id, response)
                if task is not None:
                    return task

            # Every path through the loop body reaches this check -- a
            # transport error included -- so the poll cannot outlive its
            # deadline.
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                shown = _loggable_task_id(task_id)
                logger.warning("Task %r did not finish within %ss", shown, timeout)
                msg = f"Paperless task {shown} did not finish within {timeout}s"
                if last_transport_error is None:
                    raise PaperlessTimeoutError(msg)
                msg = f"{msg}; last error: {self._reason(last_transport_error)}"
                raise PaperlessTimeoutError(msg) from self._cause(last_transport_error)

            # Clamped so the poll never sleeps past its own deadline -- that
            # is what makes a sub-second timeout cost what it says it does
            # rather than the 0.5s first backoff.
            time.sleep(min(delay, remaining))
            delay = min(delay * 2, 30.0)

    def _finished_task(
        self, task_id: str, response: httpx2.Response
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
                is one line.

        """
        if response.status_code != 200:
            msg = (
                f"Paperless task poll failed ({_status_text(response)}): "
                f"{_render_error_body(response, self._token)}"
            )
            raise PaperlessError(msg)

        try:
            payload = response.json()
        except ValueError as exc:
            msg = (
                f"Paperless{self._at_url} returned a task response that is "
                f"not JSON: {self._reason(exc)}"
            )
            raise PaperlessError(msg) from self._cause(exc)

        task = _extract_task(payload)
        if task is None:
            return None
        status = _task_status(task)
        if status == "SUCCESS":
            logger.info("Task %r completed: %s", _loggable_task_id(task_id), status)
            return task
        if status in _TERMINAL_STATUSES:
            full_failure = " ".join(
                _strike(_failure_message(task), self._token).split()
            )
            # Bounded like an error body: a failure result can embed a whole
            # OCR or consumer traceback, and this text becomes job.error and
            # the CLI line.  The duplicate check reads the
            # whole text, so a hint past the cut is not lost.
            failure = _bounded_line(full_failure) or _NO_FAILURE_MESSAGE
            msg = (
                f"Paperless task {_loggable_task_id(task_id)} ended {status}: {failure}"
            )
            if _is_duplicate_failure(task, full_failure):
                msg = f"{msg}; {_DUPLICATE_HINT}"
            raise PaperlessError(msg)
        return None

    def test_connection(
        self, *, timeout: httpx2.Timeout | None = None
    ) -> ConnectionStatus:
        """
        Probe paperless-ngx and report which of six outcomes occurred.

        CONNECTED means a 2xx and nothing else.  A 404 says the API is not
        where the configured URL points -- a different thing to fix than a
        500, which says paperless-ngx itself is unwell, and both used to be
        reported as CONNECTED.  A 406 is INCOMPATIBLE: paperless-ngx refused
        the API version, so it is older than 2.16 or newer than this client
        knows.  It is not tried again with another version.

        The classification is an ordered chain of integer comparisons rather
        than a ``match`` with ``assert_never``, for the same reason
        ``vocabulary.classify_error`` is an ``isinstance`` chain: the input
        is a range of integers, not a closed set of members, so exhaustive
        matching does not apply and a trailing fallback is the correct total
        answer.  ``assert_never`` governs the *message* lookup in
        ``vocabulary.connection_status_message``, which does dispatch on a
        closed set.

        The bound is a per-request override rather than a constructor
        argument, because the two kinds of caller want different budgets.
        ``SaneBackend.get_devices()`` still has no timeout inside python-sane
        or ``sane_get_devices(3)``, and none is settable from Python.  saneless
        runs it in a child process that is stopped at a deadline, and the SANE
        side of the status strip keeps its socket pre-probe as well, so a
        silent host is named in seconds and never holds the scanner gate for
        the whole deadline; httpx2, by contrast, takes a bound per request.
        The status strip and ``saneless doctor`` pass a short budget so an
        unplugged host is discovered in about two seconds instead of thirty,
        while ``GET /api/paperless/test`` deliberately keeps today's client
        default and therefore calls this with no argument at all.

        Bounding the probe needs no new exception handling.  The
        ``except httpx2.TransportError`` arm below is the base class of
        ``ConnectTimeout`` and ``ReadTimeout`` as well as ``ConnectError``, so
        a budget that expires already lands on UNREACHABLE.

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
        try:
            response = self._client.get(
                "/api/tags/",
                params={"page_size": 1},
                timeout=timeout if timeout is not None else httpx2.USE_CLIENT_DEFAULT,
            )
        except httpx2.TransportError:
            # The base class of ConnectError, ConnectTimeout and ReadTimeout.
            # Catching only ConnectError let the timeout siblings escape to
            # routes.py's blanket handler, which answers HTTP 502
            # {"status": "error"} -- none of the outcomes.
            logger.warning("Paperless is unreachable")
            return ConnectionStatus.UNREACHABLE

        if response.is_success:
            return ConnectionStatus.CONNECTED
        if response.status_code in (401, 403):
            return ConnectionStatus.TOKEN_REJECTED
        if response.status_code == 404:
            return ConnectionStatus.NOT_FOUND
        if response.status_code == 406:
            return ConnectionStatus.INCOMPATIBLE
        logger.warning("Unexpected paperless status %s", response.status_code)
        return ConnectionStatus.SERVER_ERROR

    def get_tags(self) -> list[dict[str, object]]:
        """
        Fetch all tags from paperless-ngx.

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
        return self._fetch_collection("/api/tags/", "tags")

    def get_correspondents(self) -> list[dict[str, object]]:
        """
        Fetch all correspondents from paperless-ngx.

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
        return self._fetch_collection("/api/correspondents/", "correspondents")

    def _fetch_collection(self, path: str, noun: str) -> list[dict[str, object]]:
        """
        Fetch every page of one metadata collection.

        Pages are requested by number, ``?page=N``, on the configured base
        URL until a page's ``next`` is null, a page comes back empty, or the
        items collected reach the server's own ``count``.  The ``next`` link
        itself is only read as "there is another page" and is never
        requested: a server behind a misconfigured proxy builds it
        from the wrong host or scheme, and following it would send the API
        token there.  A redirect is not followed either; it fails the fetch.
        A bare-list response on page 1 is the whole collection; on a later
        page it fails the fetch rather than replacing the pages collected.
        Each page's items must be a list of objects.

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

        Returns:
            The ``results`` of every page in order, or a bare list as is.

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
            data = self._fetch_page(path, page, prefix)
            if isinstance(data, list):
                if page > 1:
                    msg = f"{prefix}: page {page} was a bare list, which only page 1 may be"
                    raise PaperlessError(msg)
                return _metadata_items(data, prefix, page)
            if not isinstance(data, dict):
                msg = f"{prefix}: the response was neither a list nor an object"
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

    def _fetch_page(self, path: str, page: int, prefix: str) -> object:
        """
        Request one metadata page and return its decoded JSON body.

        Args:
            path: The collection endpoint, e.g. ``/api/tags/``.
            page: The page number to ask for.
            prefix: The message prefix naming the collection and base URL.

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
        try:
            response = self._client.get(
                path, params={"page": page, "page_size": _METADATA_PAGE_SIZE}
            )
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
