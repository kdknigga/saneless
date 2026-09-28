"""Tests for paperless-ngx REST client."""

from __future__ import annotations

import dataclasses
import errno
import inspect
import logging
import os
import re
import shutil
import stat
import time
import traceback
from collections import Counter
from typing import TYPE_CHECKING

import httpx2
import pytest

from saneless.exceptions import (
    ConfigError,
    PaperlessError,
    PaperlessTimeoutError,
    describe,
)
from saneless.paperless import (
    ApiDelivery,
    FolderDelivery,
    PaperlessClient,
    UploadResult,
    _not_accepted_message,
    _one_line_reason,
    _render_error_body,
    _retry_decision,
    _RetryDecision,
    _without_userinfo,
)
from saneless.text_safety import has_control_characters
from saneless.vocabulary import ConnectionStatus
from tests.golden_support import (
    DOCUMENTS_PATH,
    LOOPBACK_SESSION_COOKIE,
    LOOPBACK_TASK_ID,
    TASKS_PATH,
    LoopbackHit,
    loopback_paperless,
    multipart_fields,
    production_debug_logging,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path
    from typing import BinaryIO

_MOCK_AUTH = "testtoken"


@pytest.fixture
def sample_pdf(tmp_path: Path) -> Path:
    """Create a minimal PDF file for upload tests."""
    pdf_path = tmp_path / "test.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake content")
    return pdf_path


def _make_transport(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> httpx2.MockTransport:
    """Create an httpx2.MockTransport from a handler function."""
    return httpx2.MockTransport(handler)


def _v9_payload(status: str, message: str | None) -> object:
    """
    Build an API v9 ``/api/tasks/`` body: a bare list, uppercase status.

    The failure text lives in a flat ``result`` string on v9.
    """
    task: dict[str, object] = {"task_id": "t1", "status": status.upper()}
    if message is not None:
        task["result"] = message
    return [task]


def _v10_payload(status: str, message: str | None) -> object:
    """
    Build an API v10 ``/api/tasks/`` body: paginated, lowercase status.

    The failure text moved into ``result_data["error_message"]`` on v10,
    which is why a single-field extraction cannot serve both versions.
    """
    task: dict[str, object] = {"task_id": "t1", "status": status.lower()}
    if message is not None:
        task["result_data"] = {
            "error_type": "ConsumerError",
            "error_message": message,
        }
    return {"count": 1, "next": None, "previous": None, "results": [task]}


def _v9_no_task() -> object:
    """Build the v9 spelling of "200, but the task is not visible yet"."""
    return []


def _v10_no_task() -> object:
    """Build the v10 spelling of "200, but the task is not visible yet"."""
    return {"count": 0, "next": None, "previous": None, "results": []}


_API_SHAPES = [
    pytest.param(_v9_payload, id="v9"),
    pytest.param(_v10_payload, id="v10"),
]

_API_NO_TASK_SHAPES = [
    pytest.param(_v9_payload, _v9_no_task, id="v9"),
    pytest.param(_v10_payload, _v10_no_task, id="v10"),
]


def _poll_client(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> PaperlessClient:
    """Build a client wired to the given mock handler."""
    return PaperlessClient(
        url="http://paperless:8000",
        token=_MOCK_AUTH,
        transport=_make_transport(handler),
    )


def _connection_result_for_status(status_code: int) -> ConnectionStatus:
    """Run test_connection against a server that answers with one status code."""

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status_code, text="")

    client = _poll_client(handler)
    try:
        return client.test_connection()
    finally:
        client.close()


def _connection_result_for_exception(
    exc_type: type[httpx2.TransportError],
) -> ConnectionStatus:
    """Run test_connection against a transport that raises."""

    def handler(_request: httpx2.Request) -> httpx2.Response:
        msg = "transport failed"
        raise exc_type(msg)

    client = _poll_client(handler)
    try:
        return client.test_connection()
    finally:
        client.close()


_CONNECTION_STATUS_CASES = [
    pytest.param(200, ConnectionStatus.CONNECTED, id="200"),
    pytest.param(204, ConnectionStatus.CONNECTED, id="204"),
    pytest.param(401, ConnectionStatus.TOKEN_REJECTED, id="401"),
    pytest.param(403, ConnectionStatus.TOKEN_REJECTED, id="403"),
    pytest.param(404, ConnectionStatus.NOT_FOUND, id="404"),
    pytest.param(500, ConnectionStatus.SERVER_ERROR, id="500"),
    pytest.param(503, ConnectionStatus.SERVER_ERROR, id="503"),
    pytest.param(302, ConnectionStatus.SERVER_ERROR, id="302"),
    pytest.param(429, ConnectionStatus.SERVER_ERROR, id="429"),
]

_CONNECTION_EXCEPTION_CASES = [
    pytest.param(httpx2.ConnectError, id="connect-error"),
    pytest.param(httpx2.ConnectTimeout, id="connect-timeout"),
    pytest.param(httpx2.ReadTimeout, id="read-timeout"),
]


def _assert_no_staging_files(consume_dir: Path) -> None:
    """Assert no half-written staging file survives in the consume directory."""
    leftovers = sorted(
        entry.name
        for entry in consume_dir.iterdir()
        if entry.name.startswith(".") or entry.name.endswith(".part")
    )
    assert not leftovers, (
        f"staging files left behind in the consume directory paperless-ngx "
        f"watches: {leftovers}. The dotfile prefix and the '.part' extension "
        f"exist only so the inotify consumer skips the file while it is being "
        f"written; one surviving the upload means the atomic rename did not "
        f"happen or its cleanup did not run."
    )


def _always_refused(_request: httpx2.Request) -> httpx2.Response:
    """Refuse every connection, forcing the consume-directory fallback."""
    msg = "connection refused"
    raise httpx2.ConnectError(msg)


# ---------------------------------------------------------------------------
# Upload tests
# ---------------------------------------------------------------------------


class TestClientSignature:
    """The client's injection seam and its timeout are part of its API shape."""

    def test_transport_is_a_keyword_only_parameter_defaulting_to_none(self) -> None:
        """``transport`` mirrors ``httpx2.Client(transport=...)``, keyword-only."""
        parameter = inspect.signature(PaperlessClient.__init__).parameters["transport"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is None

    def test_the_private_transport_spelling_is_gone(self) -> None:
        """No ``_transport`` parameter remains alongside the public one."""
        assert (
            "_transport" not in inspect.signature(PaperlessClient.__init__).parameters
        )

    def test_poll_task_timeout_is_required_and_keyword_only(self) -> None:
        """The configured task timeout is the only source; there is no default."""
        parameter = inspect.signature(PaperlessClient.poll_task).parameters["timeout"]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty


def _uploaded_fields(
    pdf: Path,
    title: str,
    tags: list[int] | None = None,
    correspondent: int | None = None,
) -> list[tuple[str, str | bytes]]:
    """
    Upload ``pdf`` once and read back the form parts that went on the wire.

    Args:
        pdf: The PDF to upload.
        title: Document title.
        tags: Optional tag ids.
        correspondent: Optional correspondent id.

    Returns:
        The parsed ``(name, value)`` form parts of the one upload request.

    """
    uploads: list[list[tuple[str, str | bytes]]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        uploads.append(multipart_fields(request))
        return httpx2.Response(200, json="task-id")

    client = PaperlessClient(
        url="http://paperless:8000",
        token=_MOCK_AUTH,
        transport=_make_transport(handler),
    )
    try:
        client.upload_document(pdf, title, tags=tags, correspondent=correspondent)
    finally:
        client.close()
    assert len(uploads) == 1
    return uploads[0]


class TestUploadDocument:
    """Document upload tests."""

    def test_upload_document(self, sample_pdf: Path) -> None:
        """Upload returns task UUID on success."""
        task_uuid = "abc-123-def"

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=task_uuid)

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            transport=transport,
        )
        result = client.upload_document(sample_pdf, title="Test Doc")
        assert result.delivered_to_api is True
        assert result.task_uuid == task_uuid
        assert result.consume_dir_path is None
        client.close()

    def test_upload_with_tags(self, sample_pdf: Path) -> None:
        """Each tag id travels as its own ``tags`` form field, and nothing else."""
        fields = _uploaded_fields(sample_pdf, "Test", tags=[1, 2, 3])
        assert Counter(fields) == Counter(
            [
                ("title", "Test"),
                ("tags", "1"),
                ("tags", "2"),
                ("tags", "3"),
                ("document", sample_pdf.read_bytes()),
            ]
        )

    def test_upload_with_correspondent(self, sample_pdf: Path) -> None:
        """The correspondent id travels as one exact ``correspondent`` field."""
        fields = _uploaded_fields(sample_pdf, "Test", correspondent=5)
        assert Counter(fields) == Counter(
            [
                ("title", "Test"),
                ("correspondent", "5"),
                ("document", sample_pdf.read_bytes()),
            ]
        )

    def test_upload_never_sends_created(self, sample_pdf: Path) -> None:
        """
        No document date is sent, so paperless-ngx dates the document itself.

        The full metadata set goes out and nothing more: no ``created`` field,
        and ``upload_document`` has no way to be given one.
        """
        fields = _uploaded_fields(sample_pdf, "Test", tags=[1, 2, 3], correspondent=5)
        assert "created" not in [name for name, _ in fields]
        assert Counter(fields) == Counter(
            [
                ("title", "Test"),
                ("correspondent", "5"),
                ("tags", "1"),
                ("tags", "2"),
                ("tags", "3"),
                ("document", sample_pdf.read_bytes()),
            ]
        )
        parameters = inspect.signature(PaperlessClient.upload_document).parameters
        assert "created" not in parameters

    def test_form_fields_sent_as_data_not_files(self, sample_pdf: Path) -> None:
        """The title is a plain form field; only the PDF is a file part."""
        fields = _uploaded_fields(sample_pdf, "Test Doc")
        # multipart_fields decodes a part without a filename to str and keeps
        # a file part's raw bytes, so an exact match also shows how each part
        # was sent: a title sent as a file would arrive as b"Test Doc".
        assert Counter(fields) == Counter(
            [
                ("title", "Test Doc"),
                ("document", sample_pdf.read_bytes()),
            ]
        )

    def test_upload_retry_on_network_error(
        self, sample_pdf: Path, sleeps: list[float]
    ) -> None:
        """Upload retries on ConnectError and eventually succeeds."""
        call_count = {"n": 0}

        def handler(_request: httpx2.Request) -> httpx2.Response:
            call_count["n"] += 1
            if call_count["n"] <= 2:
                msg = "connection refused"
                raise httpx2.ConnectError(msg)
            return httpx2.Response(200, json="task-id-ok")

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            transport=transport,
            max_retries=3,
        )
        result = client.upload_document(sample_pdf, title="Retry Test")
        assert result.delivered_to_api is True
        assert result.task_uuid == "task-id-ok"
        assert call_count["n"] == 3
        assert sleeps == [1, 2]
        client.close()

    def test_upload_no_retry_on_4xx(self, sample_pdf: Path) -> None:
        """Upload does not retry on 4xx errors."""
        call_count = {"n": 0}

        def handler(_request: httpx2.Request) -> httpx2.Response:
            call_count["n"] += 1
            return httpx2.Response(400, text="Bad Request")

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            transport=transport,
        )
        with pytest.raises(PaperlessError, match="rejected"):
            client.upload_document(sample_pdf, title="Bad")
        assert call_count["n"] == 1
        client.close()

    def test_upload_retry_exhausted_no_fallback(
        self, sample_pdf: Path, sleeps: list[float]
    ) -> None:
        """Upload raises PaperlessError when retries are exhausted without fallback."""

        def handler(_request: httpx2.Request) -> httpx2.Response:
            msg = "connection refused"
            raise httpx2.ConnectError(msg)

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            transport=transport,
            max_retries=3,
        )
        with pytest.raises(PaperlessError, match="attempts"):
            client.upload_document(sample_pdf, title="Fail")
        assert sleeps == [1, 2]
        client.close()

    def test_upload_retry_exhausted_with_fallback(
        self, sample_pdf: Path, tmp_path: Path, sleeps: list[float]
    ) -> None:
        """Upload falls back to consume directory when retries are exhausted."""
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()

        def handler(_request: httpx2.Request) -> httpx2.Response:
            msg = "connection refused"
            raise httpx2.ConnectError(msg)

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            consume_dir=consume_dir,
            transport=transport,
            max_retries=3,
        )
        result = client.upload_document(sample_pdf, title="Fallback")
        assert result.delivered_to_api is False
        assert result.task_uuid is None
        # PDF should have been copied to consume dir
        copied = list(consume_dir.iterdir())
        assert len(copied) == 1
        assert copied[0].name == "test.pdf"
        assert copied[0].read_bytes() == sample_pdf.read_bytes()
        _assert_no_staging_files(consume_dir)
        # The result names the exact file the PDF was copied to -- something
        # the old magic-string sentinel could not carry.
        assert result.consume_dir_path == copied[0]
        assert sleeps == [1, 2]
        client.close()


# ---------------------------------------------------------------------------
# Upload failure translation (EXC-01, D-08, D-10)
# ---------------------------------------------------------------------------


_TRANSIENT_TRANSPORT_CASES = [
    pytest.param(httpx2.ReadError, id="read-error"),
    pytest.param(httpx2.WriteError, id="write-error"),
    pytest.param(httpx2.RemoteProtocolError, id="remote-protocol-error"),
    pytest.param(httpx2.ConnectError, id="connect-error"),
    pytest.param(httpx2.ConnectTimeout, id="connect-timeout"),
    pytest.param(httpx2.ReadTimeout, id="read-timeout"),
]

_UNSUPPORTED_PROTOCOL_TEXT = (
    "Request URL is missing an 'http://' or 'https://' protocol."
)

# The fixed text an upload with an unset or scheme-less paperless.url fails
# with.  Fixed so that nothing httpx2 or h11 says -- which for a refused header
# value is the header value itself -- can reach it.
_UNSET_URL_UPLOAD_MESSAGE = (
    "Uploading to Paperless: paperless.url is not set, or has no http or https "
    "scheme; set it to the paperless-ngx address"
)

# The fixed text for a request the transport refused to send.
_UNSENDABLE_REQUEST_REASON = (
    "the request could not be sent with the configured paperless.url and "
    "paperless.token; check both for spaces, line breaks or control characters"
)


def _assert_token_absent(token: str, text: str) -> None:
    """
    Assert neither the token as configured nor its stripped form is in ``text``.

    Library text shows a header value as a ``bytes`` repr, where a trailing
    carriage return is spelled out as an escape and no longer matches the raw
    token, so the stripped form is what catches it there.

    Args:
        token: The configured token.
        text: The message, log text or repr to search.

    """
    assert token not in text
    assert token.strip() not in text


class _CountingHandler:
    """A mock transport handler that counts calls and replays a script."""

    def __init__(self, respond: Callable[[int], httpx2.Response]) -> None:
        """Build a handler whose n-th call (1-based) is answered by ``respond``."""
        self.calls = 0
        self._respond = respond

    def __call__(self, _request: httpx2.Request) -> httpx2.Response:
        """Count the call and answer it."""
        self.calls += 1
        return self._respond(self.calls)


def _raising(exc: Exception) -> Callable[[int], httpx2.Response]:
    """Build a script that raises ``exc`` on every call."""

    def respond(_call: int) -> httpx2.Response:
        raise exc

    return respond


def _answering(response: httpx2.Response) -> Callable[[int], httpx2.Response]:
    """Build a script that answers every call with ``response``."""

    def respond(_call: int) -> httpx2.Response:
        return response

    return respond


def _upload_client(
    handler: _CountingHandler, consume_dir: Path | None = None
) -> PaperlessClient:
    """Build a three-attempt upload client wired to the counting handler."""
    return PaperlessClient(
        url="http://paperless:8000",
        token=_MOCK_AUTH,
        consume_dir=consume_dir,
        transport=_make_transport(handler),
        max_retries=3,
    )


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record upload backoff sleeps instead of really sleeping."""
    recorded: list[float] = []
    monkeypatch.setattr("saneless.paperless.time.sleep", recorded.append)
    return recorded


class TestPaperlessUrlValidation:
    """
    A URL or token the client cannot use is refused at construction.

    Load-time validation already refuses all of these, so they reach the
    client only when it is built directly.  Each refusal is a fixed-text
    ``PaperlessError`` with no chained cause: the parser's and the codec's own
    text can quote a password or a token character, and a traceback prints the
    cause.
    """

    def test_invalid_url_is_refused_without_the_parser_text(self) -> None:
        """``http://host:abc`` names the URL, not httpx2's complaint about it."""
        with pytest.raises(PaperlessError) as exc_info:
            PaperlessClient("http://host:abc", "tok-SECRET-5d1")
        message = str(exc_info.value)
        assert message == "Paperless URL http://host:abc is not valid"
        assert "Invalid port" not in message
        assert "tok-SECRET-5d1" not in message
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__suppress_context__

    def test_invalid_url_never_quotes_a_password_the_parser_misread(self) -> None:
        """
        A ``/`` in a password makes httpx2 read the password as a port.

        Its own text would then quote the first half of the password
        (``Invalid port: 'hunter'``); the message shows the URL with its
        userinfo cut through the last ``@`` and nothing else.
        """
        with pytest.raises(PaperlessError) as exc_info:
            PaperlessClient("http://scanner:hunter/2secret@host", _MOCK_AUTH)
        message = str(exc_info.value)
        assert message == "Paperless URL http://host is not valid"
        for fragment in ("hunter", "2secret", "scanner", "Invalid port"):
            assert fragment not in message
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__suppress_context__

    def test_non_ascii_token_is_refused_with_a_fixed_message(self) -> None:
        """A token no HTTP header can carry is named by key, never quoted."""
        with pytest.raises(PaperlessError) as exc_info:
            PaperlessClient("http://paperless:8000", "tök")
        message = str(exc_info.value)
        assert message == (
            "Paperless API token in paperless.token contains a character an "
            "HTTP header cannot carry"
        )
        assert "tök" not in message
        assert "ö" not in message
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__suppress_context__


class TestTrustStoreConfigErrors:
    """
    A broken ``SSL_CERT_FILE`` is a PaperlessError at construction.

    The TLS trust anchors are read while the client is being built, so both
    ways a deployer following the documented ``SSL_CERT_FILE`` remedy can get
    it wrong arrive at this boundary.  A typo in the path is the missing-file
    case.  The documented docker-compose bind mount
    ``./my-ca.crt:/etc/ssl/certs/my-ca.crt:ro`` is the directory case: when the
    host file is absent Docker silently creates a *directory* at the container
    path.  Neither may leave the boundary as a raw ``OSError`` traceback.
    """

    def test_missing_ssl_cert_file_raises_a_paperless_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """An unreadable SSL_CERT_FILE names both variables and not the token."""
        monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "absent-ca.crt"))
        # An SSL_CERT_DIR inherited from the developer's environment would
        # change which OpenSSL branch is taken, so the test sets the whole
        # pair rather than only the half it is about.
        monkeypatch.delenv("SSL_CERT_DIR", raising=False)
        # No transport= argument, deliberately: httpx2.Client skips
        # create_ssl_context entirely when a transport is supplied, so this
        # test would pass against an unguarded client if it reached for the
        # module's usual MockTransport seam.  Only the real transport reads
        # the trust store.  Do not add a transport here.
        with pytest.raises(PaperlessError) as exc_info:
            PaperlessClient("https://paperless.example.com", "tok-SECRET-5d1")
        message = str(exc_info.value)
        assert "SSL_CERT_FILE" in message
        assert "SSL_CERT_DIR" in message
        assert isinstance(exc_info.value.__cause__, FileNotFoundError)
        assert "tok-SECRET-5d1" not in message

    def test_ssl_cert_file_naming_a_directory_raises_a_paperless_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """An SSL_CERT_FILE that is a directory chains its IsADirectoryError."""
        monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path))
        monkeypatch.delenv("SSL_CERT_DIR", raising=False)
        # No transport= argument, for the reason given in the test above.
        with pytest.raises(PaperlessError) as exc_info:
            PaperlessClient("https://paperless.example.com", "tok-SECRET-5d1")
        assert "SSL_CERT_FILE" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, IsADirectoryError)


_URL_SECRET = "pr0xy-S3CRET"


class TestUrlCredentialsNeverShown:
    """
    A ``user:password@`` in paperless.url never reaches a request or a message.

    The client refuses such a URL outright rather than turning its userinfo
    into Basic auth that would replace the API token.  Messages land in
    ``job.error`` (rendered in the web status area), on the terminal and in
    the log, so the refusal is checked for the password as well.
    """

    def test_userinfo_url_is_refused_at_construction(self) -> None:
        """The refusal names both keys and carries no part of the credentials."""
        with pytest.raises(PaperlessError) as exc_info:
            PaperlessClient("http://u:secretpw@paperless:8000", "tok")
        message = str(exc_info.value)
        assert message == (
            "Paperless URL http://paperless:8000 carries a user name or "
            "password; remove it from paperless.url and put the paperless-ngx "
            "API token in paperless.token"
        )
        assert "secretpw" not in message
        assert "u:" not in message
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__suppress_context__

    def test_userinfo_url_never_sends_basic_auth(self) -> None:
        """No request is made, so no Basic header can replace the token."""
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(200, json={"results": []})

        with pytest.raises(PaperlessError):
            PaperlessClient(
                url=f"https://scanner:{_URL_SECRET}@paperless.example/sub/",
                token=_MOCK_AUTH,
                transport=_make_transport(handler),
            )
        assert seen == []

    def test_userinfo_refusal_logs_nothing_carrying_the_password(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Neither the message nor any log record names the password."""
        caplog.set_level(logging.DEBUG)
        with pytest.raises(PaperlessError) as exc_info:
            PaperlessClient(
                url=f"https://scanner:{_URL_SECRET}@paperless.example",
                token=_MOCK_AUTH,
                transport=_make_transport(_CountingHandler(_raising(AssertionError()))),
            )
        assert _URL_SECRET not in str(exc_info.value)
        assert _URL_SECRET not in repr(exc_info.value)
        assert all(_URL_SECRET not in record.getMessage() for record in caplog.records)

    def test_invalid_url_with_userinfo_is_refused_without_the_password(self) -> None:
        """A URL httpx2 rejects is shown without its userinfo and parser text."""
        with pytest.raises(PaperlessError) as exc_info:
            PaperlessClient(f"http://scanner:{_URL_SECRET}@host:abc", _MOCK_AUTH)
        assert str(exc_info.value) == "Paperless URL http://host:abc is not valid"
        assert exc_info.value.__cause__ is None

    def test_scheme_less_url_message_strips_the_password(
        self, sample_pdf: Path, sleeps: list[float], caplog: pytest.LogCaptureFixture
    ) -> None:
        """A URL with no scheme, which httpx2 cannot parse as userinfo, is cut too."""
        caplog.set_level(logging.WARNING, logger="saneless.paperless")
        client = PaperlessClient(
            url=f"scanner:{_URL_SECRET}@paperless:8000", token=_MOCK_AUTH
        )
        try:
            with pytest.raises(ConfigError) as exc_info:
                client.upload_document(sample_pdf, title="No scheme")
        finally:
            client.close()
        assert str(exc_info.value) == _UNSET_URL_UPLOAD_MESSAGE
        assert exc_info.value.__cause__ is None
        assert all(_URL_SECRET not in message for message in caplog.messages)
        assert sleeps == []

    @pytest.mark.parametrize(
        ("url", "shown"),
        [
            ("https://u:pw@paperless.example/sub", "https://paperless.example/sub"),
            ("http://scanner:p@ss/w0rd@host:8000", "http://host:8000"),
            ("scanner:pw@paperless:8000", "paperless:8000"),
            ("http://paperless:8000", "http://paperless:8000"),
            ("", ""),
        ],
        ids=["userinfo", "raw-at-and-slash", "no-scheme", "no-userinfo", "empty"],
    )
    def test_userinfo_is_cut_through_the_last_at(self, url: str, shown: str) -> None:
        """No piece of a password holding a raw ``@`` or ``/`` is left behind."""
        assert _without_userinfo(url) == shown

    def test_redirect_target_is_shown_without_credentials(
        self, sample_pdf: Path, sleeps: list[float]
    ) -> None:
        """A redirect target carrying userinfo is redacted like the base URL."""
        target = f"https://scanner:{_URL_SECRET}@paperless.example/api/"
        handler = _CountingHandler(
            _answering(httpx2.Response(302, headers={"location": target}))
        )
        client = _upload_client(handler)
        try:
            with pytest.raises(PaperlessError) as exc_info:
                client.upload_document(sample_pdf, title="Redirected")
        finally:
            client.close()
        assert str(exc_info.value) == (
            "Paperless redirected the upload (302 Found) to "
            "https://paperless.example/api/; check paperless.url"
        )
        assert sleeps == []


_CLASSIFIED_REQUEST = httpx2.Request("POST", "http://paperless:8000/api/documents/")


def _status_error(status_code: int) -> httpx2.HTTPStatusError:
    """
    Build the ``HTTPStatusError`` ``raise_for_status`` raises for a status.

    Args:
        status_code: The response status.

    Returns:
        The error, carrying a response with that status.

    """
    response = httpx2.Response(status_code, request=_CLASSIFIED_REQUEST)
    return httpx2.HTTPStatusError(
        f"status {status_code}", request=_CLASSIFIED_REQUEST, response=response
    )


_RETRY_DECISION_CASES = [
    pytest.param(
        httpx2.LocalProtocolError("Illegal header value"),
        _RetryDecision.MISCONFIGURED,
        id="local-protocol-error",
    ),
    pytest.param(
        httpx2.UnsupportedProtocol(_UNSUPPORTED_PROTOCOL_TEXT),
        _RetryDecision.MISCONFIGURED,
        id="unsupported-protocol",
    ),
    pytest.param(httpx2.ConnectError("refused"), _RetryDecision.RETRY, id="connect"),
    pytest.param(
        httpx2.ConnectTimeout("slow"), _RetryDecision.RETRY, id="connect-timeout"
    ),
    pytest.param(httpx2.ReadTimeout("slow"), _RetryDecision.RETRY, id="read-timeout"),
    pytest.param(httpx2.ReadError("reset"), _RetryDecision.RETRY, id="read-error"),
    pytest.param(httpx2.WriteError("pipe"), _RetryDecision.RETRY, id="write-error"),
    pytest.param(
        httpx2.RemoteProtocolError("server hung up"),
        _RetryDecision.RETRY,
        id="remote-protocol-error",
    ),
    pytest.param(_status_error(503), _RetryDecision.RETRY, id="503"),
    pytest.param(_status_error(500), _RetryDecision.RETRY, id="500"),
    pytest.param(_status_error(404), _RetryDecision.REFUSED, id="404"),
    pytest.param(_status_error(302), _RetryDecision.REFUSED, id="302"),
    pytest.param(
        httpx2.DecodingError("corrupt gzip", request=_CLASSIFIED_REQUEST),
        _RetryDecision.UNEXPECTED,
        id="decoding-error",
    ),
]


class TestRetryDecision:
    """One pure function decides what the client does with every httpx2 error."""

    @pytest.mark.parametrize(("exc", "expected"), _RETRY_DECISION_CASES)
    def test_retry_decision_classifies_each_error(
        self, exc: httpx2.HTTPError, expected: _RetryDecision
    ) -> None:
        """
        Client-side protocol errors are configuration, never transient.

        ``LocalProtocolError`` and ``UnsupportedProtocol`` are both
        ``TransportError`` subclasses, so a classifier that tests for
        ``TransportError`` first would retry them.
        """
        assert _retry_decision(exc) is expected


class TestUploadFailureTranslation:
    """EXC-01 / D-10 / M-17: every upload failure ends as a PaperlessError."""

    @pytest.mark.parametrize("exc_type", _TRANSIENT_TRANSPORT_CASES)
    def test_transient_transport_failure_is_retried_then_raises(
        self,
        exc_type: type[httpx2.TransportError],
        sample_pdf: Path,
        sleeps: list[float],
    ) -> None:
        """
        Every transient TransportError retries for max_retries attempts.

        M-17: a reverse proxy closing the connection (ReadError,
        RemoteProtocolError) used to escape raw instead of retrying.
        """
        failure = exc_type("upstream went away")
        handler = _CountingHandler(_raising(failure))
        client = _upload_client(handler)
        try:
            with pytest.raises(
                PaperlessError,
                match=(
                    "Upload to Paperless at http://paperless:8000 failed after "
                    "3 attempts: upstream went away"
                ),
            ) as exc_info:
                client.upload_document(sample_pdf, title="Transient")
        finally:
            client.close()
        assert handler.calls == 3
        assert exc_info.value.__cause__ is failure
        assert sleeps == [1, 2]

    @pytest.mark.parametrize("exc_type", _TRANSIENT_TRANSPORT_CASES)
    def test_transient_transport_failure_falls_back_after_retries(
        self,
        exc_type: type[httpx2.TransportError],
        sample_pdf: Path,
        tmp_path: Path,
        sleeps: list[float],
    ) -> None:
        """With a consume directory the exhausted retries take the fallback."""
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()
        handler = _CountingHandler(_raising(exc_type("upstream went away")))
        client = _upload_client(handler, consume_dir=consume_dir)
        try:
            result = client.upload_document(sample_pdf, title="Transient")
        finally:
            client.close()
        assert handler.calls == 3
        assert result == UploadResult(
            delivered_to_api=False, consume_dir_path=consume_dir / "test.pdf"
        )
        assert (consume_dir / "test.pdf").read_bytes() == sample_pdf.read_bytes()
        assert sleeps == [1, 2]

    def test_remote_protocol_error_then_success_is_delivered(
        self, sample_pdf: Path, sleeps: list[float]
    ) -> None:
        """Two dropped connections then a 200 deliver to the API on attempt 3."""

        def respond(call: int) -> httpx2.Response:
            if call <= 2:
                msg = "Server disconnected without sending a response."
                raise httpx2.RemoteProtocolError(msg)
            return httpx2.Response(200, json="task-id")

        handler = _CountingHandler(respond)
        client = _upload_client(handler)
        try:
            result = client.upload_document(sample_pdf, title="Flaky proxy")
        finally:
            client.close()
        assert result.delivered_to_api is True
        assert result.task_uuid == "task-id"
        assert handler.calls == 3
        assert sleeps == [1, 2]

    def test_empty_read_timeout_names_its_class(
        self, sample_pdf: Path, sleeps: list[float]
    ) -> None:
        """An empty ``ReadTimeout("")`` still says what happened (describe rule)."""
        handler = _CountingHandler(_raising(httpx2.ReadTimeout("")))
        client = _upload_client(handler)
        try:
            with pytest.raises(PaperlessError) as exc_info:
                client.upload_document(sample_pdf, title="Silent timeout")
        finally:
            client.close()
        assert str(exc_info.value).endswith(": ReadTimeout")
        assert len(sleeps) == 2

    def test_unsupported_protocol_url_fails_fast_without_fallback(
        self, sample_pdf: Path, sleeps: list[float]
    ) -> None:
        """
        A URL with no usable scheme is a configuration error, never retried.

        UnsupportedProtocol is a TransportError, so a classifier that tests for
        TransportError first would retry it with backoff.  The message is fixed
        text naming the setting, never httpx2's own words, and it carries no
        cause.
        """
        failure = httpx2.UnsupportedProtocol(_UNSUPPORTED_PROTOCOL_TEXT)
        handler = _CountingHandler(_raising(failure))
        client = _upload_client(handler)
        try:
            with pytest.raises(ConfigError) as exc_info:
                client.upload_document(sample_pdf, title="No scheme")
        finally:
            client.close()
        assert str(exc_info.value) == _UNSET_URL_UPLOAD_MESSAGE
        assert _UNSUPPORTED_PROTOCOL_TEXT not in str(exc_info.value)
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__suppress_context__
        assert handler.calls == 1
        assert sleeps == []

    def test_unsupported_protocol_url_never_takes_the_fallback(
        self, sample_pdf: Path, tmp_path: Path, sleeps: list[float]
    ) -> None:
        """
        A configured consume directory does not turn it into a fallback.

        Every scan would otherwise land in the folder without its metadata and
        be reported as saved; the scan is kept in ``failed/`` by the pipeline
        instead.
        """
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()
        failure = httpx2.UnsupportedProtocol(_UNSUPPORTED_PROTOCOL_TEXT)
        handler = _CountingHandler(_raising(failure))
        client = _upload_client(handler, consume_dir=consume_dir)
        try:
            with pytest.raises(ConfigError):
                client.upload_document(sample_pdf, title="No scheme")
        finally:
            client.close()
        assert handler.calls == 1
        assert sleeps == []
        assert list(consume_dir.iterdir()) == []

    def test_unsupported_protocol_logs_no_attempt_and_copies_nothing(
        self,
        sample_pdf: Path,
        tmp_path: Path,
        sleeps: list[float],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        No retry warning and no consume-folder line is logged for it.

        The configuration error the caller raises is the whole report; a retry
        line would repeat httpx2's text, and a fallback line would claim a copy
        that was never made.
        """
        caplog.set_level(logging.DEBUG, logger="saneless.paperless")
        consume_dir = tmp_path / "consume"
        failure = httpx2.UnsupportedProtocol(_UNSUPPORTED_PROTOCOL_TEXT)
        handler = _CountingHandler(_raising(failure))
        client = _upload_client(handler, consume_dir=consume_dir)
        try:
            with pytest.raises(ConfigError):
                client.upload_document(sample_pdf, title="No scheme")
        finally:
            client.close()
        assert sleeps == []
        assert not consume_dir.exists()
        assert [
            record.getMessage()
            for record in caplog.records
            if record.name == "saneless.paperless" and record.levelno >= logging.INFO
        ] == []

    def test_empty_url_is_a_configuration_error_through_the_real_transport(
        self, sample_pdf: Path, tmp_path: Path, sleeps: list[float]
    ) -> None:
        """
        An unset paperless.url fails at once, names the setting, copies nothing.

        The real transport, not a mock: this is the one way UnsupportedProtocol
        is still reachable once a set URL is checked at load.
        """
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()
        client = PaperlessClient(
            url="", token=_MOCK_AUTH, consume_dir=consume_dir, max_retries=3
        )
        try:
            with pytest.raises(ConfigError) as exc_info:
                client.upload_document(sample_pdf, title="Empty URL")
        finally:
            client.close()
        assert str(exc_info.value) == _UNSET_URL_UPLOAD_MESSAGE
        assert "paperless.url" in str(exc_info.value)
        assert _MOCK_AUTH not in str(exc_info.value)
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__suppress_context__
        assert sleeps == []
        assert list(consume_dir.iterdir()) == []

    @pytest.mark.parametrize(
        "token",
        [
            pytest.param("tok-9e41d2", id="plain"),
            pytest.param("\ttok-5c7b20 ", id="padded"),
        ],
    )
    def test_retry_and_final_messages_redact_the_token(
        self,
        token: str,
        sample_pdf: Path,
        sleeps: list[float],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        Third-party text naming the token is struck before it is interpolated.

        A transport error whose own text carries the token -- as h11's refusal
        of a header value does -- is still retried when it is transient, and
        neither the retry warnings nor the final message may repeat it.  The
        rest of the text survives, so the line still says what happened.
        """
        caplog.set_level(logging.WARNING, logger="saneless.paperless")
        failure = httpx2.ConnectError(f"refused: Token {token} user:pass@h")
        handler = _CountingHandler(_raising(failure))
        client = PaperlessClient(
            url="http://paperless:8000",
            token=token,
            max_retries=3,
            transport=_make_transport(handler),
        )
        try:
            with pytest.raises(PaperlessError) as exc_info:
                client.upload_document(sample_pdf, title="Redacted")
        finally:
            client.close()
        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        ]
        assert len(warnings) == 3
        assert "refused: Token" in str(exc_info.value)
        for text in [str(exc_info.value), *warnings]:
            _assert_token_absent(token, text)

    @pytest.mark.parametrize("consume_name", ["", "consume"], ids=["plain", "fallback"])
    def test_4xx_body_is_rendered_and_never_falls_back(
        self,
        consume_name: str,
        sample_pdf: Path,
        tmp_path: Path,
        sleeps: list[float],
    ) -> None:
        """
        D-09: a 4xx names status, reason and the first field error.

        An empty ``consume_name`` runs without a consume directory; either
        way a rejection is final and nothing is copied.
        """
        consume_dir = tmp_path / "consume"
        handler = _CountingHandler(
            _answering(
                httpx2.Response(400, json={"title": ["This field may not be blank."]})
            )
        )
        client = _upload_client(
            handler, consume_dir=tmp_path / consume_name if consume_name else None
        )
        try:
            with pytest.raises(PaperlessError) as exc_info:
                client.upload_document(sample_pdf, title="")
        finally:
            client.close()
        assert str(exc_info.value) == (
            "Paperless rejected the upload (400 Bad Request): "
            "title: This field may not be blank."
        )
        assert isinstance(exc_info.value.__cause__, httpx2.HTTPStatusError)
        assert handler.calls == 1
        assert sleeps == []
        assert not consume_dir.exists()

    @pytest.mark.parametrize("consume_name", ["", "consume"], ids=["plain", "fallback"])
    def test_redirect_fails_fast_naming_its_target(
        self,
        consume_name: str,
        sample_pdf: Path,
        tmp_path: Path,
        sleeps: list[float],
    ) -> None:
        """
        WR-03: a redirect is not transient, so it is never retried or copied.

        A plain ``http://`` URL behind a proxy that redirects to ``https://``
        would only be redirected again; the message names the target so the
        operator can correct ``paperless.url``.
        """
        consume_dir = tmp_path / "consume"
        target = "https://paperless:8443/api/documents/post_document/"
        handler = _CountingHandler(
            _answering(httpx2.Response(301, headers={"location": target}))
        )
        client = _upload_client(
            handler, consume_dir=tmp_path / consume_name if consume_name else None
        )
        try:
            with pytest.raises(PaperlessError) as exc_info:
                client.upload_document(sample_pdf, title="Redirected")
        finally:
            client.close()
        assert str(exc_info.value) == (
            f"Paperless redirected the upload (301 Moved Permanently) to {target}; "
            "check paperless.url"
        )
        assert isinstance(exc_info.value.__cause__, httpx2.HTTPStatusError)
        assert handler.calls == 1
        assert sleeps == []
        assert not consume_dir.exists()

    def test_non_redirect_3xx_fails_fast_by_its_status(
        self, sample_pdf: Path, sleeps: list[float]
    ) -> None:
        """A 3xx with no target is final too, reported by status and body."""
        handler = _CountingHandler(_answering(httpx2.Response(304)))
        client = _upload_client(handler)
        try:
            with pytest.raises(PaperlessError) as exc_info:
                client.upload_document(sample_pdf, title="Not modified")
        finally:
            client.close()
        assert str(exc_info.value) == (
            "Paperless did not accept the upload (304 Not Modified): "
            "(empty response body)"
        )
        assert handler.calls == 1
        assert sleeps == []

    def test_retry_log_line_is_one_line_without_the_url(
        self,
        sample_pdf: Path,
        sleeps: list[float],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        WR-03: the attempt log renders a 5xx as status and body, on one line.

        httpx2's own text for a status error spans three lines and names the
        full request URL.
        """
        caplog.set_level(logging.WARNING, logger="saneless.paperless")
        handler = _CountingHandler(_answering(httpx2.Response(503, text="down")))
        client = _upload_client(handler)
        try:
            with pytest.raises(PaperlessError):
                client.upload_document(sample_pdf, title="Down")
        finally:
            client.close()
        attempts = [
            record.getMessage()
            for record in caplog.records
            if record.getMessage().startswith("Upload attempt")
        ]
        assert attempts == [
            f"Upload attempt {n}/3 failed: 503 Service Unavailable: down"
            for n in (1, 2, 3)
        ]
        assert len(sleeps) == 2

    def test_5xx_is_retried_then_raises(
        self, sample_pdf: Path, sleeps: list[float]
    ) -> None:
        """A 503 on every attempt exhausts the retries and names the status."""
        handler = _CountingHandler(_answering(httpx2.Response(503, text="down")))
        client = _upload_client(handler)
        try:
            with pytest.raises(
                PaperlessError, match=r"failed after 3 attempts: .*503"
            ) as exc_info:
                client.upload_document(sample_pdf, title="Down")
        finally:
            client.close()
        assert handler.calls == 3
        assert sleeps == [1, 2]
        assert isinstance(exc_info.value.__cause__, httpx2.HTTPStatusError)

    def test_5xx_exhausted_message_is_one_line(
        self, sample_pdf: Path, sleeps: list[float]
    ) -> None:
        """
        EXC-02: httpx2's two-line 5xx text never reaches the message.

        ``str(HTTPStatusError)`` is ``Server error '503 ...' for url '...'``
        followed by a second ``For more information check:`` line, so the
        cause is rendered as status, reason and the one-line body instead.
        """
        handler = _CountingHandler(_answering(httpx2.Response(503, text="down\n")))
        client = _upload_client(handler)
        try:
            with pytest.raises(PaperlessError) as exc_info:
                client.upload_document(sample_pdf, title="Down")
        finally:
            client.close()
        assert str(exc_info.value) == (
            "Upload to Paperless at http://paperless:8000 failed after 3 attempts: "
            "503 Service Unavailable: down"
        )
        assert sleeps == [1, 2]

    def test_5xx_is_retried_then_falls_back(
        self, sample_pdf: Path, tmp_path: Path, sleeps: list[float]
    ) -> None:
        """A 503 on every attempt takes the fallback when one is configured."""
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()
        handler = _CountingHandler(_answering(httpx2.Response(503, text="down")))
        client = _upload_client(handler, consume_dir=consume_dir)
        try:
            result = client.upload_document(sample_pdf, title="Down")
        finally:
            client.close()
        assert handler.calls == 3
        assert sleeps == [1, 2]
        assert result.consume_dir_path == consume_dir / "test.pdf"

    def test_non_json_200_raises_without_retry(
        self, sample_pdf: Path, sleeps: list[float]
    ) -> None:
        """A login page served with 200 is a PaperlessError, not a raw ValueError."""
        handler = _CountingHandler(
            _answering(httpx2.Response(200, text="<html>login</html>"))
        )
        client = _upload_client(handler)
        try:
            with pytest.raises(
                PaperlessError,
                match=(
                    "Paperless at http://paperless:8000 returned a response "
                    "that is not JSON"
                ),
            ) as exc_info:
                client.upload_document(sample_pdf, title="Login page")
        finally:
            client.close()
        assert isinstance(exc_info.value.__cause__, ValueError)
        assert handler.calls == 1
        assert sleeps == []

    def test_unreadable_pdf_is_a_paperless_error(
        self, tmp_path: Path, sleeps: list[float]
    ) -> None:
        """
        IN-08: an OSError opening the PDF leaves as a PaperlessError, too.

        The class promises that whatever goes wrong on the upload path is a
        ``PaperlessError``; a PDF gone from the workspace used to escape as a
        raw ``FileNotFoundError``, which the CLI reports as a saneless bug.
        """
        missing = tmp_path / "gone.pdf"
        handler = _CountingHandler(_answering(httpx2.Response(200, json="task-id")))
        client = _upload_client(handler, consume_dir=tmp_path / "consume")
        try:
            with pytest.raises(PaperlessError) as exc_info:
                client.upload_document(missing, title="Gone")
        finally:
            client.close()
        assert str(exc_info.value) == (
            f"Could not read the PDF {missing} to upload it: No such file or directory"
        )
        assert isinstance(exc_info.value.__cause__, FileNotFoundError)
        assert handler.calls == 0
        assert sleeps == []

    def test_other_http_error_raises_at_once(
        self, sample_pdf: Path, sleeps: list[float]
    ) -> None:
        """Any remaining httpx2.HTTPError (TooManyRedirects) is wrapped and chained."""
        failure = httpx2.TooManyRedirects("Exceeded maximum allowed redirects.")
        handler = _CountingHandler(_raising(failure))
        client = _upload_client(handler)
        try:
            with pytest.raises(
                PaperlessError,
                match=re.escape(
                    "Could not upload to Paperless at http://paperless:8000: "
                    "Exceeded maximum allowed redirects."
                ),
            ) as exc_info:
                client.upload_document(sample_pdf, title="Loop")
        finally:
            client.close()
        assert exc_info.value.__cause__ is failure
        assert handler.calls == 1
        assert sleeps == []


# ---------------------------------------------------------------------------
# Error body renderer
# ---------------------------------------------------------------------------


_JSON_BODY_CASES = [
    pytest.param(
        {"detail": "Authentication credentials were not provided."},
        "Authentication credentials were not provided.",
        id="detail-str",
    ),
    pytest.param({"detail": ["a", "b"]}, "a b", id="detail-list"),
    pytest.param(
        {"title": ["This field may not be blank."]},
        "title: This field may not be blank.",
        id="field-list",
    ),
    pytest.param(
        {"non_field_errors": ["Bad combo"]},
        "non_field_errors: Bad combo",
        id="non-field-errors",
    ),
    pytest.param(
        {"title": "This field may not be blank."},
        "title: This field may not be blank.",
        id="field-str",
    ),
    pytest.param(["first", "second"], "first", id="top-level-list"),
]


class TestRenderErrorBody:
    """D-09: one renderer reduces every Paperless error body to one line."""

    @pytest.mark.parametrize(("payload", "expected"), _JSON_BODY_CASES)
    def test_json_body_shapes(self, payload: object, expected: str) -> None:
        """
        Each DRF error shape renders as the one line a reader needs.

        ``detail`` wins, then the first field error as ``field: message``,
        then the first entry of a top-level list.
        """
        response = httpx2.Response(400, json=payload)
        assert _render_error_body(response, _MOCK_AUTH) == expected

    def test_html_body_is_one_bounded_line(self) -> None:
        """
        T-23-16 / M-17: a 5000-character HTML page cannot flood job.error.

        Whitespace is collapsed so no newline survives, and the text is cut
        to 200 characters plus an ellipsis.
        """
        line = "<p>502 Bad Gateway from the reverse proxy</p>\n"
        html = (line * (5000 // len(line) + 1))[:5000]
        response = httpx2.Response(502, text=html)
        result = _render_error_body(response, _MOCK_AUTH)
        assert "\n" not in result
        assert len(result) <= 201
        assert result.endswith("…")

    @pytest.mark.parametrize(
        "response",
        [
            pytest.param(
                httpx2.Response(502, text="<p>\x1b]0;owned\x07Bad \x9bGateway</p>"),
                id="text-body",
            ),
            pytest.param(
                httpx2.Response(400, json={"detail": "bad\x1b[2Jrequest\x00"}),
                id="json-detail",
            ),
        ],
    )
    def test_body_controls_are_shown_as_escapes(
        self, response: httpx2.Response
    ) -> None:
        """
        ESC, BEL, NUL and a C1 CSI in a body never reach the message live.

        None of them is whitespace, so collapsing whitespace alone keeps
        them, and the message is printed on the terminal and logged.
        """
        result = _render_error_body(response, _MOCK_AUTH)
        assert not has_control_characters(result)
        assert "\\x1b" in result

    @pytest.mark.parametrize(
        "render",
        [
            pytest.param(
                lambda response: _one_line_reason(
                    httpx2.HTTPStatusError(
                        "refused",
                        request=httpx2.Request("POST", "http://paperless.test/"),
                        response=response,
                    ),
                    _MOCK_AUTH,
                ),
                id="status-error",
            ),
            pytest.param(
                lambda response: _not_accepted_message(response, _MOCK_AUTH),
                id="upload-not-accepted",
            ),
            pytest.param(
                lambda response: _failed_poll_message(lambda _call: response),
                id="task-poll",
            ),
        ],
    )
    def test_reason_phrase_controls_are_shown_as_escapes(
        self, render: Callable[[httpx2.Response], str]
    ) -> None:
        """
        The status line's reason phrase is upstream text like the body.

        HTTP allows any control but NUL, CR and LF in a reason phrase, so a
        misbehaving server or proxy can put a terminal escape there, and the
        message it lands in is printed and logged.
        """
        response = httpx2.Response(
            400,
            json={"detail": "no"},
            extensions={"reason_phrase": b"Bad\x1b]0;owned\x07\x1b[31mRed"},
        )
        result = render(response)
        assert not has_control_characters(result)
        assert "400 Bad\\x1b]0;owned\\x07\\x1b[31mRed" in result

    def test_empty_body_says_so(self) -> None:
        """An empty body renders as an explicit marker, never an empty string."""
        response = httpx2.Response(500, text="")
        assert _render_error_body(response, _MOCK_AUTH) == "(empty response body)"

    def test_multiline_json_detail_body_is_collapsed(self) -> None:
        """No path yields a newline, including JSON-derived text (T-28-24)."""
        response = httpx2.Response(400, json={"detail": "line one\nline two"})
        assert _render_error_body(response, _MOCK_AUTH) == "line one line two"

    def test_full_body_is_logged_at_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The whole body stays diagnosable, but only at DEBUG (T-28-21)."""
        body = "<html>" + ("x" * 900) + "</html>"
        response = httpx2.Response(502, text=body)
        with caplog.at_level(logging.DEBUG, logger="saneless.paperless"):
            _render_error_body(response, _MOCK_AUTH)
        debug_messages = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.DEBUG and record.name == "saneless.paperless"
        ]
        assert any(body in message for message in debug_messages)


# ---------------------------------------------------------------------------
# Poll task tests
# ---------------------------------------------------------------------------


class TestPollTask:
    """Task polling tests."""

    @pytest.mark.parametrize("build_payload", _API_SHAPES)
    def test_success_across_api_versions(
        self,
        build_payload: Callable[[str, str | None], object],
    ) -> None:
        """A completed task is reached on both the v9 and the v10 wire shape."""

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=build_payload("SUCCESS", None))

        client = _poll_client(handler)
        try:
            result = client.poll_task("t1", timeout=10)
            assert str(result["status"]).upper() == "SUCCESS"
        finally:
            client.close()

    @pytest.mark.parametrize("build_payload", _API_SHAPES)
    def test_failure_raises_across_api_versions(
        self,
        build_payload: Callable[[str, str | None], object],
    ) -> None:
        """A failed task raises, carrying the message Paperless supplied."""

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=build_payload("FAILURE", "disk on fire"))

        client = _poll_client(handler)
        try:
            with pytest.raises(PaperlessError, match="disk on fire"):
                client.poll_task("t1", timeout=10)
        finally:
            client.close()

    @pytest.mark.parametrize("build_payload", _API_SHAPES)
    def test_revoked_raises_across_api_versions(
        self,
        build_payload: Callable[[str, str | None], object],
    ) -> None:
        """
        REVOKED is terminal, not something to keep polling.

        It is one of paperless-ngx's COMPLETE_STATUSES, so looping on it
        would turn an administrative cancellation into a misattributed
        timeout that also blocks the worker for the whole budget.
        """

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=build_payload("REVOKED", "cancelled"))

        client = _poll_client(handler)
        try:
            with pytest.raises(PaperlessError, match="cancelled"):
                client.poll_task("t1", timeout=10)
        finally:
            client.close()

    @pytest.mark.parametrize(("build_payload", "build_no_task"), _API_NO_TASK_SHAPES)
    def test_no_task_yet_is_tolerated_across_api_versions(
        self,
        build_payload: Callable[[str, str | None], object],
        build_no_task: Callable[[], object],
    ) -> None:
        """Pitfall #8: a 200 with no task keeps polling rather than raising."""
        call_count = {"n": 0}

        def handler(_request: httpx2.Request) -> httpx2.Response:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return httpx2.Response(200, json=build_no_task())
            return httpx2.Response(200, json=build_payload("SUCCESS", None))

        client = _poll_client(handler)
        try:
            result = client.poll_task("t1", timeout=30)
            assert str(result["status"]).upper() == "SUCCESS"
            assert call_count["n"] >= 2
        finally:
            client.close()

    def test_failure_without_a_message_still_raises(self) -> None:
        """A failure carrying neither field raises with a stand-in message."""

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=[{"status": "FAILURE", "task_id": "t1"}])

        client = _poll_client(handler)
        try:
            with pytest.raises(PaperlessError, match="no message"):
                client.poll_task("t1", timeout=10)
        finally:
            client.close()

    def test_non_200_raises_on_the_first_poll(self) -> None:
        """
        D-12: any non-200 is a hard failure, reported immediately.

        The counter is the point of the test: a revoked token used to be
        silently re-polled for the full 300 s and then misreported as a
        timeout.
        """
        call_count = {"n": 0}

        def handler(_request: httpx2.Request) -> httpx2.Response:
            call_count["n"] += 1
            return httpx2.Response(401, text="Invalid token")

        client = _poll_client(handler)
        try:
            with pytest.raises(PaperlessError, match="401"):
                client.poll_task("t1", timeout=30)
            assert call_count["n"] == 1
        finally:
            client.close()

    def test_non_200_body_is_rendered_as_one_line(self) -> None:
        """
        D-09: a poll non-200 names status, reason and the DRF detail.

        The body goes through the same single renderer as the upload 4xx, so
        a revoked token reads ``(401 Unauthorized): Invalid token.`` rather
        than a raw JSON blob.
        """

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(401, json={"detail": "Invalid token."})

        client = _poll_client(handler)
        try:
            with pytest.raises(PaperlessError) as exc_info:
                client.poll_task("t1", timeout=30)
            assert str(exc_info.value) == (
                "Paperless task poll failed (401 Unauthorized): Invalid token."
            )
        finally:
            client.close()

    def test_non_200_html_body_cannot_flood_the_message(self) -> None:
        """
        T-23-16 / M-17: a 5 KB proxy error page becomes one short line.

        That message is recorded in the job store and shown in the web status
        area and on the terminal, so neither its length nor a newline may
        come from the upstream body.
        """
        page = "<html>\n<body>\n" + ("<p>Bad Gateway</p>\n" * 260) + "</body></html>"

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(502, text=page)

        client = _poll_client(handler)
        try:
            with pytest.raises(PaperlessError) as exc_info:
                client.poll_task("t1", timeout=30)
            message = str(exc_info.value)
            assert "\n" not in message
            assert len(message) < 300
        finally:
            client.close()

    def test_timeout_raises_naming_the_task(self) -> None:
        """A deadline-expired poll raises and names the task id."""

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=[{"status": "PENDING", "task_id": "t1"}])

        client = _poll_client(handler)
        try:
            with pytest.raises(PaperlessTimeoutError, match="t1"):
                client.poll_task("t1", timeout=0.05)
        finally:
            client.close()

    def test_timeout_is_catchable_as_a_paperless_error(self) -> None:
        """D-11: `except PaperlessError` catches the timeout subclass too."""
        assert issubclass(PaperlessTimeoutError, PaperlessError)

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=[{"status": "PENDING", "task_id": "t1"}])

        client = _poll_client(handler)
        try:
            with pytest.raises(PaperlessError):
                client.poll_task("t1", timeout=0.05)
        finally:
            client.close()

    def test_timeout_does_not_sleep_past_its_own_deadline(self) -> None:
        """
        A 0.05 s budget costs 0.05 s, not the 0.5 s first sleep.

        0.5 s is exactly what the unclamped first backoff sleep cost
        unconditionally, so that threshold is the regression this asserts.
        """

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=[{"status": "PENDING", "task_id": "t1"}])

        client = _poll_client(handler)
        try:
            start = time.monotonic()
            with pytest.raises(PaperlessTimeoutError):
                client.poll_task("t1", timeout=0.05)
            assert time.monotonic() - start < 0.5
        finally:
            client.close()


_POLL_TRANSPORT_CASES = [
    pytest.param(httpx2.ConnectError("connection refused"), id="connect-error"),
    pytest.param(httpx2.ReadTimeout(""), id="read-timeout-empty"),
    pytest.param(
        httpx2.RemoteProtocolError("Server disconnected without sending a response."),
        id="remote-protocol-error",
    ),
    # A RequestError but not a TransportError: a proxy sending a corrupt gzip
    # body.  It must not escape poll_task raw or fail the accepted upload (WR-05).
    pytest.param(
        httpx2.DecodingError("Error -3 while decompressing data"),
        id="decoding-error",
    ),
]

_DUPLICATE_SENTENCE = (
    "the document may already be in Paperless; check before scanning again"
)


def _task_answer(task: dict[str, object]) -> Callable[[int], httpx2.Response]:
    """Build a script that answers every poll with a v9 list holding ``task``."""
    return _answering(httpx2.Response(200, json=[task]))


def _failed_poll_message(respond: Callable[[int], httpx2.Response]) -> str:
    """Poll ``t1`` against ``respond`` and return the PaperlessError text."""
    client = _poll_client(_CountingHandler(respond))
    try:
        with pytest.raises(PaperlessError) as exc_info:
            client.poll_task("t1", timeout=10)
    finally:
        client.close()
    return str(exc_info.value)


class TestPollTaskFailureTranslation:
    """EXC-01 / D-10 / D-11 / M-17: the read side of an accepted upload."""

    def test_poll_continues_through_transport_errors_to_success(
        self, sleeps: list[float]
    ) -> None:
        """
        D-11 / M-17: a network blip after the upload succeeded is not a failure.

        The upload was accepted, so failing the job now invites a rescan and
        a duplicate document.  Two ReadErrors are followed by SUCCESS, and the
        poll backs off between them exactly as it does for a pending task.
        """

        def respond(call: int) -> httpx2.Response:
            if call <= 2:
                msg = "reset"
                raise httpx2.ReadError(msg)
            return httpx2.Response(200, json=[{"task_id": "t1", "status": "SUCCESS"}])

        handler = _CountingHandler(respond)
        client = _poll_client(handler)
        try:
            result = client.poll_task("t1", timeout=5)
        finally:
            client.close()
        assert result["status"] == "SUCCESS"
        assert handler.calls == 3
        assert sleeps == [0.5, 1.0]

    @pytest.mark.parametrize("failure", _POLL_TRANSPORT_CASES)
    def test_poll_deadline_after_transport_errors_names_the_last_error(
        self, failure: httpx2.RequestError, sleeps: list[float]
    ) -> None:
        """
        D-11 / OUTC-07 / T-28-38: transport errors still end at the deadline.

        The sleep recorder advances no time, so only the real monotonic
        deadline can end the loop; a poll that skipped the deadline check on
        a transport error would hang here.  The message names the task and
        ``describe`` of the last error (the class name for an empty one).
        """
        handler = _CountingHandler(_raising(failure))
        client = _poll_client(handler)
        try:
            with pytest.raises(PaperlessTimeoutError) as exc_info:
                client.poll_task("t1", timeout=0.05)
        finally:
            client.close()
        message = str(exc_info.value)
        assert message == (
            "Paperless task t1 did not finish within 0.05s; "
            f"last error: {describe(failure)}"
        )
        assert exc_info.value.__cause__ is failure
        assert "\n" not in message
        # Every poll but the one that found the deadline gone was followed by
        # a sleep: the error fell through to the backoff, not a bare `continue`.
        assert handler.calls >= 1
        assert len(sleeps) == handler.calls - 1

    def test_poll_continues_through_a_decoding_error_to_success(
        self, sleeps: list[float]
    ) -> None:
        """
        WR-05: an undecodable poll response is a blip, not a raw httpx2 escape.

        ``httpx2.DecodingError`` is a ``RequestError`` but not a
        ``TransportError``; the upload was already accepted, so the poll keeps
        asking within its deadline (D-11).
        """

        def respond(call: int) -> httpx2.Response:
            if call == 1:
                msg = "Error -3 while decompressing data"
                raise httpx2.DecodingError(msg)
            return httpx2.Response(200, json=[{"task_id": "t1", "status": "SUCCESS"}])

        handler = _CountingHandler(respond)
        client = _poll_client(handler)
        try:
            result = client.poll_task("t1", timeout=5)
        finally:
            client.close()
        assert result["status"] == "SUCCESS"
        assert handler.calls == 2
        assert sleeps == [0.5]

    def test_poll_deadline_without_transport_error_has_no_last_error(
        self, sleeps: list[float]
    ) -> None:
        """A task that simply stays PENDING keeps today's timeout message."""
        client = _poll_client(
            _CountingHandler(_task_answer({"task_id": "t1", "status": "PENDING"}))
        )
        try:
            with pytest.raises(PaperlessTimeoutError) as exc_info:
                client.poll_task("t1", timeout=0.05)
        finally:
            client.close()
        assert str(exc_info.value) == "Paperless task t1 did not finish within 0.05s"
        assert exc_info.value.__cause__ is None
        assert sleeps

    def test_poll_deadline_after_a_recovered_blip_names_no_stale_error(
        self, sleeps: list[float]
    ) -> None:
        """
        IN-02: a transport error followed by answered polls is not the cause.

        One blip early in a poll that then simply waited on a slow task must
        not end with "last error: ...", which would blame the network.
        """

        def respond(call: int) -> httpx2.Response:
            if call == 1:
                msg = "connection refused"
                raise httpx2.ConnectError(msg)
            return httpx2.Response(200, json=[{"task_id": "t1", "status": "PENDING"}])

        handler = _CountingHandler(respond)
        client = _poll_client(handler)
        try:
            with pytest.raises(PaperlessTimeoutError) as exc_info:
                client.poll_task("t1", timeout=0.05)
        finally:
            client.close()
        assert str(exc_info.value) == "Paperless task t1 did not finish within 0.05s"
        assert exc_info.value.__cause__ is None
        assert handler.calls >= 2
        assert sleeps

    def test_poll_401_still_fails_at_once(self, sleeps: list[float]) -> None:
        """OUTC-07: a non-200 is not a transport blip and ends the poll at once."""
        handler = _CountingHandler(_answering(httpx2.Response(401, text="Invalid")))
        client = _poll_client(handler)
        try:
            with pytest.raises(PaperlessError, match="401 Unauthorized"):
                client.poll_task("t1", timeout=30)
        finally:
            client.close()
        assert handler.calls == 1
        assert sleeps == []

    def test_poll_non_json_200_is_a_paperless_error(self, sleeps: list[float]) -> None:
        """EXC-01: a login page served with 200 is not a raw ValueError."""
        handler = _CountingHandler(_answering(httpx2.Response(200, text="<html>")))
        client = _poll_client(handler)
        try:
            with pytest.raises(
                PaperlessError,
                match=(
                    "Paperless at http://paperless:8000 returned a task response "
                    "that is not JSON"
                ),
            ) as exc_info:
                client.poll_task("t1", timeout=30)
        finally:
            client.close()
        assert isinstance(exc_info.value.__cause__, ValueError)
        assert handler.calls == 1
        assert sleeps == []

    def test_duplicate_v9_failure_says_check_before_rescanning(self) -> None:
        """D-10: the v9 duplicate text gets the check-first sentence."""
        text = "Not consuming: It is a duplicate of document #42"
        message = _failed_poll_message(
            _task_answer({"task_id": "t1", "status": "FAILURE", "result": text})
        )
        assert (
            message == f"Paperless task t1 ended FAILURE: {text}; {_DUPLICATE_SENTENCE}"
        )

    def test_duplicate_v2_failure_matches_case_insensitively(self) -> None:
        """D-10: the paperless-ngx 2.x spelling capitalises "Duplicate"."""
        text = "Not consuming scan.pdf: It is a Duplicate of Invoice (#7)."
        message = _failed_poll_message(
            _task_answer({"task_id": "t1", "status": "FAILURE", "result": text})
        )
        assert text in message
        assert message.endswith(f"; {_DUPLICATE_SENTENCE}")

    def test_duplicate_v10_result_data_without_message(self) -> None:
        """D-10: v10 reports a duplicate only as ``result_data.duplicate_of``."""
        payload = {
            "count": 1,
            "next": None,
            "previous": None,
            "results": [
                {
                    "task_id": "t1",
                    "status": "failure",
                    "result_data": {"duplicate_of": 42, "duplicate_in_trash": False},
                }
            ],
        }
        message = _failed_poll_message(_answering(httpx2.Response(200, json=payload)))
        assert message == (
            "Paperless task t1 ended FAILURE: Paperless reported a failure but "
            f"supplied no message; {_DUPLICATE_SENTENCE}"
        )

    def test_non_duplicate_failure_has_no_duplicate_sentence(self) -> None:
        """D-10: an ordinary failure is not dressed up as a duplicate."""
        message = _failed_poll_message(
            _task_answer(
                {"task_id": "t1", "status": "FAILURE", "result": "disk on fire"}
            )
        )
        assert message == "Paperless task t1 ended FAILURE: disk on fire"

    def test_poll_failure_text_is_one_line(self) -> None:
        """EXC-02: a multi-line failure text from Paperless is one message line."""
        message = _failed_poll_message(
            _task_answer(
                {"task_id": "t1", "status": "FAILURE", "result": "bad\nthings\r\n"}
            )
        )
        assert message == "Paperless task t1 ended FAILURE: bad things"

    def test_poll_failure_text_shows_controls_as_escapes(self) -> None:
        """A terminal escape in a task failure reaches job.error as visible text."""
        message = _failed_poll_message(
            _task_answer(
                {
                    "task_id": "t1",
                    "status": "FAILURE",
                    "result": "OCR \x1b]0;owned\x07failed\x1b[2J",
                }
            )
        )
        assert not has_control_characters(message)
        assert message == (
            "Paperless task t1 ended FAILURE: OCR \\x1b]0;owned\\x07failed\\x1b[2J"
        )

    def test_poll_failure_text_is_length_bounded(self) -> None:
        """
        IN-05 / T-23-16: a long task failure cannot flood job.error or the CLI.

        Paperless failure results can embed whole OCR or consumer tracebacks;
        they are cut like an error body, with an ellipsis.
        """
        message = _failed_poll_message(
            _task_answer({"task_id": "t1", "status": "FAILURE", "result": "x" * 500})
        )
        assert message == f"Paperless task t1 ended FAILURE: {'x' * 200}…"

    def test_duplicate_past_the_cut_still_says_check_before_rescanning(self) -> None:
        """The duplicate check reads the whole failure text, not the cut one."""
        text = f"{'consumer output ' * 20}It is a duplicate of document #42"
        message = _failed_poll_message(
            _task_answer({"task_id": "t1", "status": "FAILURE", "result": text})
        )
        assert "duplicate of document" not in message
        assert message.endswith(f"…; {_DUPLICATE_SENTENCE}")

    def test_whitespace_only_failure_text_still_says_something(self) -> None:
        """A failure text of only whitespace is as empty as none."""
        message = _failed_poll_message(
            _task_answer({"task_id": "t1", "status": "FAILURE", "result": " \n "})
        )
        assert message == (
            "Paperless task t1 ended FAILURE: Paperless reported a failure but "
            "supplied no message"
        )


# ---------------------------------------------------------------------------
# Metadata fetches
# ---------------------------------------------------------------------------


_METADATA_METHODS = [
    pytest.param("get_tags", "tags", id="tags"),
    pytest.param("get_correspondents", "correspondents", id="correspondents"),
]

# (script, expected message suffix or None for ``describe(cause)``, cause type)
_METADATA_FAILURES = [
    pytest.param(
        _raising(httpx2.ConnectError("connection refused")),
        "connection refused",
        httpx2.ConnectError,
        id="connect-error",
    ),
    pytest.param(
        _raising(httpx2.ReadTimeout("")),
        "ReadTimeout",
        httpx2.ReadTimeout,
        id="read-timeout-empty",
    ),
    pytest.param(
        _answering(httpx2.Response(500, text="boom")),
        "500 Internal Server Error: boom",
        httpx2.HTTPStatusError,
        id="500",
    ),
    pytest.param(
        _answering(
            httpx2.Response(403, json={"detail": "You do not have permission."})
        ),
        "403 Forbidden: You do not have permission.",
        httpx2.HTTPStatusError,
        id="403",
    ),
    pytest.param(
        _answering(httpx2.Response(200, text="<html>login</html>")),
        None,
        ValueError,
        id="non-json-200",
    ),
]


def _metadata_client(handler: _CountingHandler) -> PaperlessClient:
    """Build a client for the metadata tests on a distinct base URL."""
    return PaperlessClient(
        url="http://paperless.test:8000",
        token=_MOCK_AUTH,
        transport=_make_transport(handler),
    )


class TestMetadataFetchTranslation:
    """EXC-01 / D-11: get_tags and get_correspondents raise only PaperlessError."""

    @pytest.mark.parametrize(("method", "noun"), _METADATA_METHODS)
    @pytest.mark.parametrize(("respond", "suffix", "cause_type"), _METADATA_FAILURES)
    def test_metadata_failure_is_a_paperless_error(
        self,
        method: str,
        noun: str,
        respond: Callable[[int], httpx2.Response],
        suffix: str | None,
        cause_type: type[Exception],
    ) -> None:
        """
        Every failure names the endpoint and base URL and keeps the cause.

        A status error is rendered as status, reason and the one-line body
        rather than httpx2's two-line text, so the message stays one line
        (EXC-02); a non-JSON body ends with ``describe`` of the ValueError.
        """
        handler = _CountingHandler(respond)
        client = _metadata_client(handler)
        try:
            with pytest.raises(PaperlessError) as exc_info:
                getattr(client, method)()
        finally:
            client.close()
        error = exc_info.value
        cause = error.__cause__
        assert isinstance(cause, cause_type)
        assert not isinstance(error, httpx2.HTTPError)
        expected_suffix = describe(cause) if suffix is None else suffix
        assert str(error) == (
            f"Could not fetch {noun} from Paperless at http://paperless.test:8000: "
            f"{expected_suffix}"
        )
        assert "\n" not in str(error)
        assert handler.calls == 1

    @pytest.mark.parametrize(("method", "noun"), _METADATA_METHODS)
    def test_empty_url_metadata_message_names_no_empty_address(
        self, method: str, noun: str
    ) -> None:
        """With no address to name, the message leaves the "at <url>" out."""
        client = PaperlessClient(url="", token=_MOCK_AUTH)
        try:
            with pytest.raises(ConfigError) as exc_info:
                getattr(client, method)()
        finally:
            client.close()
        assert str(exc_info.value) == (
            f"Could not fetch {noun} from Paperless: paperless.url is not set, or "
            "has no http or https scheme; set it to the paperless-ngx address"
        )

    @pytest.mark.parametrize(("method", "noun"), _METADATA_METHODS)
    def test_metadata_list_response_is_returned(self, method: str, noun: str) -> None:
        """A bare-list response is returned unchanged."""
        items = [{"id": 1, "name": f"first {noun}"}]
        handler = _CountingHandler(_answering(httpx2.Response(200, json=items)))
        client = _metadata_client(handler)
        try:
            assert getattr(client, method)() == items
        finally:
            client.close()
        assert handler.calls == 1

    @pytest.mark.parametrize(("method", "noun"), _METADATA_METHODS)
    def test_metadata_paginated_response_is_unwrapped(
        self, method: str, noun: str
    ) -> None:
        """A paginated dict response yields its ``results``."""
        items = [{"id": 2, "name": f"second {noun}"}]
        payload = {"count": 1, "next": None, "previous": None, "results": items}
        handler = _CountingHandler(_answering(httpx2.Response(200, json=payload)))
        client = _metadata_client(handler)
        try:
            assert getattr(client, method)() == items
        finally:
            client.close()


# Tokens h11 refuses to put in a header: edge whitespace, and a control
# character.  Neither id nor any test name here contains a token's text,
# because tmp_path is named after the test and job text names paths under it.
_UNSENDABLE_TOKENS = [
    pytest.param("abc ", id="trailing-space"),
    pytest.param("tok-7f3a9c\r", id="carriage-return"),
]

_HTTP_LIBRARIES = frozenset({"httpx2", "httpcore2"})


def _is_http_library_record(record: logging.LogRecord) -> bool:
    """
    Tell whether a log record came from httpx2 or httpcore2.

    Args:
        record: The captured record.

    Returns:
        True for a record from either library's logger or one of its children.

    """
    return record.name.split(".", 1)[0] in _HTTP_LIBRARIES


@pytest.fixture
def debug_capture(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """
    Log as ``output.log_level = "DEBUG"`` does, and capture every record.

    Yields:
        Nothing; the logging configuration is undone after the test.

    """
    # caplog first: it restores the root level it found at teardown, which
    # runs after this fixture's, so it must find the level from before the
    # production configuration lowered it.
    caplog.set_level(logging.DEBUG)
    with production_debug_logging():
        yield


class TestLoopbackClientSideProtocolErrors:
    """
    A token the transport refuses to send is a configuration error, end to end.

    Over a real socket, because ``httpx2.MockTransport`` replaces the layer
    that refuses: h11 raises ``LocalProtocolError`` with the header value --
    the token -- in its text.  That error is a ``TransportError``, so without
    its own classification it would be retried, logged on every attempt,
    copied to the consume folder and stored in the job's error.
    """

    @pytest.mark.parametrize("token", _UNSENDABLE_TOKENS)
    @pytest.mark.usefixtures("debug_capture")
    def test_upload_refuses_without_retry_copy_or_leak(
        self,
        token: str,
        sample_pdf: Path,
        tmp_path: Path,
        sleeps: list[float],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Nothing reaches the server, nothing is retried, copied or echoed."""
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()
        with loopback_paperless() as server:
            client = PaperlessClient(
                url=server.url, token=token, consume_dir=consume_dir, max_retries=3
            )
            try:
                with pytest.raises(ConfigError) as exc_info:
                    client.upload_document(sample_pdf, title="Refused header")
            finally:
                client.close()
        error = exc_info.value
        assert str(error) == f"Uploading to Paperless: {_UNSENDABLE_REQUEST_REASON}"
        # A chained cause would put h11's text, and so the token, into the
        # worker's traceback however clean the message is.
        assert error.__cause__ is None
        assert error.__suppress_context__
        assert server.hits == []
        assert sleeps == []
        assert list(consume_dir.iterdir()) == []
        for text in (str(error), repr(error), caplog.text):
            _assert_token_absent(token, text)

    @pytest.mark.parametrize("token", _UNSENDABLE_TOKENS)
    @pytest.mark.parametrize(("method", "noun"), _METADATA_METHODS)
    @pytest.mark.usefixtures("debug_capture")
    def test_metadata_fetch_refuses_without_leak(
        self,
        method: str,
        noun: str,
        token: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Tag and correspondent fetches take the same arm, with the same text."""
        with loopback_paperless() as server:
            client = PaperlessClient(url=server.url, token=token)
            try:
                with pytest.raises(ConfigError) as exc_info:
                    getattr(client, method)()
            finally:
                client.close()
        error = exc_info.value
        assert str(error) == (
            f"Could not fetch {noun} from Paperless at {server.url}: "
            f"{_UNSENDABLE_REQUEST_REASON}"
        )
        assert error.__cause__ is None
        assert error.__suppress_context__
        assert server.hits == []
        for text in (str(error), repr(error), caplog.text):
            _assert_token_absent(token, text)

    @pytest.mark.usefixtures("debug_capture")
    def test_valid_token_is_sent_once_and_no_library_debug_is_logged(
        self, sample_pdf: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        The control: the same server takes a sendable token, under DEBUG.

        It proves the refusals above are the token's doing, and that a real
        request at ``log_level = "DEBUG"`` produces no httpcore2 or httpx2
        DEBUG record -- the trace that prints request and response headers --
        and so no ``Set-Cookie`` value in the log.
        """
        with loopback_paperless() as server:
            client = PaperlessClient(url=server.url, token=_MOCK_AUTH)
            try:
                result = client.upload_document(sample_pdf, title="Control")
                task = client.poll_task(LOOPBACK_TASK_ID, timeout=5)
            finally:
                client.close()
        assert result == UploadResult(delivered_to_api=True, task_uuid=LOOPBACK_TASK_ID)
        assert task["status"] == "SUCCESS"
        assert server.hits == [
            LoopbackHit("POST", DOCUMENTS_PATH, f"Token {_MOCK_AUTH}"),
            LoopbackHit("GET", TASKS_PATH, f"Token {_MOCK_AUTH}"),
        ]
        library_records = [r for r in caplog.records if _is_http_library_record(r)]
        # httpx2's INFO request line is captured, so the libraries' records do
        # reach caplog and the DEBUG check below can fail.
        assert library_records != []
        assert [r for r in library_records if r.levelno <= logging.DEBUG] == []
        assert LOOPBACK_SESSION_COOKIE not in caplog.text


# What h11 says when a request body does not match its declared length: a
# LocalProtocolError that has nothing to do with the configured URL or token.
_LENGTH_MISMATCH = "Too much data for declared Content-Length"

# The fixed reason for a request the HTTP library refused for another cause.
_REQUEST_REFUSED_REASON = (
    "the request could not be sent: the HTTP library refused it (LocalProtocolError)"
)


class TestUnsendableRequestWithASendableConfiguration:
    """
    A request refused for a reason that is not the configuration says so.

    Load validation makes a URL or token h11 would refuse impossible, so a
    ``LocalProtocolError`` from a client built with sendable values has
    another cause.  Blaming ``paperless.url`` and ``paperless.token`` would send
    the operator to edit a configuration file that is fine.
    """

    def test_upload_is_a_paperless_error_without_retry_or_copy(
        self, sample_pdf: Path, tmp_path: Path, sleeps: list[float]
    ) -> None:
        """No retry, no consume-folder copy, and no configuration named."""
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()
        handler = _CountingHandler(
            _raising(httpx2.LocalProtocolError(_LENGTH_MISMATCH))
        )
        client = _upload_client(handler, consume_dir=consume_dir)
        try:
            with pytest.raises(PaperlessError) as exc_info:
                client.upload_document(sample_pdf, title="Length")
        finally:
            client.close()
        assert not isinstance(exc_info.value, ConfigError)
        assert str(exc_info.value) == (
            f"Uploading to Paperless: {_REQUEST_REFUSED_REASON}"
        )
        assert "paperless.url" not in str(exc_info.value)
        assert handler.calls == 1
        assert sleeps == []
        assert list(consume_dir.iterdir()) == []

    @pytest.mark.parametrize(("method", "noun"), _METADATA_METHODS)
    def test_metadata_fetch_is_a_paperless_error(self, method: str, noun: str) -> None:
        """Tag and correspondent fetches give the same verdict."""
        handler = _CountingHandler(
            _raising(httpx2.LocalProtocolError(_LENGTH_MISMATCH))
        )
        client = _metadata_client(handler)
        try:
            with pytest.raises(PaperlessError) as exc_info:
                getattr(client, method)()
        finally:
            client.close()
        assert not isinstance(exc_info.value, ConfigError)
        assert str(exc_info.value) == (
            f"Could not fetch {noun} from Paperless at http://paperless.test:8000: "
            f"{_REQUEST_REFUSED_REASON}"
        )


_REDACTED_TOKENS = [
    pytest.param("tok-9e41d2", id="plain"),
    pytest.param("\ttok-5c7b20 ", id="padded"),
]


def _token_bearing_failure(token: str) -> httpx2.ConnectError:
    """
    Build a transport error whose own text carries the token.

    Args:
        token: The configured token.

    Returns:
        A ConnectError naming the token as a library message might.

    """
    return httpx2.ConnectError(f"refused: Token {token} user:pass@h")


class TestThirdPartyTextIsRedacted:
    """Every place the client quotes library text strikes the token out first."""

    @pytest.mark.parametrize("token", _REDACTED_TOKENS)
    def test_poll_warning_and_timeout_redact_the_token(
        self, token: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed poll's warning and the timeout that follows it are clean."""
        caplog.set_level(logging.WARNING, logger="saneless.paperless")
        handler = _CountingHandler(_raising(_token_bearing_failure(token)))
        client = PaperlessClient(
            url="http://paperless:8000", token=token, transport=_make_transport(handler)
        )
        try:
            with pytest.raises(PaperlessTimeoutError) as exc_info:
                client.poll_task("t1", timeout=0)
        finally:
            client.close()
        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        ]
        assert any("refused: Token" in message for message in warnings)
        assert "refused: Token" in str(exc_info.value)
        for text in [str(exc_info.value), *warnings]:
            _assert_token_absent(token, text)

    @pytest.mark.parametrize("token", _REDACTED_TOKENS)
    @pytest.mark.parametrize(("method", "noun"), _METADATA_METHODS)
    def test_metadata_failure_redacts_the_token(
        self, method: str, noun: str, token: str
    ) -> None:
        """A failed tag or correspondent fetch names the failure, not the token."""
        handler = _CountingHandler(_raising(_token_bearing_failure(token)))
        client = PaperlessClient(
            url="http://paperless:8000", token=token, transport=_make_transport(handler)
        )
        try:
            with pytest.raises(PaperlessError) as exc_info:
                getattr(client, method)()
        finally:
            client.close()
        assert str(exc_info.value).startswith(f"Could not fetch {noun} from Paperless")
        assert "refused: Token" in str(exc_info.value)
        _assert_token_absent(token, str(exc_info.value))


def _auth_echo(request: httpx2.Request) -> str:
    """
    Build the line an auth gateway answers with: the header it refused.

    Args:
        request: The request the gateway received.

    Returns:
        A sentence quoting the request's ``Authorization`` header whole.

    """
    return f"Invalid token header: {request.headers['authorization']}"


def _upload_401_json(request: httpx2.Request) -> httpx2.Response:
    """Refuse an upload with a DRF-style 401 that quotes the header."""
    return httpx2.Response(401, json={"detail": _auth_echo(request)})


def _upload_403_text(request: httpx2.Request) -> httpx2.Response:
    """Refuse an upload with a plain-text 403 that quotes the header."""
    return httpx2.Response(403, text=_auth_echo(request))


def _upload_redirect(request: httpx2.Request) -> httpx2.Response:
    """Redirect an upload to a login page whose query carries the token."""
    token = request.headers["authorization"].split()[-1]
    return httpx2.Response(
        302, headers={"location": f"https://sso.example/login?auth={token}"}
    )


def _upload_500_text(request: httpx2.Request) -> httpx2.Response:
    """Fail an upload with a 5xx page that quotes the header."""
    return httpx2.Response(500, text=_auth_echo(request))


def _poll_401_json(request: httpx2.Request) -> httpx2.Response:
    """Refuse a task poll with a DRF-style 401 that quotes the header."""
    return httpx2.Response(401, json={"detail": _auth_echo(request)})


def _poll_failure_text(request: httpx2.Request) -> httpx2.Response:
    """Answer a task poll with a FAILURE whose result quotes the header."""
    task = {"task_id": "t1", "status": "FAILURE", "result": _auth_echo(request)}
    return httpx2.Response(200, json=[task])


def _tags_403_text(request: httpx2.Request) -> httpx2.Response:
    """Refuse a tag list with a plain-text 403 that quotes the header."""
    return httpx2.Response(403, text=_auth_echo(request))


# (what the client does, how the server answers it)
_ECHOING_ANSWERS = [
    pytest.param("upload", _upload_401_json, id="upload-401-json"),
    pytest.param("upload", _upload_403_text, id="upload-403-text"),
    pytest.param("upload", _upload_redirect, id="upload-redirect-location"),
    pytest.param("upload", _upload_500_text, id="upload-500-text"),
    pytest.param("poll", _poll_401_json, id="poll-401-json"),
    pytest.param("poll", _poll_failure_text, id="poll-task-failure"),
    pytest.param("tags", _tags_403_text, id="tags-403-text"),
]


def _run_operation(client: PaperlessClient, operation: str, pdf: Path) -> None:
    """
    Make the one client call a response-text case exercises.

    Args:
        client: The client under test.
        operation: ``upload``, ``poll`` or ``tags``.
        pdf: The PDF an upload sends.

    """
    match operation:
        case "upload":
            client.upload_document(pdf, title="Echo")
        case "poll":
            client.poll_task("t1", timeout=5)
        case _:
            client.get_tags()


class TestResponseTextIsRedacted:
    """
    Text the server sends back has the token struck out wherever it is shown.

    A reverse proxy or auth gateway in front of paperless-ngx may answer a
    refused request by quoting the ``Authorization`` header it refused.  That
    body becomes the error message -- the job's error, shown on the status
    page to anyone on the LAN -- and is logged in full at DEBUG.
    """

    @pytest.mark.parametrize("token", _REDACTED_TOKENS)
    @pytest.mark.parametrize(("operation", "answer"), _ECHOING_ANSWERS)
    @pytest.mark.usefixtures("debug_capture")
    def test_token_quoted_by_the_server_is_struck(
        self,
        operation: str,
        answer: Callable[[httpx2.Request], httpx2.Response],
        token: str,
        sample_pdf: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Neither the message nor any log line carries the echoed token."""
        client = PaperlessClient(
            url="http://paperless:8000",
            token=token,
            max_retries=1,
            transport=_make_transport(answer),
        )
        try:
            with pytest.raises(PaperlessError) as exc_info:
                _run_operation(client, operation, sample_pdf)
        finally:
            client.close()
        message = str(exc_info.value)
        # The server's text is still shown, with the token replaced.
        assert "***" in message
        for text in (message, repr(exc_info.value), caplog.text):
            _assert_token_absent(token, text)

    @pytest.mark.parametrize("token", _REDACTED_TOKENS)
    @pytest.mark.usefixtures("debug_capture")
    def test_full_body_logged_at_debug_is_struck(
        self, token: str, sample_pdf: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The DEBUG copy of an error body keeps the text but not the token."""
        client = PaperlessClient(
            url="http://paperless:8000",
            token=token,
            transport=_make_transport(_upload_403_text),
        )
        try:
            with pytest.raises(PaperlessError):
                client.upload_document(sample_pdf, title="Echo")
        finally:
            client.close()
        bodies = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.DEBUG
            and record.getMessage().startswith("Paperless error body")
        ]
        assert bodies == ["Paperless error body (403): Invalid token header: Token ***"]

    @pytest.mark.usefixtures("debug_capture")
    def test_full_body_logged_at_debug_carries_no_control_characters(
        self, sample_pdf: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        The DEBUG copy of an error body shows escape sequences as text.

        ``serve`` streams its log to the terminal, so a body carrying ESC from a
        hostile server or proxy must not reach it live.  The body is still logged
        in full, with each control character written out.
        """

        def hostile_body(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(403, text="bad\x1b[2Jgateway\x07\nline two")

        client = PaperlessClient(
            url="http://paperless:8000",
            token="abc123",
            transport=_make_transport(hostile_body),
        )
        try:
            with pytest.raises(PaperlessError):
                client.upload_document(sample_pdf, title="Echo")
        finally:
            client.close()
        bodies = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.DEBUG
            and record.getMessage().startswith("Paperless error body")
        ]
        assert bodies == [
            "Paperless error body (403): bad\\x1b[2Jgateway\\x07\\nline two"
        ]


def _inner_token_failure(token: str) -> httpx2.ConnectError:
    """
    Build a transport error whose own text is clean but whose cause quotes the token.

    httpx2 re-raises its transport's errors as its own types, chained to the
    original, so the token can sit one link down the chain.

    Args:
        token: The configured token.

    Returns:
        A ConnectError chained to an error naming the token.

    """
    error = httpx2.ConnectError("connection failed")
    error.__cause__ = OSError(f"header refused: Token {token}")
    return error


_TOKEN_BEARING_CAUSES = [
    pytest.param(_token_bearing_failure, id="own-text"),
    pytest.param(_inner_token_failure, id="inner-cause"),
]


def _formatted(error: BaseException) -> str:
    """
    Render an error the way a logged traceback shows it, causes included.

    Args:
        error: The error to render.

    Returns:
        The full traceback text, every chained cause and context included.

    """
    return "".join(traceback.format_exception(error))


class TestChainedCausesCarryNoToken:
    """
    A traceback of any client error is as clean as its message.

    The worker logs a failed job with ``logger.exception`` and the CLI logs a
    failure with ``exc_info``, and both print the whole chain.  A message with
    the token struck out is worth nothing if the cause it is chained to
    prints the token a line further down.
    """

    @pytest.mark.parametrize("token", _REDACTED_TOKENS)
    @pytest.mark.parametrize("build", _TOKEN_BEARING_CAUSES)
    @pytest.mark.usefixtures("sleeps")
    def test_upload_retries_exhausted(
        self,
        build: Callable[[str], httpx2.ConnectError],
        token: str,
        sample_pdf: Path,
    ) -> None:
        """The failed-after-N-attempts error formats without the token."""
        handler = _CountingHandler(_raising(build(token)))
        client = PaperlessClient(
            url="http://paperless:8000",
            token=token,
            max_retries=2,
            transport=_make_transport(handler),
        )
        try:
            with pytest.raises(PaperlessError) as exc_info:
                client.upload_document(sample_pdf, title="Chain")
        finally:
            client.close()
        assert handler.calls == 2
        assert "failed after 2 attempts" in str(exc_info.value)
        _assert_token_absent(token, _formatted(exc_info.value))

    @pytest.mark.parametrize("token", _REDACTED_TOKENS)
    @pytest.mark.parametrize("build", _TOKEN_BEARING_CAUSES)
    def test_poll_timeout(
        self, build: Callable[[str], httpx2.ConnectError], token: str
    ) -> None:
        """The poll timeout naming the last request error formats without it."""
        handler = _CountingHandler(_raising(build(token)))
        client = PaperlessClient(
            url="http://paperless:8000", token=token, transport=_make_transport(handler)
        )
        try:
            with pytest.raises(PaperlessTimeoutError) as exc_info:
                client.poll_task("t1", timeout=0)
        finally:
            client.close()
        assert "last error:" in str(exc_info.value)
        _assert_token_absent(token, _formatted(exc_info.value))

    @pytest.mark.parametrize("token", _REDACTED_TOKENS)
    @pytest.mark.parametrize("build", _TOKEN_BEARING_CAUSES)
    @pytest.mark.parametrize(("method", "noun"), _METADATA_METHODS)
    def test_metadata_fetch(
        self,
        method: str,
        noun: str,
        build: Callable[[str], httpx2.ConnectError],
        token: str,
    ) -> None:
        """A failed tag or correspondent fetch formats without the token."""
        handler = _CountingHandler(_raising(build(token)))
        client = PaperlessClient(
            url="http://paperless:8000", token=token, transport=_make_transport(handler)
        )
        try:
            with pytest.raises(PaperlessError) as exc_info:
                getattr(client, method)()
        finally:
            client.close()
        assert str(exc_info.value).startswith(f"Could not fetch {noun} from Paperless")
        _assert_token_absent(token, _formatted(exc_info.value))

    @pytest.mark.usefixtures("sleeps")
    def test_a_clean_cause_is_still_chained(self, sample_pdf: Path) -> None:
        """
        The control: a cause that quotes nothing secret stays attached.

        Dropping every cause would pass the tests above too, and lose the
        diagnosis a traceback exists to give.
        """
        failure = httpx2.ConnectError("connection refused")
        handler = _CountingHandler(_raising(failure))
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            max_retries=2,
            transport=_make_transport(handler),
        )
        try:
            with pytest.raises(PaperlessError) as exc_info:
                client.upload_document(sample_pdf, title="Chain")
        finally:
            client.close()
        assert exc_info.value.__cause__ is failure


_CONFIGURED_HOST = "paperless.test"
_FOREIGN_HOST = "evil.example"


def _page_payload(items: list[dict[str, object]], next_url: str | None) -> object:
    """Build one paginated metadata page as paperless-ngx serves it."""
    return {"count": None, "next": next_url, "previous": None, "results": items}


def _items(noun: str, first: int, count: int) -> list[dict[str, object]]:
    """Build ``count`` metadata items with ids from ``first`` upwards."""
    return [
        {"id": item_id, "name": f"{noun} {item_id}"}
        for item_id in range(first, first + count)
    ]


class _PagedHandler:
    """
    A mock server that records every request and answers by page number.

    A request without a ``page`` parameter is page 1, as it is for the real
    server; a page the script does not name is answered 404, the server's
    "Invalid page".
    """

    def __init__(self, pages: dict[int, httpx2.Response]) -> None:
        """Build a handler that answers page N with ``pages[N]``."""
        self.requests: list[httpx2.Request] = []
        self._pages = pages

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Record the request and answer the page it asks for."""
        self.requests.append(request)
        page = int(request.url.params.get("page", "1"))
        response = self._pages.get(page)
        if response is None:
            return httpx2.Response(404, json={"detail": "Invalid page."})
        return response

    @property
    def pages_requested(self) -> list[str | None]:
        """Return the ``page`` parameter of every request, in order."""
        return [request.url.params.get("page") for request in self.requests]


def _paged_client(handler: _PagedHandler) -> PaperlessClient:
    """Build a metadata client on the configured test host."""
    return PaperlessClient(
        url=f"http://{_CONFIGURED_HOST}:8000",
        token=_MOCK_AUTH,
        transport=_make_transport(handler),
    )


def _fetch(handler: _PagedHandler, method: str) -> list[dict[str, object]]:
    """Run one metadata fetch against ``handler`` and close the client."""
    client = _paged_client(handler)
    try:
        return getattr(client, method)()
    finally:
        client.close()


def _next_link(noun: str, page: int, host: str = _CONFIGURED_HOST) -> str:
    """Build the absolute ``next`` link a server would put on a page."""
    return f"http://{host}:8000/api/{noun}/?page={page}&page_size=1000"


class TestMetadataPagination:
    """Tags and correspondents are fetched page by page on the configured URL."""

    @pytest.mark.parametrize(("method", "noun"), _METADATA_METHODS)
    def test_all_pages_are_concatenated_in_order(self, method: str, noun: str) -> None:
        """Pages 1, 2 and 3 are fetched until ``next`` is null and joined."""
        first, second, third = (
            _items(noun, 1, 3),
            _items(noun, 4, 3),
            _items(noun, 7, 1),
        )
        handler = _PagedHandler(
            {
                1: httpx2.Response(200, json=_page_payload(first, _next_link(noun, 2))),
                2: httpx2.Response(
                    200, json=_page_payload(second, _next_link(noun, 3))
                ),
                3: httpx2.Response(200, json=_page_payload(third, None)),
            }
        )
        assert _fetch(handler, method) == first + second + third
        assert handler.pages_requested == ["1", "2", "3"]

    @pytest.mark.parametrize(("method", "noun"), _METADATA_METHODS)
    def test_a_foreign_next_link_is_never_contacted(
        self, method: str, noun: str
    ) -> None:
        """
        A ``next`` naming another host is read only as "there is a page 2".

        A server behind a misconfigured proxy builds its ``next`` links from
        the wrong host or scheme.  The client carries the API token on every
        request, so every request must go to the configured host and
        collection path, with page 2 asked for by number.
        """
        first, second = _items(noun, 1, 2), _items(noun, 3, 2)
        foreign_next = f"http://{_FOREIGN_HOST}/api/{noun}/?page=2"
        handler = _PagedHandler(
            {
                1: httpx2.Response(200, json=_page_payload(first, foreign_next)),
                2: httpx2.Response(200, json=_page_payload(second, None)),
            }
        )
        assert _fetch(handler, method) == first + second
        assert {request.url.host for request in handler.requests} == {_CONFIGURED_HOST}
        assert all(request.url.path == f"/api/{noun}/" for request in handler.requests)
        assert all(
            request.headers["Authorization"] == f"Token {_MOCK_AUTH}"
            for request in handler.requests
        )
        assert handler.pages_requested == ["1", "2"]
        assert not any(
            _FOREIGN_HOST in str(request.url) for request in handler.requests
        )

    @pytest.mark.parametrize(("method", "noun"), _METADATA_METHODS)
    def test_a_bare_list_is_returned_after_one_request(
        self, method: str, noun: str
    ) -> None:
        """A bare JSON list is the whole collection: nothing more is asked for."""
        items = _items(noun, 1, 2)
        handler = _PagedHandler({1: httpx2.Response(200, json=items)})
        assert _fetch(handler, method) == items
        assert len(handler.requests) == 1

    @pytest.mark.parametrize(("method", "noun"), _METADATA_METHODS)
    def test_an_empty_page_ends_the_fetch(self, method: str, noun: str) -> None:
        """
        An empty page stops the fetch even though its ``next`` is not null.

        Otherwise a server that always names a next page would keep the
        client asking forever.
        """
        first = _items(noun, 1, 2)
        handler = _PagedHandler(
            {
                1: httpx2.Response(200, json=_page_payload(first, _next_link(noun, 2))),
                2: httpx2.Response(200, json=_page_payload([], _next_link(noun, 3))),
                3: httpx2.Response(200, json=_page_payload(_items(noun, 9, 1), None)),
            }
        )
        assert _fetch(handler, method) == first
        assert handler.pages_requested == ["1", "2"]

    @pytest.mark.parametrize(("method", "noun"), _METADATA_METHODS)
    def test_a_redirect_on_a_later_page_is_a_paperless_error(
        self, method: str, noun: str
    ) -> None:
        """A 302 on page 2 is not followed; it fails the whole fetch."""
        handler = _PagedHandler(
            {
                1: httpx2.Response(
                    200, json=_page_payload(_items(noun, 1, 1), _next_link(noun, 2))
                ),
                2: httpx2.Response(
                    302,
                    headers={"Location": f"http://{_FOREIGN_HOST}/api/{noun}/?page=2"},
                ),
            }
        )
        with pytest.raises(PaperlessError) as exc_info:
            _fetch(handler, method)
        assert str(exc_info.value).startswith(f"Could not fetch {noun} from Paperless")
        assert isinstance(exc_info.value.__cause__, httpx2.HTTPStatusError)
        assert handler.pages_requested == ["1", "2"]
        assert {request.url.host for request in handler.requests} == {_CONFIGURED_HOST}

    @pytest.mark.parametrize(("method", "noun"), _METADATA_METHODS)
    def test_every_page_asks_for_a_large_page_size(
        self, method: str, noun: str
    ) -> None:
        """
        Each request names its page and asks for at least 1000 items.

        A typical install then fits in one round trip.
        """
        handler = _PagedHandler(
            {
                1: httpx2.Response(
                    200, json=_page_payload(_items(noun, 1, 1), _next_link(noun, 2))
                ),
                2: httpx2.Response(200, json=_page_payload(_items(noun, 2, 1), None)),
            }
        )
        _fetch(handler, method)
        assert handler.pages_requested == ["1", "2"]
        assert all(
            int(request.url.params["page_size"]) >= 1000 for request in handler.requests
        )


class TestMetadataResponseShape:
    """A metadata page of the wrong shape is a PaperlessError, not a raw error."""

    @pytest.mark.parametrize(("method", "noun"), _METADATA_METHODS)
    @pytest.mark.parametrize(
        "results",
        [None, "abc", {"id": 1}, [1, 2], [{"id": 1}, "two"]],
        ids=["null", "string", "object", "numbers", "one-not-an-object"],
    )
    def test_a_page_whose_results_are_not_a_list_of_objects_fails(
        self, method: str, noun: str, results: object
    ) -> None:
        """
        ``results`` must be a list of objects.

        ``null`` used to raise a TypeError out of the client, and a string or
        an object was taken apart into its characters or keys.
        """
        handler = _PagedHandler(
            {
                1: httpx2.Response(
                    200, json={"count": 1, "next": None, "results": results}
                )
            }
        )
        with pytest.raises(PaperlessError) as exc_info:
            _fetch(handler, method)
        assert str(exc_info.value) == (
            f"Could not fetch {noun} from Paperless at "
            f"http://{_CONFIGURED_HOST}:8000: page 1 did not hold a list of objects"
        )

    @pytest.mark.parametrize(("method", "noun"), _METADATA_METHODS)
    def test_a_page_without_results_fails(self, method: str, noun: str) -> None:
        """A paginated page with no ``results`` key is malformed, not empty."""
        handler = _PagedHandler(
            {1: httpx2.Response(200, json={"count": 0, "next": None})}
        )
        with pytest.raises(PaperlessError, match="page 1 did not hold a list"):
            _fetch(handler, method)

    @pytest.mark.parametrize(("method", "noun"), _METADATA_METHODS)
    def test_a_bare_list_of_non_objects_fails(self, method: str, noun: str) -> None:
        """A bare list must hold objects too."""
        handler = _PagedHandler({1: httpx2.Response(200, json=["one", "two"])})
        with pytest.raises(PaperlessError, match="page 1 did not hold a list"):
            _fetch(handler, method)

    @pytest.mark.parametrize(("method", "noun"), _METADATA_METHODS)
    def test_a_bare_list_after_page_one_fails(self, method: str, noun: str) -> None:
        """
        A bare list is the whole collection only on page 1.

        On a later page it used to be returned alone, throwing away the pages
        already collected.
        """
        handler = _PagedHandler(
            {
                1: httpx2.Response(
                    200, json=_page_payload(_items(noun, 1, 2), _next_link(noun, 2))
                ),
                2: httpx2.Response(200, json=_items(noun, 3, 2)),
            }
        )
        with pytest.raises(PaperlessError) as exc_info:
            _fetch(handler, method)
        assert str(exc_info.value).endswith(
            "page 2 was a bare list, which only page 1 may be"
        )

    @pytest.mark.parametrize(("method", "noun"), _METADATA_METHODS)
    @pytest.mark.parametrize("body", [42, "tags", True])
    def test_a_body_that_is_neither_a_list_nor_an_object_fails(
        self, method: str, noun: str, body: object
    ) -> None:
        """A scalar body is a PaperlessError naming the collection."""
        handler = _PagedHandler({1: httpx2.Response(200, json=body)})
        with pytest.raises(PaperlessError) as exc_info:
            _fetch(handler, method)
        assert str(exc_info.value).startswith(f"Could not fetch {noun} from Paperless")
        assert str(exc_info.value).endswith("neither a list nor an object")


class _EndlessHandler:
    """
    A mock server that always names a next page, whatever page is asked for.

    ``page_for`` builds the body for the page number requested.  The handler
    refuses to answer more than ``limit`` requests, so a client that never
    stops fails the test with an AssertionError instead of hanging it.
    """

    def __init__(self, page_for: Callable[[int], object], limit: int = 50) -> None:
        """Build a handler that answers page N with ``page_for(N)``."""
        self.requests: list[httpx2.Request] = []
        self._page_for = page_for
        self._limit = limit

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Record the request and answer it, refusing to go on forever."""
        self.requests.append(request)
        assert len(self.requests) <= self._limit, "the client never stopped"
        page = int(request.url.params.get("page", "1"))
        return httpx2.Response(200, json=self._page_for(page))


def _fetch_endless(handler: _EndlessHandler, method: str) -> list[dict[str, object]]:
    """Run one metadata fetch against an endless ``handler``."""
    client = PaperlessClient(
        url=f"http://{_CONFIGURED_HOST}:8000",
        token=_MOCK_AUTH,
        transport=_make_transport(handler),
    )
    try:
        return getattr(client, method)()
    finally:
        client.close()


class TestMetadataPaginationTerminates:
    """A server that never stops naming a next page cannot keep the client busy."""

    @pytest.mark.parametrize(("method", "noun"), _METADATA_METHODS)
    @pytest.mark.parametrize("count", [None, 50, "many"])
    def test_a_server_ignoring_page_fails_on_the_second_request(
        self, method: str, noun: str, count: object
    ) -> None:
        """
        The same non-final page twice in a row is a PaperlessError.

        A proxy that drops the query string, or a server that ignores
        ``page``, answers page 1 to every request.  The second, identical
        answer shows the client is making no progress, so it stops loudly
        instead of asking forever while the metadata cache lock is held.
        """
        items = _items(noun, 1, 2)
        handler = _EndlessHandler(
            lambda _page: {
                "count": count,
                "next": _next_link(noun, 2),
                "previous": None,
                "results": items,
            }
        )
        with pytest.raises(PaperlessError) as exc_info:
            _fetch_endless(handler, method)
        message = str(exc_info.value)
        assert message.startswith(
            f"Could not fetch {noun} from Paperless at http://{_CONFIGURED_HOST}:8000: "
        )
        assert "page 2 repeated page 1" in message
        assert "\n" not in message
        assert len(handler.requests) == 2

    @pytest.mark.parametrize(("method", "noun"), _METADATA_METHODS)
    def test_reaching_the_servers_count_ends_the_fetch(
        self, method: str, noun: str
    ) -> None:
        """Once the collected items reach ``count``, a non-null ``next`` is ignored."""
        items = _items(noun, 1, 3)
        handler = _EndlessHandler(
            lambda page: {
                "count": 3,
                "next": _next_link(noun, page + 1),
                "previous": None,
                "results": items if page == 1 else _items(noun, 100 + page, 1),
            }
        )
        assert _fetch_endless(handler, method) == items
        assert len(handler.requests) == 1

    @pytest.mark.parametrize(("method", "noun"), _METADATA_METHODS)
    def test_count_across_several_pages_ends_the_fetch(
        self, method: str, noun: str
    ) -> None:
        """Pages are joined until their total reaches ``count``, then no more."""
        handler = _EndlessHandler(
            lambda page: {
                "count": 5,
                "next": _next_link(noun, page + 1),
                "previous": None,
                "results": _items(noun, 2 * page - 1, 2),
            }
        )
        assert _fetch_endless(handler, method) == _items(noun, 1, 6)
        assert len(handler.requests) == 3

    @pytest.mark.parametrize(("method", "noun"), _METADATA_METHODS)
    def test_pages_that_never_reach_count_stop_at_a_cap(
        self, method: str, noun: str
    ) -> None:
        """
        New pages that never add up to ``count`` stop one page past the need.

        Page 1 holds 2 of a count of 10, so 5 pages should be enough; the
        client allows one more, then reports the server rather than asking
        on.
        """
        handler = _EndlessHandler(
            lambda page: {
                "count": 10,
                "next": _next_link(noun, page + 1),
                "previous": None,
                "results": _items(noun, 10 * page, 2 if page == 1 else 1),
            }
        )
        with pytest.raises(PaperlessError) as exc_info:
            _fetch_endless(handler, method)
        assert "after 6 pages" in str(exc_info.value)
        assert len(handler.requests) == 6

    @pytest.mark.parametrize(("method", "noun"), _METADATA_METHODS)
    @pytest.mark.parametrize("count", [None, -1, True, "10"])
    def test_without_a_usable_count_a_fixed_cap_applies(
        self, monkeypatch: pytest.MonkeyPatch, method: str, noun: str, count: object
    ) -> None:
        """
        With no usable ``count``, distinct endless pages stop at a fixed cap.

        The cap is lowered here so the test makes a handful of requests
        rather than a thousand.
        """
        monkeypatch.setattr("saneless.paperless._METADATA_MAX_PAGES", 4)
        handler = _EndlessHandler(
            lambda page: {
                "count": count,
                "next": _next_link(noun, page + 1),
                "previous": None,
                "results": _items(noun, page, 1),
            }
        )
        with pytest.raises(PaperlessError) as exc_info:
            _fetch_endless(handler, method)
        assert "after 4 pages" in str(exc_info.value)
        assert len(handler.requests) == 4


# ---------------------------------------------------------------------------
# Connection test
# ---------------------------------------------------------------------------


class TestConnectionTest:
    """Connection test method tests."""

    @pytest.mark.parametrize(("status_code", "expected"), _CONNECTION_STATUS_CASES)
    def test_status_code_classification(
        self, status_code: int, expected: ConnectionStatus
    ) -> None:
        """Each HTTP status maps to exactly one ConnectionStatus member."""
        assert _connection_result_for_status(status_code) is expected

    @pytest.mark.parametrize("exc_type", _CONNECTION_EXCEPTION_CASES)
    def test_transport_failures_are_unreachable(
        self, exc_type: type[httpx2.TransportError]
    ) -> None:
        """
        Every transport-level failure is UNREACHABLE, not an escaped exception.

        ConnectTimeout and ReadTimeout used to propagate past the narrow
        `except httpx2.ConnectError` and hit routes.py's blanket handler,
        surfacing as HTTP 502 {"status": "error"} -- which is none of the
        five outcomes OUTC-08 names.
        """
        assert (
            _connection_result_for_exception(exc_type) is ConnectionStatus.UNREACHABLE
        )

    @pytest.mark.parametrize("member", list(ConnectionStatus), ids=lambda m: m.name)
    def test_every_member_is_producible(self, member: ConnectionStatus) -> None:
        """
        No ConnectionStatus member is unreachable from a real response.

        Parametrised over `list(ConnectionStatus)` so a sixth member cannot
        be added without someone deciding what produces it.
        """
        produced = {
            _connection_result_for_status(code) for code in (200, 401, 404, 500)
        }
        produced.add(_connection_result_for_exception(httpx2.ConnectError))
        assert member in produced, f"{member.name} is not produced by any input"

    def test_legacy_wire_strings_are_byte_identical(self) -> None:
        """
        The three documented JSON strings still compare equal as plain str.

        These assertions are deliberately NOT enum-identity checks: they are
        the only thing proving the StrEnum *value* is still what
        web/routes.py serialises and docs/reference/web-api.md documents.
        """
        assert _connection_result_for_status(200) == "connected"
        assert _connection_result_for_status(401) == "token_rejected"
        assert _connection_result_for_exception(httpx2.ConnectError) == "unreachable"

    def test_connection_test_hits_the_tags_endpoint(self) -> None:
        """The probe is a one-row GET against /api/tags/."""

        def handler(request: httpx2.Request) -> httpx2.Response:
            assert "/api/tags/" in str(request.url)
            assert "page_size=1" in str(request.url)
            return httpx2.Response(200, json={"count": 0, "results": []})

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            transport=transport,
        )
        assert client.test_connection() is ConnectionStatus.CONNECTED
        client.close()

    def test_unclassified_status_is_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An unclassified non-2xx is diagnosable, not silently bucketed."""
        with caplog.at_level(logging.WARNING, logger="saneless.paperless"):
            assert _connection_result_for_status(429) is ConnectionStatus.SERVER_ERROR
        assert any("429" in message for message in caplog.messages)


class TestConnectionTimeout:
    """
    The probe can be bounded per request without changing any caller (APPL-02).

    ``PaperlessClient`` sets one flat 30 s on its ``httpx2.Client``, which is
    thirty seconds of a household member staring at a spinner when the
    paperless-ngx host is unplugged.  The status strip and ``saneless doctor``
    need a two-second answer, while ``GET /api/paperless/test`` deliberately
    keeps today's client default, so the bound is a per-request override and
    not a new constructor argument.

    Every assertion here reads ``request.extensions["timeout"]``, the dict
    httpx2 hands the transport, rather than measuring wall-clock.  The suite
    forbids ``sleep`` and a timing assertion against a real socket would be
    flaky on a loaded machine; what actually needs proving is *which budget was
    sent*, and that is a value, not a duration.
    """

    @staticmethod
    def _recorded_timeout(
        timeout: httpx2.Timeout | None,
    ) -> dict[str, float | None]:
        """
        Run ``test_connection`` and return the timeout the transport was given.

        Args:
            timeout: The bound to pass, or None to call with no argument at
                all -- which is the case that proves the existing route is
                untouched.

        Returns:
            The ``timeout`` extension dict httpx2 handed the mock transport.

        """
        seen: list[dict[str, float | None]] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request.extensions["timeout"])
            return httpx2.Response(200, json={"count": 0, "results": []})

        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            transport=_make_transport(handler),
        )
        try:
            if timeout is None:
                client.test_connection()
            else:
                client.test_connection(timeout=timeout)
        finally:
            client.close()
        return seen[0]

    @pytest.mark.parametrize(("status_code", "expected"), _CONNECTION_STATUS_CASES)
    def test_no_timeout_argument_keeps_every_outcome(
        self, status_code: int, expected: ConnectionStatus
    ) -> None:
        """
        Calling with no bound classifies exactly as it does today.

        Args:
            status_code: The status the stub server answers with.
            expected: The outcome that status has always produced.

        """
        assert _connection_result_for_status(status_code) is expected

    def test_no_timeout_argument_uses_the_client_default(self) -> None:
        """
        A bare call still carries the client's 30 s, so no caller changed.

        This is the assertion that proves ``GET /api/paperless/test`` keeps
        today's behaviour: the route calls ``test_connection()`` with no
        argument, and what it sends is the constructor's flat 30 s.
        """
        recorded = self._recorded_timeout(None)
        assert recorded["connect"] == 30.0
        assert recorded["read"] == 30.0

    def test_explicit_timeout_is_sent_to_the_transport(self) -> None:
        """A passed bound reaches the request, connect and read separately."""
        recorded = self._recorded_timeout(httpx2.Timeout(5.0, connect=2.0))
        assert recorded["connect"] == 2.0
        assert recorded["read"] == 5.0

    def test_connect_timeout_is_unreachable(self) -> None:
        """
        A bounded connect that expires reports UNREACHABLE, not an exception.

        ``ConnectTimeout`` subclasses ``TransportError``, so the existing arm
        already covers it -- asserted here so bounding the probe rests on a
        tested claim rather than on reading the class hierarchy.
        """
        result = _connection_result_for_exception(httpx2.ConnectTimeout)
        assert result is ConnectionStatus.UNREACHABLE

    def test_read_timeout_is_unreachable(self) -> None:
        """A host that accepts and then says nothing is UNREACHABLE too."""
        result = _connection_result_for_exception(httpx2.ReadTimeout)
        assert result is ConnectionStatus.UNREACHABLE


# ---------------------------------------------------------------------------
# Auth header test
# ---------------------------------------------------------------------------


class TestConsumeDir:
    """Consume directory fallback tests."""

    def test_consume_dir_works_when_exists(
        self, sample_pdf: Path, tmp_path: Path
    ) -> None:
        """Fallback works when consume_dir already exists."""
        consume_dir = tmp_path / "existing-consume"
        consume_dir.mkdir()

        def handler(_request: httpx2.Request) -> httpx2.Response:
            msg = "connection refused"
            raise httpx2.ConnectError(msg)

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            consume_dir=consume_dir,
            transport=transport,
            max_retries=1,
        )
        result = client.upload_document(sample_pdf, title="Existing dir test")
        assert result.delivered_to_api is False
        copied = list(consume_dir.iterdir())
        assert len(copied) == 1
        assert result.consume_dir_path == consume_dir / "test.pdf"
        client.close()

    def test_delivery_leaves_only_the_final_file(
        self, sample_pdf: Path, tmp_path: Path
    ) -> None:
        """
        The finished consume directory holds the PDF and nothing else.

        paperless-ngx is watching this directory with inotify, so a staging
        file surviving the handoff is a defect in the other system, not a
        cosmetic one.
        """
        consume_dir = tmp_path / "atomic-consume"
        consume_dir.mkdir()

        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            consume_dir=consume_dir,
            transport=_make_transport(_always_refused),
            max_retries=1,
        )
        result = client.upload_document(sample_pdf, title="Atomic test")
        client.close()

        entries = sorted(entry.name for entry in consume_dir.iterdir())
        assert entries == ["test.pdf"]
        _assert_no_staging_files(consume_dir)
        assert result.consume_dir_path == consume_dir / "test.pdf"
        assert (consume_dir / "test.pdf").read_bytes() == sample_pdf.read_bytes()

    def test_a_failed_staged_write_leaves_the_directory_empty(
        self, sample_pdf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A copy that dies part-way leaves nothing behind and still raises.

        Without the cleanup a retry could later find -- or paperless-ngx
        could consume -- a truncated remnant.
        """
        consume_dir = tmp_path / "doomed-consume"
        consume_dir.mkdir()

        def half_a_file(fsrc: BinaryIO, fdst: BinaryIO, length: int = 0) -> None:
            """Write half the bytes, then fail the way a full disk would."""
            fdst.write(b"%PDF-1.4 trun")
            msg = "no space left on device"
            raise OSError(msg)

        monkeypatch.setattr(shutil, "copyfileobj", half_a_file)

        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            consume_dir=consume_dir,
            transport=_make_transport(_always_refused),
            max_retries=1,
        )
        try:
            with pytest.raises(
                PaperlessError,
                match=(
                    "Could not copy the PDF to the consume directory "
                    f"{re.escape(str(consume_dir))}: no space left"
                ),
            ) as exc_info:
                client.upload_document(sample_pdf, title="Doomed")
        finally:
            client.close()

        assert isinstance(exc_info.value.__cause__, OSError)
        assert sorted(entry.name for entry in consume_dir.iterdir()) == []

    def test_consume_dir_missing_is_reported_and_nothing_is_created(
        self, sample_pdf: Path, tmp_path: Path
    ) -> None:
        """
        A missing consume directory fails the handoff and is never created.

        A directory created where no volume is mounted is one nothing
        watches: the copy would sit there unseen while the job reported a
        fallback that saved the scan.
        """
        consume_dir = tmp_path / "missing" / "consume"
        before = sorted(entry.name for entry in tmp_path.iterdir())

        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            consume_dir=consume_dir,
            transport=_make_transport(_always_refused),
            max_retries=1,
        )
        try:
            with pytest.raises(PaperlessError) as exc_info:
                client.upload_document(sample_pdf, title="Unmounted")
        finally:
            client.close()

        message = str(exc_info.value)
        assert message == (
            f"consume directory {consume_dir} does not exist — "
            "is the paperless-ngx volume mounted?"
        )
        assert not (tmp_path / "missing").exists()
        assert sorted(entry.name for entry in tmp_path.iterdir()) == before

    def test_consume_dir_missing_when_a_file_is_in_its_place(
        self, sample_pdf: Path, tmp_path: Path
    ) -> None:
        """A regular file where the directory should be is not a directory."""
        consume_dir = tmp_path / "consume"
        consume_dir.write_bytes(b"occupied")

        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            consume_dir=consume_dir,
            transport=_make_transport(_always_refused),
            max_retries=1,
        )
        try:
            with pytest.raises(PaperlessError) as exc_info:
                client.upload_document(sample_pdf, title="Occupied")
        finally:
            client.close()

        message = str(exc_info.value)
        assert "does not exist" in message
        assert "is the paperless-ngx volume mounted?" in message
        assert consume_dir.read_bytes() == b"occupied"

    def test_staging_leaves_a_symlink_at_the_old_name_untouched(
        self, sample_pdf: Path, tmp_path: Path
    ) -> None:
        """
        A symlink planted at the predictable staging name is not written through.

        The consume folder is often shared, so anyone who can write to it
        could otherwise aim the copy at a file outside it.
        """
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()
        target = tmp_path / "outside.txt"
        target.write_bytes(b"keep me")
        planted = consume_dir / f".{sample_pdf.name}.part"
        planted.symlink_to(target)

        result = _deliver_to_consume_dir(sample_pdf, consume_dir)

        dest = consume_dir / sample_pdf.name
        assert result.consume_dir_path == dest
        assert target.read_bytes() == b"keep me"
        assert planted.is_symlink()
        assert planted.readlink() == target
        assert not dest.is_symlink()
        assert dest.read_bytes() == sample_pdf.read_bytes()

    def test_staging_leaves_a_leftover_at_the_old_name_untouched(
        self, sample_pdf: Path, tmp_path: Path
    ) -> None:
        """A crash leftover at the old fixed staging name cannot collide."""
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()
        leftover = consume_dir / f".{sample_pdf.name}.part"
        leftover.write_bytes(b"leftover")

        _deliver_to_consume_dir(sample_pdf, consume_dir)

        assert leftover.read_bytes() == b"leftover"
        assert (consume_dir / sample_pdf.name).read_bytes() == sample_pdf.read_bytes()

    def test_staging_file_is_hidden_unique_and_removed(
        self, sample_pdf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The bytes go to a hidden, uniquely named ``.part`` file.

        It is created exclusively, so its name is not the predictable one,
        and it is gone once the PDF has its final name.
        """
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()
        seen: list[str] = []
        original = shutil.copyfileobj

        def recording(source: BinaryIO, target: BinaryIO) -> None:
            seen.extend(entry.name for entry in consume_dir.iterdir())
            original(source, target)

        monkeypatch.setattr(shutil, "copyfileobj", recording)
        _deliver_to_consume_dir(sample_pdf, consume_dir)

        assert len(seen) == 1
        staged = seen[0]
        assert staged.startswith(f".{sample_pdf.name}.")
        assert staged.endswith(".part")
        assert staged != f".{sample_pdf.name}.part"
        assert sorted(entry.name for entry in consume_dir.iterdir()) == [
            sample_pdf.name
        ]


class TestAuthHeader:
    """Authentication header tests."""

    def test_auth_header(self) -> None:
        """Authorization header contains Token prefix and credential."""
        captured_headers: dict[str, str | None] = {}

        def handler(_request: httpx2.Request) -> httpx2.Response:
            captured_headers["auth"] = _request.headers.get("authorization")
            return httpx2.Response(200, json={"status": "ok"})

        transport = _make_transport(handler)
        auth = "my-secret-token"
        client = PaperlessClient(
            url="http://paperless:8000",
            token=auth,
            transport=transport,
        )
        client.test_connection()
        assert captured_headers["auth"] == "Token my-secret-token"
        client.close()

    def test_accept_header_pins_the_api_version(self) -> None:
        """
        Every request pins the paperless-ngx API version (OUTC-11 / D-17).

        Without the header the server picks its own default -- today v10,
        one day v11 -- and the client silently tracks it.
        """
        captured_headers: dict[str, str | None] = {}

        def handler(request: httpx2.Request) -> httpx2.Response:
            captured_headers["accept"] = request.headers.get("accept")
            return httpx2.Response(200, json={"count": 0, "results": []})

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            transport=transport,
        )
        client.test_connection()
        assert captured_headers["accept"] == "application/json; version=9"
        client.close()


class TestUploadResultContract:
    """The upload result names exactly one destination, and only a real one."""

    def test_api_delivery_carries_the_task_id(self) -> None:
        """An API delivery is its task id and nothing else."""
        result = ApiDelivery(task_id="task-123")
        assert result.task_id == "task-123"
        assert [field.name for field in dataclasses.fields(result)] == ["task_id"]

    def test_folder_delivery_carries_the_path(self, tmp_path: Path) -> None:
        """A consume-folder delivery is its path and nothing else."""
        dest = tmp_path / "consume" / "doc.pdf"
        result = FolderDelivery(path=dest)
        assert result.path == dest
        assert [field.name for field in dataclasses.fields(result)] == ["path"]

    def test_both_deliveries_are_frozen_and_slotted(self, tmp_path: Path) -> None:
        """A delivery cannot be changed after the fact or grow a field."""
        for result in (ApiDelivery(task_id="t"), FolderDelivery(path=tmp_path / "x")):
            for field in dataclasses.fields(result):
                with pytest.raises(dataclasses.FrozenInstanceError):
                    setattr(result, field.name, getattr(result, field.name))
            assert not hasattr(result, "__dict__")

    def test_upload_result_is_the_union_of_the_two(self, tmp_path: Path) -> None:
        """The alias is a plain union, so ``isinstance`` accepts it."""
        assert UploadResult == ApiDelivery | FolderDelivery
        assert isinstance(ApiDelivery(task_id="t"), UploadResult)
        assert isinstance(FolderDelivery(path=tmp_path / "x"), UploadResult)
        assert not isinstance("t", UploadResult)

    def test_upload_document_returns_an_api_delivery(self, sample_pdf: Path) -> None:
        """An accepted upload is an ApiDelivery carrying the task id."""

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json="task-abc")

        client = _poll_client(handler)
        try:
            result = client.upload_document(sample_pdf, title="Accepted")
        finally:
            client.close()
        assert result == ApiDelivery(task_id="task-abc")

    def test_upload_document_returns_a_folder_delivery(
        self, sample_pdf: Path, tmp_path: Path
    ) -> None:
        """A consume-folder fallback is a FolderDelivery carrying the path."""
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()
        result = _deliver_to_consume_dir(sample_pdf, consume_dir)
        assert result == FolderDelivery(path=consume_dir / sample_pdf.name)

    def test_null_task_id_from_paperless_is_rejected(self, sample_pdf: Path) -> None:
        """
        A JSON null task id raises instead of becoming the string "None".

        `str(response.json())` would turn a null body into "None" -- truthy and
        not None -- which then reaches poll_task as if it were a real task id.
        """

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(
                200,
                content=b"null",
                headers={"content-type": "application/json"},
            )

        transport = httpx2.MockTransport(handler)
        client = PaperlessClient(
            "http://localhost:8000",
            "token",
            transport=transport,
        )
        try:
            with pytest.raises(PaperlessError, match="no task ID"):
                client.upload_document(sample_pdf, "Null Task")
        finally:
            client.close()


def _deliver_to_consume_dir(sample_pdf: Path, consume_dir: Path) -> UploadResult:
    """
    Force the consume-directory fallback and return its result.

    Args:
        sample_pdf: The PDF to hand over.
        consume_dir: The consume directory, which already exists.

    Returns:
        What ``upload_document`` reported.

    """
    client = PaperlessClient(
        url="http://paperless:8000",
        token=_MOCK_AUTH,
        consume_dir=consume_dir,
        transport=_make_transport(_always_refused),
        max_retries=1,
    )
    try:
        return client.upload_document(sample_pdf, title="Mode test")
    finally:
        client.close()


_REFUSING_ERRNOS = [
    pytest.param(errno.EPERM, id="EPERM"),
    pytest.param(errno.EINVAL, id="EINVAL"),
    pytest.param(errno.EOPNOTSUPP, id="EOPNOTSUPP"),
    pytest.param(errno.ENOTSUP, id="ENOTSUP"),
]


class TestConsumeCopyMode:
    """
    The consume copy is 0644 whatever the umask.

    paperless-ngx often runs as another user, so a copy left to a umask of 077
    would be unreadable to it and the fallback would save nothing. The consume
    folder's own mode decides who can reach the file.
    """

    def test_consume_mode_is_0644_under_umask_077(
        self, sample_pdf: Path, tmp_path: Path
    ) -> None:
        """A umask that would give 0600 still yields a 0644 copy."""
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()

        old = os.umask(0o077)
        try:
            result = _deliver_to_consume_dir(sample_pdf, consume_dir)
        finally:
            os.umask(old)

        assert result.consume_dir_path == consume_dir / "test.pdf"
        assert stat.S_IMODE((consume_dir / "test.pdf").stat().st_mode) == 0o644
        _assert_no_staging_files(consume_dir)

    def test_consume_copy_is_never_group_writable_while_written(
        self, sample_pdf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The staging copy has its final mode before the first byte goes in.

        The consume folder is often shared with a group, so a staging file
        created with the umask's mode could be written to by others while
        the scan's bytes were going in.  A umask of 000 makes that visible.
        """
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()
        modes: list[int] = []
        original = shutil.copyfileobj

        def recording(source: BinaryIO, target: BinaryIO) -> None:
            modes.append(stat.S_IMODE(os.fstat(target.fileno()).st_mode))
            original(source, target)

        monkeypatch.setattr(shutil, "copyfileobj", recording)
        old = os.umask(0)
        try:
            _deliver_to_consume_dir(sample_pdf, consume_dir)
        finally:
            os.umask(old)

        assert modes == [0o644]
        assert stat.S_IMODE((consume_dir / "test.pdf").stat().st_mode) == 0o644

    @pytest.mark.parametrize("code", _REFUSING_ERRNOS)
    def test_consume_mode_refusal_still_delivers_the_file(
        self,
        sample_pdf: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        code: int,
    ) -> None:
        """
        A filesystem that refuses the mode change still gets the document.

        CIFS, vfat and some FUSE mounts have no Unix modes. Refusing to hand
        the scan over because its mode could not be set would lose it.
        """
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()
        calls: list[int] = []

        def refuse(_fd: int, mode: int) -> None:
            calls.append(mode)
            raise OSError(code, "refused")

        monkeypatch.setattr(os, "fchmod", refuse)
        with caplog.at_level(logging.DEBUG, logger="saneless.paperless"):
            result = _deliver_to_consume_dir(sample_pdf, consume_dir)

        assert calls == [0o644]
        assert result.consume_dir_path == consume_dir / "test.pdf"
        assert (consume_dir / "test.pdf").read_bytes() == sample_pdf.read_bytes()
        _assert_no_staging_files(consume_dir)
        assert any(
            record.levelno == logging.DEBUG and "mode" in record.getMessage()
            for record in caplog.records
            if record.name == "saneless.paperless"
        )

    def test_consume_mode_io_error_fails_and_removes_the_staging_file(
        self,
        sample_pdf: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A real failure of the mode change fails the handoff and cleans up."""
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()

        def broken(_fd: int, _mode: int) -> None:
            raise OSError(errno.EIO, "input/output error")

        monkeypatch.setattr(os, "fchmod", broken)
        with pytest.raises(
            PaperlessError, match="Could not copy the PDF to the consume directory"
        ) as exc_info:
            _deliver_to_consume_dir(sample_pdf, consume_dir)

        assert isinstance(exc_info.value.__cause__, OSError)
        assert sorted(entry.name for entry in consume_dir.iterdir()) == []


_TASK_ID_LIMIT = 64
"""The most characters of a paperless task id a log line or message carries."""

_HOSTILE_TASK_ID = "\x1b[31m" + "x" * 295
"""A 300-character task id that opens with a terminal colour escape."""


def _assert_task_id_is_tame(text: str) -> None:
    """
    Assert ``text`` carries the hostile task id only in its bounded form.

    Args:
        text: A log line or an exception message.

    """
    assert not has_control_characters(text)
    assert "x" * _TASK_ID_LIMIT not in text


def _task_id_part(message: str, after: str) -> str:
    """
    Return the task id a ``Paperless task <id> <after>`` message names.

    Args:
        message: The exception text.
        after: The words that follow the id.

    Returns:
        The text between ``Paperless task`` and ``after``.

    """
    match = re.match(rf"Paperless task (.*) {re.escape(after)}", message, re.DOTALL)
    assert match is not None, message
    return match.group(1)


def _paperless_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return every formatted line ``saneless.paperless`` logged."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "saneless.paperless"
    ]


class TestTaskIdIsBoundedInLogs:
    """
    Paperless's task id is third-party text, so it is bounded and neutralised.

    It is still sent back to paperless unchanged: only what saneless prints is
    tamed.
    """

    def test_task_id_in_the_upload_log_is_bounded(
        self, sample_pdf: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The upload's INFO line shows a short, escaped id; the result keeps it."""

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=_HOSTILE_TASK_ID)

        client = _poll_client(handler)
        try:
            with caplog.at_level(logging.INFO, logger="saneless.paperless"):
                result = client.upload_document(sample_pdf, title="Hostile id")
        finally:
            client.close()

        assert result.task_uuid == _HOSTILE_TASK_ID
        lines = [line for line in _paperless_lines(caplog) if "task" in line.lower()]
        assert lines
        for line in lines:
            _assert_task_id_is_tame(line)
        # The escape is shown, not dropped, so the operator can see it was there.
        assert any("x1b[31m" in line for line in lines)

    @pytest.mark.parametrize(
        "respond",
        [
            pytest.param(
                _task_answer({"task_id": _HOSTILE_TASK_ID, "status": "PENDING"}),
                id="pending",
            ),
            pytest.param(
                _raising(httpx2.ConnectError("connection refused")),
                id="transport-error",
            ),
        ],
    )
    def test_task_id_in_the_timeout_message_is_bounded(
        self,
        sleeps: list[float],
        caplog: pytest.LogCaptureFixture,
        respond: Callable[[int], httpx2.Response],
    ) -> None:
        """A poll timeout names a bounded id, and so does every log line."""
        sent: list[str] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            sent.append(request.url.params["task_id"])
            return respond(len(sent))

        client = _poll_client(handler)
        try:
            with (
                caplog.at_level(logging.INFO, logger="saneless.paperless"),
                pytest.raises(PaperlessTimeoutError) as exc_info,
            ):
                client.poll_task(_HOSTILE_TASK_ID, timeout=0.05)
        finally:
            client.close()

        assert sleeps
        # What goes back to paperless is the id it issued, untouched.
        assert set(sent) == {_HOSTILE_TASK_ID}
        message = str(exc_info.value)
        _assert_task_id_is_tame(message)
        task_id = _task_id_part(message, "did not finish within")
        assert len(task_id) <= _TASK_ID_LIMIT
        lines = _paperless_lines(caplog)
        assert lines
        for line in lines:
            _assert_task_id_is_tame(line)

    def test_task_id_in_the_failure_message_is_bounded(self) -> None:
        """A FAILURE names a bounded id in the message that becomes job.error."""
        client = _poll_client(
            _CountingHandler(
                _task_answer(
                    {
                        "task_id": _HOSTILE_TASK_ID,
                        "status": "FAILURE",
                        "result": "disk on fire",
                    }
                )
            )
        )
        try:
            with pytest.raises(PaperlessError) as exc_info:
                client.poll_task(_HOSTILE_TASK_ID, timeout=10)
        finally:
            client.close()

        message = str(exc_info.value)
        _assert_task_id_is_tame(message)
        assert len(_task_id_part(message, "ended FAILURE")) <= _TASK_ID_LIMIT
        assert "disk on fire" in message

    def test_task_id_in_the_success_log_is_bounded(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The completion INFO line shows a short, escaped id."""
        client = _poll_client(
            _CountingHandler(
                _task_answer({"task_id": _HOSTILE_TASK_ID, "status": "SUCCESS"})
            )
        )
        try:
            with caplog.at_level(logging.INFO, logger="saneless.paperless"):
                client.poll_task(_HOSTILE_TASK_ID, timeout=10)
        finally:
            client.close()

        lines = [line for line in _paperless_lines(caplog) if "completed" in line]
        assert len(lines) == 1
        _assert_task_id_is_tame(lines[0])
