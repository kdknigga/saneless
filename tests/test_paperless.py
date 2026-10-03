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
    PaperlessIncompatibleError,
    PaperlessTimeoutError,
    PaperlessTrustStoreError,
    PaperlessUncertainSendError,
    PaperlessUnconfirmedError,
    describe,
)
from saneless.paperless import (
    ApiDelivery,
    ConnectionProbe,
    FolderDelivery,
    PaperlessClient,
    PaperlessTiming,
    TaskDuplicate,
    TaskFiled,
    TaskOutcome,
    UploadResult,
    _not_accepted_message,
    _one_line_reason,
    _redirect_target,
    _render_error_body,
    _retry_decision,
    _RetryDecision,
    _upload_timeout,
    _without_userinfo,
)
from saneless.text_safety import has_control_characters
from saneless.vocabulary import ConnectionStatus, ErrorCategory, classify_error
from tests.fake_clock import FakeClock
from tests.golden_support import (
    DOCUMENTS_PATH,
    LOOPBACK_SESSION_COOKIE,
    LOOPBACK_TASK_ID,
    TASKS_PATH,
    LoopbackHit,
    RecordingPaperless,
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
    *,
    clock: FakeClock | None = None,
) -> PaperlessClient:
    """
    Build a client wired to the given mock handler and a fake clock.

    Args:
        handler: The transport handler.
        clock: The clock the poll's deadline and waits run on; a fresh one
            when None, so no poll ever waits in real time.

    Returns:
        The client.

    """
    fake = FakeClock() if clock is None else clock
    return PaperlessClient(
        url="http://paperless:8000",
        token=_MOCK_AUTH,
        transport=_make_transport(handler),
        timing=PaperlessTiming(clock=fake.now, sleep=fake.sleep),
    )


def _filed(outcome: TaskOutcome) -> dict[str, object]:
    """
    Return the task of a poll that filed its document.

    Args:
        outcome: What ``poll_task`` returned.

    Returns:
        The SUCCESS task paperless-ngx answered with.

    """
    assert isinstance(outcome, TaskFiled), outcome
    return outcome.task


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
    pytest.param(406, ConnectionStatus.INCOMPATIBLE, id="406"),
    pytest.param(500, ConnectionStatus.SERVER_ERROR, id="500"),
    pytest.param(503, ConnectionStatus.SERVER_ERROR, id="503"),
    pytest.param(429, ConnectionStatus.SERVER_ERROR, id="429"),
    # A redirect is paperless.url pointing at the wrong address, never a
    # server error.  These carry no Location; TestProbeConnection covers the
    # ones that do.
    pytest.param(301, ConnectionStatus.REDIRECTED, id="301"),
    pytest.param(302, ConnectionStatus.REDIRECTED, id="302"),
    pytest.param(303, ConnectionStatus.REDIRECTED, id="303"),
    pytest.param(307, ConnectionStatus.REDIRECTED, id="307"),
    pytest.param(308, ConnectionStatus.REDIRECTED, id="308"),
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

    def test_before_send_budget_seams_are_keyword_only(self) -> None:
        """
        The send budget, its clock, its sleep and the timeout are one seam.

        The budget is a time, not a count of attempts, so there is no attempt
        count to pass any more.  The four travel together as ``timing``, which
        is keyword-only and defaults to the production values.
        """
        parameters = inspect.signature(PaperlessClient.__init__).parameters
        assert list(parameters) == [
            "self",
            "url",
            "token",
            "consume_dir",
            "transport",
            "timing",
        ]
        assert parameters["timing"].kind is inspect.Parameter.KEYWORD_ONLY
        assert parameters["timing"].default == PaperlessTiming()
        fields = inspect.signature(PaperlessTiming).parameters
        assert list(fields) == ["send_budget", "clock", "sleep", "upload_timeout"]
        assert all(
            field.kind is inspect.Parameter.KEYWORD_ONLY for field in fields.values()
        )
        timing = PaperlessTiming()
        assert timing.send_budget == 60.0
        assert timing.clock is time.monotonic
        # Named rather than compared, since tests may not touch the real sleep.
        assert timing.sleep.__module__ == "time"
        assert repr(timing.sleep) == "<built-in function sleep>"
        assert timing.upload_timeout is _upload_timeout


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
        """Upload returns the task id on success."""
        task_id = "abc-123-def"

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=task_id)

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            transport=transport,
        )
        result = client.upload_document(sample_pdf, title="Test Doc")
        assert result == ApiDelivery(task_id=task_id)
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

    def test_upload_retry_on_network_error(self, sample_pdf: Path) -> None:
        """Upload retries on ConnectError and eventually succeeds."""
        call_count = {"n": 0}

        def handler(_request: httpx2.Request) -> httpx2.Response:
            call_count["n"] += 1
            if call_count["n"] <= 2:
                msg = "connection refused"
                raise httpx2.ConnectError(msg)
            return httpx2.Response(200, json="task-id-ok")

        clock = FakeClock()
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            transport=_make_transport(handler),
            timing=PaperlessTiming(clock=clock.now, sleep=clock.sleep),
        )
        result = client.upload_document(sample_pdf, title="Retry Test")
        assert result == ApiDelivery(task_id="task-id-ok")
        assert call_count["n"] == 3
        assert clock.waits == [1, 2]
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

    def test_upload_before_send_budget_spent_no_fallback(
        self, sample_pdf: Path
    ) -> None:
        """A refused connection for the whole budget fails, naming the budget."""
        handler = _CountingHandler(_raising(httpx2.ConnectError("connection refused")))
        clock = FakeClock()
        client = _upload_client(handler, clock=clock)
        try:
            with pytest.raises(PaperlessError) as exc_info:
                client.upload_document(sample_pdf, title="Fail")
        finally:
            client.close()
        assert str(exc_info.value) == (
            "Upload to Paperless at http://paperless:8000 could not connect for "
            "60s: connection refused"
        )
        assert type(exc_info.value) is PaperlessError
        assert clock.now() == 60
        assert sum(clock.waits) == 60

    def test_upload_before_send_budget_spent_with_fallback(
        self, sample_pdf: Path, tmp_path: Path
    ) -> None:
        """Upload falls back to the consume directory once the budget is spent."""
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()
        handler = _CountingHandler(_raising(httpx2.ConnectError("connection refused")))
        clock = FakeClock()
        client = _upload_client(handler, consume_dir=consume_dir, clock=clock)
        result = client.upload_document(sample_pdf, title="Fallback")
        # PDF should have been copied to consume dir
        copied = list(consume_dir.iterdir())
        assert len(copied) == 1
        assert copied[0].name == "test.pdf"
        assert copied[0].read_bytes() == sample_pdf.read_bytes()
        _assert_no_staging_files(consume_dir)
        # The result names the exact file the PDF was copied to -- something
        # the old magic-string sentinel could not carry.
        assert result == FolderDelivery(path=copied[0])
        assert clock.now() == 60
        client.close()


# ---------------------------------------------------------------------------
# Upload failure translation (EXC-01, D-08, D-10)
# ---------------------------------------------------------------------------


# Errors that prove the upload never reached paperless-ngx whole.
_BEFORE_SEND_CASES = [
    pytest.param(httpx2.ConnectError, id="connect-error"),
    pytest.param(httpx2.ConnectTimeout, id="connect-timeout"),
    pytest.param(httpx2.PoolTimeout, id="pool-timeout"),
    pytest.param(httpx2.ProxyError, id="proxy-error"),
    pytest.param(httpx2.WriteTimeout, id="write-timeout"),
]

# What the spent budget's message says each of those failed to do.  Only a
# connection that was never made "could not connect"; a stalled body write had
# connected, and a pool with no free connection never tried.
_BUDGET_SPENT_WORDING: dict[type[httpx2.TransportError], str] = {
    httpx2.ConnectError: "could not connect",
    httpx2.ConnectTimeout: "could not connect",
    httpx2.PoolTimeout: "could not deliver the upload",
    httpx2.ProxyError: "could not connect",
    httpx2.WriteTimeout: "could not deliver the upload",
}

# Errors after which paperless-ngx may hold the document.
_AFTER_SEND_TRANSPORT_CASES = [
    pytest.param(httpx2.ReadTimeout, id="read-timeout"),
    pytest.param(httpx2.ReadError, id="read-error"),
    pytest.param(httpx2.RemoteProtocolError, id="remote-protocol-error"),
    pytest.param(httpx2.WriteError, id="write-error"),
    pytest.param(httpx2.CloseError, id="close-error"),
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
    handler: _CountingHandler,
    consume_dir: Path | None = None,
    *,
    clock: FakeClock | None = None,
    send_budget: float = 60.0,
) -> PaperlessClient:
    """
    Build an upload client wired to the counting handler and a fake clock.

    Args:
        handler: The transport handler.
        consume_dir: The fallback folder, or None for none.
        clock: The clock the before-send waits run on; a fresh one when None,
            so no test ever waits the real budget out.
        send_budget: How long before-send failures are retried for.

    Returns:
        The client.

    """
    fake = FakeClock() if clock is None else clock
    return PaperlessClient(
        url="http://paperless:8000",
        token=_MOCK_AUTH,
        consume_dir=consume_dir,
        transport=_make_transport(handler),
        timing=PaperlessTiming(
            send_budget=send_budget, clock=fake.now, sleep=fake.sleep
        ),
    )


@pytest.fixture
def upload_clock() -> FakeClock:
    """Provide a fake clock for the upload's before-send waits, so none pauses."""
    return FakeClock()


@pytest.fixture
def poll_clock() -> FakeClock:
    """Provide a fake clock for the task poll's deadline and backoff waits."""
    return FakeClock()


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

    @pytest.mark.parametrize(
        "make_path",
        [
            pytest.param(lambda root: root / "absent-ca.crt", id="missing-file"),
            pytest.param(lambda root: root, id="directory"),
        ],
    )
    def test_an_unreadable_trust_store_is_its_own_error_with_its_own_fix(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        make_path: Callable[[Path], Path],
    ) -> None:
        """
        The trust-store refusal is a PaperlessTrustStoreError with a next step.

        It stays a PaperlessError, so ``classify_error`` keeps filing it under
        UPLOAD and ``serve`` keeps exiting 3, but the fix it carries names
        the two variables rather than the category's generic advice.
        """
        monkeypatch.setenv("SSL_CERT_FILE", str(make_path(tmp_path)))
        monkeypatch.delenv("SSL_CERT_DIR", raising=False)
        # No transport= argument, for the reason given in the first test.
        with pytest.raises(PaperlessTrustStoreError) as exc_info:
            PaperlessClient("https://paperless.example.com", "tok-SECRET-5d1")
        exc = exc_info.value
        assert isinstance(exc, PaperlessError)
        assert classify_error(exc) is ErrorCategory.UPLOAD
        assert str(exc).startswith(
            "Could not build the TLS trust store for Paperless at "
            "https://paperless.example.com: "
        )
        assert str(exc).endswith("; check SSL_CERT_FILE and SSL_CERT_DIR")
        assert exc.next_step is not None
        assert "SSL_CERT_FILE" in exc.next_step
        assert "SSL_CERT_DIR" in exc.next_step
        assert "tok-SECRET-5d1" not in exc.next_step


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
        self,
        sample_pdf: Path,
        upload_clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A URL with no scheme, which httpx2 cannot parse as userinfo, is cut too."""
        caplog.set_level(logging.WARNING, logger="saneless.paperless")
        client = PaperlessClient(
            url=f"scanner:{_URL_SECRET}@paperless:8000",
            token=_MOCK_AUTH,
            timing=PaperlessTiming(clock=upload_clock.now, sleep=upload_clock.sleep),
        )
        try:
            with pytest.raises(ConfigError) as exc_info:
                client.upload_document(sample_pdf, title="No scheme")
        finally:
            client.close()
        assert str(exc_info.value) == _UNSET_URL_UPLOAD_MESSAGE
        assert exc_info.value.__cause__ is None
        assert all(_URL_SECRET not in message for message in caplog.messages)
        assert upload_clock.waits == []

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
        self, sample_pdf: Path, upload_clock: FakeClock
    ) -> None:
        """A redirect target carrying userinfo is redacted like the base URL."""
        target = f"https://scanner:{_URL_SECRET}@paperless.example/api/"
        handler = _CountingHandler(
            _answering(httpx2.Response(302, headers={"location": target}))
        )
        client = _upload_client(handler, clock=upload_clock)
        try:
            with pytest.raises(PaperlessError) as exc_info:
                client.upload_document(sample_pdf, title="Redirected")
        finally:
            client.close()
        assert str(exc_info.value) == (
            "Paperless redirected the upload (302 Found) to "
            "https://paperless.example/api/; check paperless.url"
        )
        assert upload_clock.waits == []


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
    # The request could not be sent at all, on any attempt.
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
    # Nothing, or never the whole body, can have reached paperless-ngx.
    pytest.param(
        httpx2.ConnectError("refused"), _RetryDecision.BEFORE_SEND, id="connect"
    ),
    pytest.param(
        httpx2.ConnectTimeout("slow"),
        _RetryDecision.BEFORE_SEND,
        id="connect-timeout",
    ),
    pytest.param(
        httpx2.PoolTimeout("no free connection"),
        _RetryDecision.BEFORE_SEND,
        id="pool-timeout",
    ),
    pytest.param(
        httpx2.ProxyError("tunnel refused"),
        _RetryDecision.BEFORE_SEND,
        id="proxy-error",
    ),
    pytest.param(
        httpx2.WriteTimeout("body stalled"),
        _RetryDecision.BEFORE_SEND,
        id="write-timeout",
    ),
    # The whole body may have been read, so the document may be stored.
    pytest.param(
        httpx2.ReadTimeout("slow"), _RetryDecision.AFTER_SEND, id="read-timeout"
    ),
    pytest.param(httpx2.ReadError("reset"), _RetryDecision.AFTER_SEND, id="read-error"),
    pytest.param(
        httpx2.WriteError("pipe"), _RetryDecision.AFTER_SEND, id="write-error"
    ),
    pytest.param(
        httpx2.RemoteProtocolError("server hung up"),
        _RetryDecision.AFTER_SEND,
        id="remote-protocol-error",
    ),
    pytest.param(
        httpx2.CloseError("close failed"), _RetryDecision.AFTER_SEND, id="close-error"
    ),
    pytest.param(
        httpx2.DecodingError("corrupt gzip", request=_CLASSIFIED_REQUEST),
        _RetryDecision.AFTER_SEND,
        id="decoding-error",
    ),
    pytest.param(_status_error(500), _RetryDecision.AFTER_SEND, id="500"),
    pytest.param(_status_error(502), _RetryDecision.AFTER_SEND, id="502"),
    pytest.param(_status_error(503), _RetryDecision.AFTER_SEND, id="503"),
    pytest.param(_status_error(504), _RetryDecision.AFTER_SEND, id="504"),
    # This server does not accept the API version asked for; nothing is stored.
    pytest.param(_status_error(406), _RetryDecision.INCOMPATIBLE, id="406"),
    # An answer that would be the same on every attempt.
    pytest.param(_status_error(400), _RetryDecision.REFUSED, id="400"),
    pytest.param(_status_error(401), _RetryDecision.REFUSED, id="401"),
    pytest.param(_status_error(404), _RetryDecision.REFUSED, id="404"),
    pytest.param(_status_error(302), _RetryDecision.REFUSED, id="302"),
    # Anything else httpx2 raises.
    pytest.param(
        httpx2.TooManyRedirects("Exceeded maximum allowed redirects."),
        _RetryDecision.UNEXPECTED,
        id="too-many-redirects",
    ),
]


class TestRetryDecision:
    """One pure function decides what the client does with every httpx2 error."""

    @pytest.mark.parametrize(("exc", "expected"), _RETRY_DECISION_CASES)
    def test_retry_decision_classifies_each_error(
        self, exc: httpx2.HTTPError, expected: _RetryDecision
    ) -> None:
        """
        Each error is sorted by whether the upload can have arrived.

        Only an error that proves the body never fully left is sent again; any
        answer, and any failure once the body may have been read, is not, since
        a resend could store the document twice.  ``LocalProtocolError`` and
        ``UnsupportedProtocol`` are both ``TransportError`` subclasses, so a
        classifier that tests for ``TransportError`` first would retry them.
        """
        assert _retry_decision(exc) is expected


class TestUploadFailureTranslation:
    """EXC-01 / D-10 / M-17: every upload failure ends as a PaperlessError."""

    @pytest.mark.parametrize("exc_type", _BEFORE_SEND_CASES)
    def test_before_send_budget_retries_then_raises(
        self,
        exc_type: type[httpx2.TransportError],
        sample_pdf: Path,
        upload_clock: FakeClock,
    ) -> None:
        """
        A failure that proves nothing arrived is retried for the whole budget.

        The waits double up to a cap of 5 s, and the last one is cut to what is
        left, so the attempts stop exactly when the budget runs out.
        """
        failure = exc_type("upstream went away")
        handler = _CountingHandler(_raising(failure))
        client = _upload_client(handler, clock=upload_clock)
        try:
            with pytest.raises(PaperlessError) as exc_info:
                client.upload_document(sample_pdf, title="Transient")
        finally:
            client.close()
        assert str(exc_info.value) == (
            f"Upload to Paperless at http://paperless:8000 "
            f"{_BUDGET_SPENT_WORDING[exc_type]} for 60s: upstream went away"
        )
        assert type(exc_info.value) is PaperlessError
        assert exc_info.value.__cause__ is failure
        assert upload_clock.waits == [1, 2, 4, *[5] * 10, 3]
        assert handler.calls == len(upload_clock.waits) + 1
        assert upload_clock.now() == 60

    @pytest.mark.parametrize("exc_type", _BEFORE_SEND_CASES)
    def test_before_send_budget_spent_falls_back(
        self,
        exc_type: type[httpx2.TransportError],
        sample_pdf: Path,
        tmp_path: Path,
        upload_clock: FakeClock,
    ) -> None:
        """With a consume directory the spent budget takes the fallback."""
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()
        handler = _CountingHandler(_raising(exc_type("upstream went away")))
        client = _upload_client(handler, consume_dir=consume_dir, clock=upload_clock)
        try:
            result = client.upload_document(sample_pdf, title="Transient")
        finally:
            client.close()
        assert result == FolderDelivery(path=consume_dir / "test.pdf")
        assert (consume_dir / "test.pdf").read_bytes() == sample_pdf.read_bytes()
        assert upload_clock.now() == 60

    def test_before_send_budget_rides_out_a_restart(
        self, sample_pdf: Path, upload_clock: FakeClock
    ) -> None:
        """
        A paperless-ngx that refuses connections for 59 s still gets the scan.

        The restart outlasts every wait but the budget, so exactly one
        document is accepted and nothing waits past 60 s.
        """
        accepted: list[int] = []

        def respond(call: int) -> httpx2.Response:
            if upload_clock.now() < 59:
                msg = "connection refused"
                raise httpx2.ConnectError(msg)
            accepted.append(call)
            return httpx2.Response(200, json="task-after-restart")

        handler = _CountingHandler(respond)
        client = _upload_client(handler, clock=upload_clock)
        try:
            result = client.upload_document(sample_pdf, title="Restart")
        finally:
            client.close()
        assert result == ApiDelivery(task_id="task-after-restart")
        assert accepted == [handler.calls]
        assert upload_clock.waits[:4] == [1, 2, 4, 5]
        assert max(upload_clock.waits) <= 5
        assert sum(upload_clock.waits) <= 60

    def test_before_send_budget_counts_the_time_attempts_take(
        self, sample_pdf: Path, upload_clock: FakeClock
    ) -> None:
        """
        Time spent inside an attempt comes out of the budget as well.

        Each attempt here stalls 10 s before its connection is refused, the
        way a connect timeout does, so the budget is spent after five.
        """

        def respond(_call: int) -> httpx2.Response:
            upload_clock.advance(10)
            msg = "timed out"
            raise httpx2.ConnectTimeout(msg)

        handler = _CountingHandler(respond)
        client = _upload_client(handler, clock=upload_clock)
        try:
            with pytest.raises(PaperlessError, match="could not connect for 60s"):
                client.upload_document(sample_pdf, title="Black hole")
        finally:
            client.close()
        assert handler.calls == 5
        assert upload_clock.waits == [1, 2, 4, 5]

    def test_before_send_budget_zero_makes_one_attempt(
        self, sample_pdf: Path, upload_clock: FakeClock
    ) -> None:
        """A zero budget tries once and neither waits nor retries."""
        handler = _CountingHandler(_raising(httpx2.ConnectError("connection refused")))
        client = _upload_client(handler, clock=upload_clock, send_budget=0.0)
        try:
            with pytest.raises(PaperlessError) as exc_info:
                client.upload_document(sample_pdf, title="Once")
        finally:
            client.close()
        assert handler.calls == 1
        assert upload_clock.waits == []
        assert str(exc_info.value) == (
            "Upload to Paperless at http://paperless:8000 could not connect for "
            "0s: connection refused"
        )

    @pytest.mark.parametrize("consume_name", ["", "consume"], ids=["plain", "fallback"])
    @pytest.mark.parametrize("exc_type", _AFTER_SEND_TRANSPORT_CASES)
    def test_after_send_transport_failure_is_never_resent_or_copied(
        self,
        exc_type: type[httpx2.TransportError],
        consume_name: str,
        sample_pdf: Path,
        tmp_path: Path,
        upload_clock: FakeClock,
    ) -> None:
        """
        A failure once the body may have been read is sent once and not copied.

        A resend or a consume-folder copy could make a second document of the
        scan; the error says it may already be in paperless-ngx instead.
        """
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()
        failure = exc_type("upstream went away")
        handler = _CountingHandler(_raising(failure))
        client = _upload_client(
            handler,
            consume_dir=tmp_path / consume_name if consume_name else None,
            clock=upload_clock,
        )
        try:
            with pytest.raises(PaperlessUncertainSendError) as exc_info:
                client.upload_document(sample_pdf, title="Maybe")
        finally:
            client.close()
        assert handler.calls == 1
        assert upload_clock.waits == []
        assert list(consume_dir.iterdir()) == []
        message = str(exc_info.value)
        assert message.startswith("Upload to Paperless at http://paperless:8000 ")
        assert "upstream went away" in message
        assert message.endswith("; it may have reached paperless-ngx")
        assert exc_info.value.__cause__ is failure

    @pytest.mark.parametrize("consume_name", ["", "consume"], ids=["plain", "fallback"])
    @pytest.mark.parametrize("status", [500, 502, 503, 504])
    def test_after_send_5xx_is_never_resent_or_copied(
        self,
        status: int,
        consume_name: str,
        sample_pdf: Path,
        tmp_path: Path,
        upload_clock: FakeClock,
    ) -> None:
        """
        Any 5xx on the upload is an answer, so the body may have been read.

        A proxy's 502 or 504 can follow a body paperless-ngx stored, and
        nothing on the wire tells it apart from one that did not.
        """
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()
        handler = _CountingHandler(_answering(httpx2.Response(status, text="down\n")))
        client = _upload_client(
            handler,
            consume_dir=tmp_path / consume_name if consume_name else None,
            clock=upload_clock,
        )
        try:
            with pytest.raises(PaperlessUncertainSendError) as exc_info:
                client.upload_document(sample_pdf, title="Down")
        finally:
            client.close()
        reason = httpx2.codes.get_reason_phrase(status)
        assert str(exc_info.value) == (
            "Upload to Paperless at http://paperless:8000 got no usable answer: "
            f"{status} {reason}: down; it may have reached paperless-ngx"
        )
        assert isinstance(exc_info.value.__cause__, httpx2.HTTPStatusError)
        assert handler.calls == 1
        assert upload_clock.waits == []
        assert list(consume_dir.iterdir()) == []

    def test_after_send_drop_is_not_resent_even_if_a_resend_would_succeed(
        self, sample_pdf: Path, upload_clock: FakeClock
    ) -> None:
        """A dropped connection after the body is final, not a first attempt."""

        def respond(call: int) -> httpx2.Response:
            if call == 1:
                msg = "Server disconnected without sending a response."
                raise httpx2.RemoteProtocolError(msg)
            return httpx2.Response(200, json="task-id")

        handler = _CountingHandler(respond)
        client = _upload_client(handler, clock=upload_clock)
        try:
            with pytest.raises(PaperlessUncertainSendError):
                client.upload_document(sample_pdf, title="Flaky proxy")
        finally:
            client.close()
        assert handler.calls == 1

    def test_after_send_empty_read_timeout_names_its_class(
        self, sample_pdf: Path, upload_clock: FakeClock
    ) -> None:
        """An empty ``ReadTimeout("")`` still says what happened (describe rule)."""
        handler = _CountingHandler(_raising(httpx2.ReadTimeout("")))
        client = _upload_client(handler, clock=upload_clock)
        try:
            with pytest.raises(PaperlessUncertainSendError) as exc_info:
                client.upload_document(sample_pdf, title="Silent timeout")
        finally:
            client.close()
        assert "(ReadTimeout)" in str(exc_info.value)
        assert upload_clock.waits == []

    @pytest.mark.parametrize("consume_name", ["", "consume"], ids=["plain", "fallback"])
    def test_406_is_incompatible_final_and_never_copied(
        self,
        consume_name: str,
        sample_pdf: Path,
        tmp_path: Path,
        upload_clock: FakeClock,
    ) -> None:
        """
        A 406 is a server that does not speak API 9 or 10: one POST, no copy.

        paperless-ngx refuses the version before it reads the upload, so
        nothing was stored and a copy would land without its metadata.
        """
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()
        handler = _CountingHandler(
            _answering(
                httpx2.Response(406, json={"detail": 'Invalid version in "Accept".'})
            )
        )
        client = _upload_client(
            handler,
            consume_dir=tmp_path / consume_name if consume_name else None,
            clock=upload_clock,
        )
        try:
            with pytest.raises(PaperlessIncompatibleError) as exc_info:
                client.upload_document(sample_pdf, title="Too old")
        finally:
            client.close()
        assert str(exc_info.value) == (
            "Paperless at http://paperless:8000 does not accept API version 9 or "
            "10; saneless needs paperless-ngx 2.16 or later"
        )
        assert exc_info.value.__cause__ is None
        assert handler.calls == 1
        assert upload_clock.waits == []
        assert list(consume_dir.iterdir()) == []

    def test_unsupported_protocol_url_fails_fast_without_fallback(
        self, sample_pdf: Path, upload_clock: FakeClock
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
        client = _upload_client(handler, clock=upload_clock)
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
        assert upload_clock.waits == []

    def test_unsupported_protocol_url_never_takes_the_fallback(
        self, sample_pdf: Path, tmp_path: Path, upload_clock: FakeClock
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
        client = _upload_client(handler, consume_dir=consume_dir, clock=upload_clock)
        try:
            with pytest.raises(ConfigError):
                client.upload_document(sample_pdf, title="No scheme")
        finally:
            client.close()
        assert handler.calls == 1
        assert upload_clock.waits == []
        assert list(consume_dir.iterdir()) == []

    def test_unsupported_protocol_logs_no_attempt_and_copies_nothing(
        self,
        sample_pdf: Path,
        tmp_path: Path,
        upload_clock: FakeClock,
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
        client = _upload_client(handler, consume_dir=consume_dir, clock=upload_clock)
        try:
            with pytest.raises(ConfigError):
                client.upload_document(sample_pdf, title="No scheme")
        finally:
            client.close()
        assert upload_clock.waits == []
        assert not consume_dir.exists()
        assert [
            record.getMessage()
            for record in caplog.records
            if record.name == "saneless.paperless" and record.levelno >= logging.INFO
        ] == []

    def test_empty_url_is_a_configuration_error_through_the_real_transport(
        self, sample_pdf: Path, tmp_path: Path, upload_clock: FakeClock
    ) -> None:
        """
        An unset paperless.url fails at once, names the setting, copies nothing.

        The real transport, not a mock: this is the one way UnsupportedProtocol
        is still reachable once a set URL is checked at load.
        """
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()
        client = PaperlessClient(
            url="",
            token=_MOCK_AUTH,
            consume_dir=consume_dir,
            timing=PaperlessTiming(clock=upload_clock.now, sleep=upload_clock.sleep),
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
        assert upload_clock.waits == []
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
        upload_clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        Third-party text naming the token is struck before it is interpolated.

        A transport error whose own text carries the token -- as h11's refusal
        of a header value does -- is still retried when it proves nothing was
        sent, and neither the retry warnings nor the final message may repeat
        it.  The rest of the text survives, so the line still says what
        happened.
        """
        caplog.set_level(logging.WARNING, logger="saneless.paperless")
        failure = httpx2.ConnectError(f"refused: Token {token} user:pass@h")
        handler = _CountingHandler(_raising(failure))
        client = PaperlessClient(
            url="http://paperless:8000",
            token=token,
            timing=PaperlessTiming(
                send_budget=3.0, clock=upload_clock.now, sleep=upload_clock.sleep
            ),
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
        assert len(warnings) == 2
        assert "refused: Token" in str(exc_info.value)
        for text in [str(exc_info.value), *warnings]:
            _assert_token_absent(token, text)

    @pytest.mark.parametrize("consume_name", ["", "consume"], ids=["plain", "fallback"])
    def test_4xx_body_is_rendered_and_never_falls_back(
        self,
        consume_name: str,
        sample_pdf: Path,
        tmp_path: Path,
        upload_clock: FakeClock,
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
            handler,
            consume_dir=tmp_path / consume_name if consume_name else None,
            clock=upload_clock,
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
        assert upload_clock.waits == []
        assert not consume_dir.exists()

    @pytest.mark.parametrize("consume_name", ["", "consume"], ids=["plain", "fallback"])
    def test_redirect_fails_fast_naming_its_target(
        self,
        consume_name: str,
        sample_pdf: Path,
        tmp_path: Path,
        upload_clock: FakeClock,
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
            handler,
            consume_dir=tmp_path / consume_name if consume_name else None,
            clock=upload_clock,
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
        assert upload_clock.waits == []
        assert not consume_dir.exists()

    def test_non_redirect_3xx_fails_fast_by_its_status(
        self, sample_pdf: Path, upload_clock: FakeClock
    ) -> None:
        """A 3xx with no target is final too, reported by status and body."""
        handler = _CountingHandler(_answering(httpx2.Response(304)))
        client = _upload_client(handler, clock=upload_clock)
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
        assert upload_clock.waits == []

    def test_before_send_retry_log_line_is_one_line(
        self,
        sample_pdf: Path,
        upload_clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        Each retry is logged on one line, with the time left in the budget.

        Library text can span lines; a log line that did too could be read as
        two records.  No line is logged for the last attempt: the error it
        raises says what happened.
        """
        caplog.set_level(logging.WARNING, logger="saneless.paperless")
        handler = _CountingHandler(_raising(httpx2.ConnectError("refused\n  by peer")))
        client = _upload_client(handler, clock=upload_clock, send_budget=3.0)
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
            "Upload attempt 1 failed; retrying for up to 3s more: refused by peer",
            "Upload attempt 2 failed; retrying for up to 2s more: refused by peer",
        ]
        assert upload_clock.waits == [1, 2]

    @pytest.mark.parametrize("consume_name", ["", "consume"], ids=["plain", "fallback"])
    def test_after_send_non_json_200_is_uncertain_and_not_copied(
        self,
        consume_name: str,
        sample_pdf: Path,
        tmp_path: Path,
        upload_clock: FakeClock,
    ) -> None:
        """
        A 200 that is not JSON may still be a stored document.

        A login page served with 200 is the usual cause, but paperless-ngx
        answered 200 and so may have read the body: nothing is resent or
        copied.
        """
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()
        handler = _CountingHandler(
            _answering(httpx2.Response(200, text="<html>login</html>"))
        )
        client = _upload_client(
            handler,
            consume_dir=tmp_path / consume_name if consume_name else None,
            clock=upload_clock,
        )
        try:
            with pytest.raises(PaperlessUncertainSendError) as exc_info:
                client.upload_document(sample_pdf, title="Login page")
        finally:
            client.close()
        assert str(exc_info.value) == (
            "Paperless at http://paperless:8000 answered the upload with a body "
            "that is not JSON: <html>login</html>; it may have reached "
            "paperless-ngx"
        )
        assert isinstance(exc_info.value.__cause__, ValueError)
        assert handler.calls == 1
        assert upload_clock.waits == []
        assert list(consume_dir.iterdir()) == []

    @pytest.mark.parametrize(
        ("body", "shown"),
        [
            pytest.param(b"42", "42", id="number"),
            pytest.param(b"true", "true", id="bool"),
            pytest.param(b"{}", "{}", id="object"),
            pytest.param(b'""', '""', id="empty-string"),
            pytest.param(b'"   "', '" "', id="blank-string"),
            pytest.param(b"null", "null", id="null"),
        ],
    )
    def test_after_send_task_id_that_is_not_a_string_is_uncertain(
        self,
        body: bytes,
        shown: str,
        sample_pdf: Path,
        tmp_path: Path,
        upload_clock: FakeClock,
    ) -> None:
        """
        Only a non-empty string is a task id; anything else is never polled.

        The server answered 200, so the document may be stored: the answer is
        named, nothing is resent and nothing is copied, though a consume
        directory is configured.
        """
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()
        handler = _CountingHandler(
            _answering(
                httpx2.Response(
                    200, content=body, headers={"content-type": "application/json"}
                )
            )
        )
        client = _upload_client(handler, consume_dir=consume_dir, clock=upload_clock)
        try:
            with pytest.raises(PaperlessUncertainSendError) as exc_info:
                client.upload_document(sample_pdf, title="Odd answer")
        finally:
            client.close()
        assert str(exc_info.value) == (
            f"Paperless at http://paperless:8000 answered the upload with {shown}, "
            "which is not a task ID; it may have reached paperless-ngx"
        )
        assert handler.calls == 1
        assert upload_clock.waits == []
        assert list(consume_dir.iterdir()) == []

    def test_unreadable_pdf_is_a_paperless_error(
        self, tmp_path: Path, upload_clock: FakeClock
    ) -> None:
        """
        IN-08: an OSError opening the PDF leaves as a PaperlessError, too.

        The class promises that whatever goes wrong on the upload path is a
        ``PaperlessError``; a PDF gone from the workspace used to escape as a
        raw ``FileNotFoundError``, which the CLI reports as a saneless bug.
        """
        missing = tmp_path / "gone.pdf"
        handler = _CountingHandler(_answering(httpx2.Response(200, json="task-id")))
        client = _upload_client(
            handler, consume_dir=tmp_path / "consume", clock=upload_clock
        )
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
        assert upload_clock.waits == []

    def test_other_http_error_raises_at_once(
        self, sample_pdf: Path, upload_clock: FakeClock
    ) -> None:
        """Any remaining httpx2.HTTPError (TooManyRedirects) is wrapped and chained."""
        failure = httpx2.TooManyRedirects("Exceeded maximum allowed redirects.")
        handler = _CountingHandler(_raising(failure))
        client = _upload_client(handler, clock=upload_clock)
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
        assert upload_clock.waits == []


class TestUploadReadTimeout:
    """The upload's read timeout grows with the PDF, since the answer waits on it."""

    @pytest.mark.parametrize(
        ("size_bytes", "read"),
        [
            pytest.param(0, 30.0, id="empty"),
            pytest.param(50 * 2**20, 80.0, id="50-mib"),
            pytest.param(270 * 2**20, 300.0, id="at-the-cap"),
            pytest.param(400 * 2**20, 300.0, id="400-mib"),
        ],
    )
    def test_upload_read_timeout_scales_with_the_size(
        self, size_bytes: int, read: float
    ) -> None:
        """30 s plus 1 s per MiB, capped at 300 s; the connect stays at 10 s."""
        timeout = _upload_timeout(size_bytes)
        assert timeout.read == read
        assert timeout.connect == 10.0
        assert timeout.write == 30.0
        assert timeout.pool == 30.0

    def test_upload_read_timeout_is_sent_on_the_post(self, sample_pdf: Path) -> None:
        """The POST carries the size-scaled timeout; nothing else does."""
        timeouts: dict[str, object] = {}

        def handler(request: httpx2.Request) -> httpx2.Response:
            timeouts[request.url.path] = request.extensions["timeout"]
            if request.url.path == DOCUMENTS_PATH:
                return httpx2.Response(200, json="task-id")
            return httpx2.Response(200, json={"count": 0, "results": []})

        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            transport=_make_transport(handler),
        )
        try:
            client.upload_document(sample_pdf, title="Timed")
            client.get_tags()
        finally:
            client.close()
        size = sample_pdf.stat().st_size
        assert timeouts[DOCUMENTS_PATH] == {
            "connect": 10.0,
            "read": 30.0 + size / 2**20,
            "write": 30.0,
            "pool": 30.0,
        }
        assert timeouts["/api/tags/"] == {
            "connect": 30.0,
            "read": 30.0,
            "write": 30.0,
            "pool": 30.0,
        }

    def test_upload_read_timeout_seam_is_given_the_file_size(
        self, sample_pdf: Path
    ) -> None:
        """An injected ``upload_timeout`` is asked once, with the PDF's size."""
        sizes: list[int] = []
        reads: list[object] = []

        def tiny(size_bytes: int) -> httpx2.Timeout:
            sizes.append(size_bytes)
            return httpx2.Timeout(5.0, read=1.5)

        def handler(request: httpx2.Request) -> httpx2.Response:
            reads.append(request.extensions["timeout"]["read"])
            return httpx2.Response(200, json="task-id")

        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            transport=_make_transport(handler),
            timing=PaperlessTiming(upload_timeout=tiny),
        )
        try:
            client.upload_document(sample_pdf, title="Seam")
        finally:
            client.close()
        assert sizes == [sample_pdf.stat().st_size]
        assert reads == [1.5]

    def test_upload_read_timeout_message_names_the_budget_and_size(
        self, tmp_path: Path, upload_clock: FakeClock
    ) -> None:
        """A read timeout says how long it waited and for how large a file."""
        pdf = tmp_path / "large.pdf"
        pdf.write_bytes(b"%PDF-1.4 " + b"0" * (3_000_000 - 9))
        handler = _CountingHandler(_raising(httpx2.ReadTimeout("timed out")))
        client = _upload_client(handler, clock=upload_clock)
        try:
            with pytest.raises(PaperlessUncertainSendError) as exc_info:
                client.upload_document(pdf, title="Large")
        finally:
            client.close()
        assert str(exc_info.value) == (
            "Upload to Paperless at http://paperless:8000 got no answer within "
            "33s, the time allowed for a 3.0 MB PDF (timed out); it may have "
            "reached paperless-ngx"
        )
        assert handler.calls == 1


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
    def test_reason_phrase_has_the_token_struck(
        self, render: Callable[[httpx2.Response], str]
    ) -> None:
        """A proxy that quotes the Authorization header in its reason is struck."""
        response = httpx2.Response(
            400,
            json={"detail": "no"},
            extensions={"reason_phrase": f"Bad Token {_MOCK_AUTH}".encode()},
        )
        result = render(response)
        assert "400 Bad Token ***" in result
        _assert_token_absent(_MOCK_AUTH, result)

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
            result = _filed(client.poll_task("t1", timeout=10))
            assert str(result["status"]).upper() == "SUCCESS"
        finally:
            client.close()

    @pytest.mark.parametrize("build_payload", _API_SHAPES)
    def test_failure_is_unconfirmed_across_api_versions(
        self,
        build_payload: Callable[[str, str | None], object],
    ) -> None:
        """
        A failed task raises, carrying the message Paperless supplied.

        paperless-ngx answered the upload with a task id, so it received the
        document; a failure after that is "received, not confirmed", never a
        plain failed upload that invites a rescan.
        """

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=build_payload("FAILURE", "disk on fire"))

        client = _poll_client(handler)
        try:
            with pytest.raises(PaperlessUnconfirmedError) as exc_info:
                client.poll_task("t1", timeout=10)
        finally:
            client.close()
        assert str(exc_info.value) == "Paperless task t1 ended FAILURE: disk on fire"

    @pytest.mark.parametrize("build_payload", _API_SHAPES)
    def test_revoked_says_the_task_was_cancelled(
        self,
        build_payload: Callable[[str, str | None], object],
    ) -> None:
        """
        REVOKED is terminal, and reads as a cancellation, not a failure.

        It is one of paperless-ngx's COMPLETE_STATUSES, so looping on it
        would turn an administrative cancellation into a misattributed
        timeout that also blocks the worker for the whole budget.
        """

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=build_payload("REVOKED", "cancelled"))

        client = _poll_client(handler)
        try:
            with pytest.raises(PaperlessUnconfirmedError) as exc_info:
                client.poll_task("t1", timeout=10)
        finally:
            client.close()
        assert str(exc_info.value) == (
            "paperless-ngx cancelled task t1 before it finished; "
            "the document was not filed"
        )

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
            result = _filed(client.poll_task("t1", timeout=30))
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
            with pytest.raises(PaperlessUnconfirmedError, match="no message"):
                client.poll_task("t1", timeout=10)
        finally:
            client.close()

    @pytest.mark.parametrize("status", [401, 403, 404])
    def test_a_refusal_ends_the_poll_at_once(
        self, status: int, poll_clock: FakeClock
    ) -> None:
        """
        A refused token or a moved endpoint is reported immediately.

        The counter is the point of the test: a revoked token used to be
        silently re-polled for the full 300 s and then misreported as a
        timeout.  The upload was accepted before the poll began, so the error
        is "received, not confirmed".
        """
        handler = _CountingHandler(_answering(httpx2.Response(status, text="no")))
        client = _poll_client(handler, clock=poll_clock)
        try:
            with pytest.raises(PaperlessUnconfirmedError, match=str(status)):
                client.poll_task("t1", timeout=30)
        finally:
            client.close()
        assert handler.calls == 1
        assert poll_clock.waits == []

    def test_a_406_ends_the_poll_with_the_version_wording(
        self, poll_clock: FakeClock
    ) -> None:
        """A 406 names the versions saneless needs, and the task it held."""
        handler = _CountingHandler(
            _answering(httpx2.Response(406, json={"detail": "Invalid version"}))
        )
        client = _poll_client(handler, clock=poll_clock)
        try:
            with pytest.raises(PaperlessUnconfirmedError) as exc_info:
                client.poll_task("t1", timeout=30)
        finally:
            client.close()
        message = str(exc_info.value)
        assert "2.16" in message
        assert "API version 9 or 10" in message
        assert "t1" in message
        assert "Invalid version" not in message
        assert handler.calls == 1
        assert poll_clock.waits == []

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

    @pytest.mark.parametrize("status", [401, 404])
    def test_an_immediate_failure_html_body_cannot_flood_the_message(
        self, status: int
    ) -> None:
        """
        A 5 KB error page on a refusal becomes one short line.

        That message is recorded in the job store and shown in the web status
        area and on the terminal, so neither its length nor a newline may
        come from the upstream body.
        """
        page = "<html>\n<body>\n" + ("<p>Refused</p>\n" * 330) + "</body></html>"

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(status, text=page)

        client = _poll_client(handler)
        try:
            with pytest.raises(PaperlessUnconfirmedError) as exc_info:
                client.poll_task("t1", timeout=30)
        finally:
            client.close()
        message = str(exc_info.value)
        assert "\n" not in message
        assert len(message) < 300

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

    def test_timeout_is_catchable_as_unconfirmed_and_as_a_paperless_error(
        self,
    ) -> None:
        """D-11: `except PaperlessError` catches the timeout subclass too."""
        assert issubclass(PaperlessTimeoutError, PaperlessUnconfirmedError)
        assert issubclass(PaperlessTimeoutError, PaperlessError)

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=[{"status": "PENDING", "task_id": "t1"}])

        client = _poll_client(handler)
        try:
            with pytest.raises(PaperlessError):
                client.poll_task("t1", timeout=0.05)
        finally:
            client.close()

    def test_timeout_does_not_sleep_past_its_own_deadline(
        self, poll_clock: FakeClock
    ) -> None:
        """
        A 0.05 s budget costs 0.05 s, not the 0.5 s first sleep.

        The one wait is cut to what remains of the deadline, and the poll
        ends on the clock it was given, exactly at the deadline.
        """

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=[{"status": "PENDING", "task_id": "t1"}])

        client = _poll_client(handler, clock=poll_clock)
        try:
            with pytest.raises(PaperlessTimeoutError):
                client.poll_task("t1", timeout=0.05)
        finally:
            client.close()
        assert poll_clock.waits == [0.05]
        assert poll_clock.now() == 0.05

    def test_backoff_doubles_up_to_five_seconds(self, poll_clock: FakeClock) -> None:
        """
        The waits double from 0.5 s and never exceed 5 s.

        A task that finishes just after a wait is then found within five
        seconds, rather than up to thirty.  The last wait is cut to the time
        that remains, so the poll ends on its deadline.
        """

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=[{"status": "PENDING", "task_id": "t1"}])

        client = _poll_client(handler, clock=poll_clock)
        try:
            with pytest.raises(PaperlessTimeoutError):
                client.poll_task("t1", timeout=30)
        finally:
            client.close()
        assert poll_clock.waits == [0.5, 1.0, 2.0, 4.0, 5.0, 5.0, 5.0, 5.0, 2.5]
        assert poll_clock.now() == 30.0

    def test_the_deadline_runs_on_the_given_clock(self) -> None:
        """
        The deadline is measured on the client's clock, from where it stands.

        A clock that starts far from zero proves the poll reads it rather
        than a clock of its own: the waits and the end are the same.
        """
        clock = FakeClock(start=10_000.0)

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=[{"status": "PENDING", "task_id": "t1"}])

        client = _poll_client(handler, clock=clock)
        try:
            with pytest.raises(PaperlessTimeoutError):
                client.poll_task("t1", timeout=2)
        finally:
            client.close()
        assert clock.waits == [0.5, 1.0, 0.5]
        assert clock.now() == 10_002.0


def _other_task_v9(status: str) -> object:
    """Build a v9 answer that lists only another task."""
    return [{"task_id": "other", "status": status.upper()}]


def _other_task_v10(status: str) -> object:
    """Build a v10 answer that lists only another task."""
    task = {"task_id": "other", "status": status.lower()}
    return {"count": 1, "next": None, "previous": None, "results": [task]}


_OTHER_TASK_SHAPES = [
    pytest.param(_other_task_v9, _v9_payload, id="v9"),
    pytest.param(_other_task_v10, _v10_payload, id="v10"),
]


class TestPollTaskSelectsItsOwnTask:
    """The poll follows only the task whose id it was given."""

    @pytest.mark.parametrize(("build_other", "build_ours"), _OTHER_TASK_SHAPES)
    def test_another_tasks_success_is_not_returned(
        self,
        build_other: Callable[[str], object],
        build_ours: Callable[[str, str | None], object],
        poll_clock: FakeClock,
    ) -> None:
        """
        An answer holding only another task means ours is not visible yet.

        paperless-ngx filters by ``task_id`` on the server, so another task
        in the answer means something between saneless and paperless-ngx
        dropped the filter.  Its SUCCESS says nothing about our document.
        """

        def respond(call: int) -> httpx2.Response:
            if call <= 2:
                return httpx2.Response(200, json=build_other("SUCCESS"))
            return httpx2.Response(200, json=build_ours("SUCCESS", None))

        handler = _CountingHandler(respond)
        client = _poll_client(handler, clock=poll_clock)
        try:
            result = _filed(client.poll_task("t1", timeout=30))
        finally:
            client.close()
        assert result["task_id"] == "t1"
        assert handler.calls == 3
        assert poll_clock.waits == [0.5, 1.0]

    @pytest.mark.parametrize(
        "build_other",
        [
            pytest.param(_other_task_v9, id="v9"),
            pytest.param(_other_task_v10, id="v10"),
        ],
    )
    def test_only_other_tasks_end_at_the_deadline(
        self, build_other: Callable[[str], object]
    ) -> None:
        """A proxy that drops the filter for ever ends in a truthful timeout."""

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=build_other("FAILURE"))

        client = _poll_client(handler)
        try:
            with pytest.raises(PaperlessTimeoutError) as exc_info:
                client.poll_task("t1", timeout=10)
        finally:
            client.close()
        assert str(exc_info.value) == "Paperless task t1 did not finish within 10s"

    def test_our_task_is_found_behind_another(self) -> None:
        """Our entry is chosen by its id, wherever it sits in the list."""
        payload = [
            {"task_id": "other", "status": "FAILURE", "result": "not ours"},
            {"task_id": "t1", "status": "SUCCESS"},
        ]

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(200, json=payload)

        client = _poll_client(handler)
        try:
            result = _filed(client.poll_task("t1", timeout=10))
        finally:
            client.close()
        assert result == {"task_id": "t1", "status": "SUCCESS"}


_TRANSIENT_STATUSES = [
    pytest.param(502, id="5xx-502"),
    pytest.param(503, id="5xx-503"),
    pytest.param(504, id="5xx-504"),
    pytest.param(429, id="429"),
]


class TestPollTaskRidesOutTransientAnswers:
    """A proxy blip or a rate limit during the poll is waited out."""

    @pytest.mark.parametrize("status", _TRANSIENT_STATUSES)
    def test_a_transient_answer_then_success_is_filed(
        self, status: int, poll_clock: FakeClock
    ) -> None:
        """
        A 5xx or a 429 followed by SUCCESS ends as a filed document.

        The upload was already accepted, so a reverse proxy's brief 502
        while paperless-ngx restarts must not fail the job.
        """

        def respond(call: int) -> httpx2.Response:
            if call == 1:
                return httpx2.Response(status, text="try again")
            return httpx2.Response(200, json=[{"task_id": "t1", "status": "SUCCESS"}])

        handler = _CountingHandler(respond)
        client = _poll_client(handler, clock=poll_clock)
        try:
            result = _filed(client.poll_task("t1", timeout=30))
        finally:
            client.close()
        assert result["status"] == "SUCCESS"
        assert handler.calls == 2
        assert poll_clock.waits == [0.5]

    def test_a_502_for_ever_names_it_in_one_bounded_line(
        self, poll_clock: FakeClock
    ) -> None:
        """
        The timeout names the last transient answer on one line.

        A 5 KB proxy page quoting the token becomes one bounded line with the
        token struck, so the job store, the web status area and the
        terminal cannot be flooded, forged or handed the credential.
        """
        line = f"<p>Bad Gateway for Token {_MOCK_AUTH}</p>\n"
        page = (line * (5000 // len(line) + 1))[:5000]
        handler = _CountingHandler(_answering(httpx2.Response(502, text=page)))
        client = _poll_client(handler, clock=poll_clock)
        try:
            with pytest.raises(PaperlessTimeoutError) as exc_info:
                client.poll_task("t1", timeout=10)
        finally:
            client.close()
        message = str(exc_info.value)
        prefix = "Paperless task t1 did not finish within 10s; last error: "
        assert message.startswith(f"{prefix}502 Bad Gateway: <p>Bad Gateway for")
        body = message.removeprefix(f"{prefix}502 Bad Gateway: ")
        assert len(body) <= 201
        assert body.endswith("…")
        assert "\n" not in message
        assert "***" in message
        _assert_token_absent(_MOCK_AUTH, message)
        assert exc_info.value.__cause__ is None
        assert handler.calls == len(poll_clock.waits) + 1

    def test_a_token_in_the_reason_phrase_never_reaches_the_timeout(
        self, poll_clock: FakeClock
    ) -> None:
        """
        A gateway echoing the Authorization header in its status line is struck.

        The last transient answer's status line becomes the timeout's message,
        which is the job's error and the CLI line, so the reason phrase gets
        the same strike as the body beside it.
        """
        handler = _CountingHandler(
            _answering(
                httpx2.Response(
                    503,
                    text="unavailable",
                    extensions={"reason_phrase": f"Denied Token {_MOCK_AUTH}".encode()},
                )
            )
        )
        client = _poll_client(handler, clock=poll_clock)
        try:
            with pytest.raises(PaperlessTimeoutError) as exc_info:
                client.poll_task("t1", timeout=10)
        finally:
            client.close()
        message = str(exc_info.value)
        assert "503 Denied Token ***: unavailable" in message
        _assert_token_absent(_MOCK_AUTH, message)

    def test_an_answer_after_a_transient_one_clears_it(
        self, poll_clock: FakeClock
    ) -> None:
        """
        A 502 followed by answered polls is not the reason for the timeout.

        The task simply stayed pending, so the message must not blame the
        proxy.
        """

        def respond(call: int) -> httpx2.Response:
            if call == 1:
                return httpx2.Response(502, text="Bad Gateway")
            return httpx2.Response(200, json=[{"task_id": "t1", "status": "PENDING"}])

        client = _poll_client(_CountingHandler(respond), clock=poll_clock)
        try:
            with pytest.raises(PaperlessTimeoutError) as exc_info:
                client.poll_task("t1", timeout=10)
        finally:
            client.close()
        assert str(exc_info.value) == "Paperless task t1 did not finish within 10s"

    def test_a_transient_answer_is_logged_as_a_retry(
        self, poll_clock: FakeClock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Each transient answer leaves one warning naming it."""

        def respond(call: int) -> httpx2.Response:
            if call == 1:
                return httpx2.Response(503, text="down\nfor maintenance")
            return httpx2.Response(200, json=[{"task_id": "t1", "status": "SUCCESS"}])

        client = _poll_client(_CountingHandler(respond), clock=poll_clock)
        try:
            with caplog.at_level(logging.WARNING, logger="saneless.paperless"):
                client.poll_task("t1", timeout=10)
        finally:
            client.close()
        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert "503 Service Unavailable: down for maintenance" in warnings[0]


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


def _task_answer(task: dict[str, object]) -> Callable[[int], httpx2.Response]:
    """Build a script that answers every poll with a v9 list holding ``task``."""
    return _answering(httpx2.Response(200, json=[task]))


def _failed_poll_message(respond: Callable[[int], httpx2.Response]) -> str:
    """
    Poll ``t1`` against ``respond`` and return the error's text.

    Every failure once a task id is held is "received, not confirmed", so
    the error must be a ``PaperlessUnconfirmedError``.
    """
    client = _poll_client(_CountingHandler(respond))
    try:
        with pytest.raises(PaperlessUnconfirmedError) as exc_info:
            client.poll_task("t1", timeout=10)
    finally:
        client.close()
    return str(exc_info.value)


class TestPollTaskFailureTranslation:
    """EXC-01 / D-10 / D-11 / M-17: the read side of an accepted upload."""

    def test_poll_continues_through_transport_errors_to_success(
        self, poll_clock: FakeClock
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
        client = _poll_client(handler, clock=poll_clock)
        try:
            result = _filed(client.poll_task("t1", timeout=5))
        finally:
            client.close()
        assert result["status"] == "SUCCESS"
        assert handler.calls == 3
        assert poll_clock.waits == [0.5, 1.0]

    @pytest.mark.parametrize("failure", _POLL_TRANSPORT_CASES)
    def test_poll_deadline_after_transport_errors_names_the_last_error(
        self, failure: httpx2.RequestError, poll_clock: FakeClock
    ) -> None:
        """
        D-11 / OUTC-07 / T-28-38: transport errors still end at the deadline.

        Only the fake clock's waits move time on, so a poll that skipped the
        deadline check on a transport error would never end here.  The
        message names the task and ``describe`` of the last error (the class
        name for an empty one).
        """
        handler = _CountingHandler(_raising(failure))
        client = _poll_client(handler, clock=poll_clock)
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
        assert len(poll_clock.waits) == handler.calls - 1

    def test_poll_continues_through_a_decoding_error_to_success(
        self, poll_clock: FakeClock
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
        client = _poll_client(handler, clock=poll_clock)
        try:
            result = _filed(client.poll_task("t1", timeout=5))
        finally:
            client.close()
        assert result["status"] == "SUCCESS"
        assert handler.calls == 2
        assert poll_clock.waits == [0.5]

    def test_poll_deadline_without_transport_error_has_no_last_error(
        self, poll_clock: FakeClock
    ) -> None:
        """A task that simply stays PENDING keeps today's timeout message."""
        client = _poll_client(
            _CountingHandler(_task_answer({"task_id": "t1", "status": "PENDING"})),
            clock=poll_clock,
        )
        try:
            with pytest.raises(PaperlessTimeoutError) as exc_info:
                client.poll_task("t1", timeout=0.05)
        finally:
            client.close()
        assert str(exc_info.value) == "Paperless task t1 did not finish within 0.05s"
        assert exc_info.value.__cause__ is None
        assert poll_clock.waits

    def test_poll_deadline_after_a_recovered_blip_names_no_stale_error(
        self, poll_clock: FakeClock
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
        client = _poll_client(handler, clock=poll_clock)
        try:
            with pytest.raises(PaperlessTimeoutError) as exc_info:
                client.poll_task("t1", timeout=0.05)
        finally:
            client.close()
        assert str(exc_info.value) == "Paperless task t1 did not finish within 0.05s"
        assert exc_info.value.__cause__ is None
        assert handler.calls >= 2
        assert poll_clock.waits

    def test_poll_401_still_fails_at_once(self, poll_clock: FakeClock) -> None:
        """OUTC-07: a non-200 is not a transport blip and ends the poll at once."""
        handler = _CountingHandler(_answering(httpx2.Response(401, text="Invalid")))
        client = _poll_client(handler, clock=poll_clock)
        try:
            with pytest.raises(PaperlessUnconfirmedError, match="401 Unauthorized"):
                client.poll_task("t1", timeout=30)
        finally:
            client.close()
        assert handler.calls == 1
        assert poll_clock.waits == []

    def test_poll_non_json_200_is_unconfirmed(self, poll_clock: FakeClock) -> None:
        """EXC-01: a login page served with 200 is not a raw ValueError."""
        handler = _CountingHandler(_answering(httpx2.Response(200, text="<html>")))
        client = _poll_client(handler, clock=poll_clock)
        try:
            with pytest.raises(
                PaperlessUnconfirmedError,
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
        assert poll_clock.waits == []

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

    def test_whitespace_only_failure_text_still_says_something(self) -> None:
        """A failure text of only whitespace is as empty as none."""
        message = _failed_poll_message(
            _task_answer({"task_id": "t1", "status": "FAILURE", "result": " \n "})
        )
        assert message == (
            "Paperless task t1 ended FAILURE: Paperless reported a failure but "
            "supplied no message"
        )


def _poll_outcome(payload: object) -> TaskOutcome:
    """
    Poll ``t1`` against a server answering every poll with ``payload``.

    Args:
        payload: The JSON body of each 200 answer.

    Returns:
        What ``poll_task`` returned.

    """
    client = _poll_client(
        _CountingHandler(_answering(httpx2.Response(200, json=payload)))
    )
    try:
        return client.poll_task("t1", timeout=10)
    finally:
        client.close()


def _v10_duplicate(result_data: dict[str, object]) -> object:
    """Build a v10 ``/api/tasks/`` body whose task failed with ``result_data``."""
    task = {"task_id": "t1", "status": "failure", "result_data": result_data}
    return {"count": 1, "next": None, "previous": None, "results": [task]}


def _v9_failure(**fields: object) -> object:
    """Build a v9 ``/api/tasks/`` body whose task failed with ``fields``."""
    return [{"task_id": "t1", "status": "FAILURE", **fields}]


# The 2.x wording for an existing document that is in the trash, appended to
# the duplicate text.
_TRASH_NOTE = " Note: existing document is in the trash."


class TestPollTaskDuplicate:
    """A duplicate refusal names the document paperless-ngx already holds."""

    @pytest.mark.parametrize(
        ("build_payload", "status"),
        [
            pytest.param(_v9_payload, "SUCCESS", id="v9"),
            pytest.param(_v10_payload, "success", id="v10"),
        ],
    )
    def test_success_is_a_filed_task_not_a_duplicate(
        self, build_payload: Callable[[str, str | None], object], status: str
    ) -> None:
        """A SUCCESS task comes back whole, wrapped as filed."""
        outcome = _poll_outcome(build_payload("success", None))
        assert outcome == TaskFiled(task={"task_id": "t1", "status": status})

    @pytest.mark.parametrize("in_trash", [False, True])
    def test_v10_duplicate_is_read_from_result_data(self, *, in_trash: bool) -> None:
        """API v10 carries the id and the trash flag in ``result_data``."""
        outcome = _poll_outcome(
            _v10_duplicate({"duplicate_of": 42, "duplicate_in_trash": in_trash})
        )
        assert outcome == TaskDuplicate(document_id=42, in_trash=in_trash)

    def test_v10_duplicate_of_true_is_not_a_document_id(self) -> None:
        """
        A bool is an int to Python, but ``True`` is not document #1.

        Nor is it a duplicate: a duplicate keeps no copy of the scan, so only
        a ``duplicate_of`` that names a document is taken as one, and anything
        else ends unconfirmed, with the copy kept.
        """
        with pytest.raises(PaperlessUnconfirmedError):
            _poll_outcome(
                _v10_duplicate({"duplicate_of": True, "duplicate_in_trash": False})
            )

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(
                _v10_duplicate(
                    {
                        "duplicate_of": None,
                        "error_type": "ConsumerError",
                        "error_message": "OCR failed on page 2",
                    }
                ),
                id="v10-duplicate_of-null",
            ),
            pytest.param(
                _v10_payload(
                    "failure",
                    "Workflow 'Receipts' failed: the title 'Duplicate of invoice' "
                    "is already in use",
                ),
                id="v10-text-quotes-the-phrase",
            ),
            pytest.param(
                _v9_payload(
                    "failure",
                    "Error while consuming: page text reads 'duplicate of the "
                    "original, keep both'",
                ),
                id="v9-text-quotes-the-phrase",
            ),
        ],
    )
    def test_a_failure_that_only_mentions_a_duplicate_is_not_one(
        self, payload: object
    ) -> None:
        """
        A near miss ends unconfirmed, never as a delivered duplicate.

        Taken as a duplicate, the job would report success and keep no copy of
        a scan paperless-ngx never filed.
        """
        with pytest.raises(PaperlessUnconfirmedError):
            _poll_outcome(payload)

    @pytest.mark.parametrize(
        ("deleted_at", "in_trash"),
        [
            pytest.param(None, False, id="kept"),
            pytest.param("2026-01-01T00:00:00Z", True, id="trashed"),
        ],
    )
    def test_v9_on_3x_duplicate_is_read_from_duplicate_documents(
        self, deleted_at: str | None, *, in_trash: bool
    ) -> None:
        """paperless-ngx 3.x on v9 lists the duplicate; ``deleted_at`` is the trash."""
        outcome = _poll_outcome(
            _v9_failure(
                result="Not consuming: It is a duplicate of document #42",
                duplicate_documents=[
                    {"id": 42, "title": "Invoice", "deleted_at": deleted_at}
                ],
            )
        )
        assert outcome == TaskDuplicate(document_id=42, in_trash=in_trash)

    def test_v9_on_2x_duplicate_is_read_from_related_document(self) -> None:
        """paperless-ngx 2.x names the document as a string of digits."""
        outcome = _poll_outcome(
            _v9_failure(
                result="Not consuming scan.pdf: It is a Duplicate of Invoice (#42).",
                related_document="42",
            )
        )
        assert outcome == TaskDuplicate(document_id=42, in_trash=False)

    def test_v9_on_2x_duplicate_in_the_trash_is_read_from_the_text(self) -> None:
        """In the trash, 2.x sends no related document and appends a note."""
        outcome = _poll_outcome(
            _v9_failure(
                result="Not consuming scan.pdf: It is a duplicate of Invoice (#42)."
                + _TRASH_NOTE,
                related_document=None,
            )
        )
        assert outcome == TaskDuplicate(document_id=42, in_trash=True)

    def test_duplicate_title_holding_a_number_cannot_spoof_the_id(self) -> None:
        """The last ``(#N)`` is the document; one in the title is not."""
        outcome = _poll_outcome(
            _v9_failure(
                result="Not consuming x: It is a duplicate of Invoice #5 (#42)."
            )
        )
        assert outcome == TaskDuplicate(document_id=42, in_trash=False)

    def test_duplicate_title_with_a_bracketed_number_cannot_spoof_the_id(
        self,
    ) -> None:
        """A title that itself ends ``(#5)`` still yields the real, last id."""
        outcome = _poll_outcome(
            _v9_failure(
                result="Not consuming x: It is a duplicate of Bill (#5) copy (#42)."
            )
        )
        assert outcome == TaskDuplicate(document_id=42, in_trash=False)

    def test_duplicate_of_document_text_names_the_id(self) -> None:
        """The v9 wording, with no structured field, still names the document."""
        outcome = _poll_outcome(
            _v9_failure(result="Not consuming: It is a duplicate of document #42")
        )
        assert outcome == TaskDuplicate(document_id=42, in_trash=False)

    def test_duplicate_past_the_cut_still_names_the_id(self) -> None:
        """The id is read from the whole failure text, not the bounded one."""
        text = f"{'consumer output ' * 20}It is a duplicate of document #42"
        outcome = _poll_outcome(_v9_failure(result=text))
        assert outcome == TaskDuplicate(document_id=42, in_trash=False)

    def test_duplicate_with_no_id_anywhere_is_still_a_duplicate(self) -> None:
        """A duplicate the payload does not identify says "an existing document"."""
        outcome = _poll_outcome(
            _v9_failure(result="Not consuming: It is a duplicate of something")
        )
        assert outcome == TaskDuplicate(document_id=None, in_trash=False)

    @pytest.mark.parametrize(
        "related",
        [
            pytest.param(True, id="bool"),
            pytest.param(0, id="zero"),
            pytest.param(-3, id="negative"),
            pytest.param("0", id="zero-text"),
            pytest.param("4x", id="not-digits"),
            pytest.param("", id="empty"),
            pytest.param(" 42", id="padded"),
            pytest.param("٤٢", id="non-ascii-digits"),
            pytest.param("9" * 40, id="overlong"),
            pytest.param([42], id="list"),
        ],
    )
    def test_duplicate_refuses_an_id_that_is_not_a_positive_integer(
        self, related: object
    ) -> None:
        """Only a positive integer, or a string of ASCII digits, is an id."""
        outcome = _poll_outcome(
            _v9_failure(result="It is a duplicate of Invoice", related_document=related)
        )
        assert outcome == TaskDuplicate(document_id=None, in_trash=False)

    @pytest.mark.parametrize(
        "result_data",
        [
            pytest.param({"duplicate_of": "42"}, id="v10-digit-text"),
            pytest.param({"duplicate_of": 42}, id="v10-int"),
        ],
    )
    def test_duplicate_accepts_an_id_as_digits_or_an_int(
        self, result_data: dict[str, object]
    ) -> None:
        """A digit string and an int name the same document."""
        assert _poll_outcome(_v10_duplicate(result_data)) == TaskDuplicate(
            document_id=42, in_trash=False
        )

    def test_non_duplicate_failure_still_raises_unconfirmed(self) -> None:
        """A task that failed for another reason is not a duplicate result."""
        message = _failed_poll_message(
            _task_answer({"task_id": "t1", "status": "FAILURE", "result": "OCR died"})
        )
        assert message == "Paperless task t1 ended FAILURE: OCR died"


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
        upload_clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Nothing reaches the server, nothing is retried, copied or echoed."""
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()
        with loopback_paperless() as server:
            client = PaperlessClient(
                url=server.url,
                token=token,
                consume_dir=consume_dir,
                timing=PaperlessTiming(
                    clock=upload_clock.now, sleep=upload_clock.sleep
                ),
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
        assert upload_clock.waits == []
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
                task = _filed(client.poll_task(LOOPBACK_TASK_ID, timeout=5))
            finally:
                client.close()
        assert result == ApiDelivery(task_id=LOOPBACK_TASK_ID)
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
        self, sample_pdf: Path, tmp_path: Path, upload_clock: FakeClock
    ) -> None:
        """No retry, no consume-folder copy, and no configuration named."""
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()
        handler = _CountingHandler(
            _raising(httpx2.LocalProtocolError(_LENGTH_MISMATCH))
        )
        client = _upload_client(handler, consume_dir=consume_dir, clock=upload_clock)
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
        assert upload_clock.waits == []
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
            timing=PaperlessTiming(send_budget=0.0),
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
    def test_upload_before_send_budget_spent(
        self,
        build: Callable[[str], httpx2.ConnectError],
        token: str,
        sample_pdf: Path,
        upload_clock: FakeClock,
    ) -> None:
        """The could-not-connect error formats without the token."""
        handler = _CountingHandler(_raising(build(token)))
        client = PaperlessClient(
            url="http://paperless:8000",
            token=token,
            timing=PaperlessTiming(
                send_budget=1.0, clock=upload_clock.now, sleep=upload_clock.sleep
            ),
            transport=_make_transport(handler),
        )
        try:
            with pytest.raises(PaperlessError) as exc_info:
                client.upload_document(sample_pdf, title="Chain")
        finally:
            client.close()
        assert handler.calls == 2
        assert "could not connect for 1s" in str(exc_info.value)
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

    def test_a_clean_cause_is_still_chained(
        self, sample_pdf: Path, upload_clock: FakeClock
    ) -> None:
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
            timing=PaperlessTiming(
                send_budget=1.0, clock=upload_clock.now, sleep=upload_clock.sleep
            ),
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
    @pytest.mark.parametrize(
        "body",
        [42, "tags", True, [], [{"id": 1, "name": "one"}]],
        ids=["int", "string", "bool", "empty-list", "bare-list"],
    )
    def test_a_body_that_is_not_an_object_fails(
        self, method: str, noun: str, body: object
    ) -> None:
        """
        A body that is not a page object is a PaperlessError naming the collection.

        Every paperless-ngx release saneless supports paginates tags and
        correspondents, so a bare list is not a collection either: it is
        something other than paperless-ngx answering.
        """
        handler = _PagedHandler({1: httpx2.Response(200, json=body)})
        with pytest.raises(PaperlessError) as exc_info:
            _fetch(handler, method)
        assert str(exc_info.value) == (
            f"Could not fetch {noun} from Paperless at "
            f"http://{_CONFIGURED_HOST}:8000: the response was not an object"
        )
        assert len(handler.requests) == 1


# A connect bound shorter than the read bound: a black-holed host is given up
# on in two seconds, while a slow but answering one gets five.
_SPLIT_BUDGET = httpx2.Timeout(5.0, connect=2.0)
_SPLIT_BUDGET_SENT = {"connect": 2.0, "read": 5.0, "write": 5.0, "pool": 5.0}


class TestMetadataTimeout:
    """
    A metadata fetch can carry a shorter timeout than the client's 30 s.

    Checking the configured tags and correspondents before a scan starts
    should answer in seconds when paperless-ngx is down, so the caller may
    pass its own bound.  What needs proving is which budget each request
    carried, which ``request.extensions["timeout"]`` records as a value.
    """

    @staticmethod
    def _recorded_timeouts(
        method: str, timeout: float | None
    ) -> list[dict[str, float | None]]:
        """
        Fetch two pages of one collection and return each request's timeout.

        Args:
            method: ``get_tags`` or ``get_correspondents``.
            timeout: The bound to pass, or None to call with no argument.

        Returns:
            The ``timeout`` extension of every request, in order.

        """
        seen: list[dict[str, float | None]] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request.extensions["timeout"])
            page = int(request.url.params["page"])
            return httpx2.Response(
                200,
                json={
                    "count": 2,
                    "next": "more" if page == 1 else None,
                    "results": [{"id": page, "name": f"item {page}"}],
                },
            )

        client = _poll_client(handler)
        try:
            fetch = getattr(client, method)
            items = fetch() if timeout is None else fetch(timeout=timeout)
        finally:
            client.close()
        assert [item["id"] for item in items] == [1, 2]
        return seen

    @pytest.mark.parametrize("method", ["get_tags", "get_correspondents"])
    def test_a_timeout_is_sent_on_every_page(self, method: str) -> None:
        """``timeout=5.0`` bounds each page request to 5 s in every phase."""
        five = {"connect": 5.0, "read": 5.0, "write": 5.0, "pool": 5.0}
        assert self._recorded_timeouts(method, 5.0) == [five, five]

    @pytest.mark.parametrize("method", ["get_tags", "get_correspondents"])
    def test_no_timeout_keeps_the_client_default(self, method: str) -> None:
        """Called with no bound, every page keeps the client's 30 s."""
        thirty = {"connect": 30.0, "read": 30.0, "write": 30.0, "pool": 30.0}
        assert self._recorded_timeouts(method, None) == [thirty, thirty]

    @pytest.mark.parametrize("method", ["get_tags", "get_correspondents"])
    def test_the_timeout_is_keyword_only_and_optional(self, method: str) -> None:
        """The bound is a keyword, defaulting to None, the client default."""
        parameter = inspect.signature(getattr(PaperlessClient, method)).parameters[
            "timeout"
        ]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is None

    @staticmethod
    def _recording_client() -> tuple[PaperlessClient, list[dict[str, float | None]]]:
        """
        Build a client whose transport records each page request's timeout.

        Returns:
            The client, answering two pages, and the list it records into.

        """
        seen: list[dict[str, float | None]] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request.extensions["timeout"])
            page = int(request.url.params["page"])
            return httpx2.Response(
                200,
                json={
                    "count": 2,
                    "next": "more" if page == 1 else None,
                    "results": [{"id": page, "name": f"item {page}"}],
                },
            )

        return _poll_client(handler), seen

    def test_get_tags_passes_a_connect_and_read_budget(self) -> None:
        """An ``httpx2.Timeout`` reaches every page with its own connect bound."""
        client, seen = self._recording_client()
        try:
            items = client.get_tags(timeout=_SPLIT_BUDGET)
        finally:
            client.close()
        assert [item["id"] for item in items] == [1, 2]
        assert seen == [_SPLIT_BUDGET_SENT, _SPLIT_BUDGET_SENT]

    def test_get_correspondents_passes_a_connect_and_read_budget(self) -> None:
        """The correspondent list carries the same split budget on every page."""
        client, seen = self._recording_client()
        try:
            items = client.get_correspondents(timeout=_SPLIT_BUDGET)
        finally:
            client.close()
        assert [item["id"] for item in items] == [1, 2]
        assert seen == [_SPLIT_BUDGET_SENT, _SPLIT_BUDGET_SENT]


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

        Parametrised over `list(ConnectionStatus)` so a new member cannot
        be added without someone deciding what produces it.
        """
        produced = {
            _connection_result_for_status(code)
            for code in (200, 302, 401, 404, 406, 500)
        }
        produced.add(_connection_result_for_exception(httpx2.ConnectError))
        produced.add(_connection_result_for_exception(httpx2.UnsupportedProtocol))
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

    def test_connection_406_is_incompatible_version_on_the_wire(self) -> None:
        """A server refusing API 9 answers the documented sixth string."""
        assert _connection_result_for_status(406) == "incompatible_version"

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


_PROBE_BASE_URL = "http://p.example:8000"
_PROBE_TOKEN = "tok-PROBE-9c4e1f"


def _probe(
    handler: Callable[[httpx2.Request], httpx2.Response],
    *,
    url: str = _PROBE_BASE_URL,
    token: str = _PROBE_TOKEN,
) -> ConnectionProbe:
    """
    Run ``probe_connection`` against a mock transport.

    Args:
        handler: What the stub server answers with, or raises.
        url: The configured paperless.url.
        token: The configured paperless.token.

    Returns:
        The probe's result.

    """
    client = PaperlessClient(url=url, token=token, transport=_make_transport(handler))
    try:
        return client.probe_connection()
    finally:
        client.close()


def _redirecting(
    location: str, status_code: int = 301
) -> Callable[[httpx2.Request], httpx2.Response]:
    """
    Answer every request with a redirect to ``location``.

    Args:
        location: The Location header to send.
        status_code: The 3xx status to answer with.

    Returns:
        A mock transport handler.

    """

    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status_code, headers={"location": location})

    return handler


def _refused_by(
    exc_type: type[httpx2.TransportError],
) -> Callable[[httpx2.Request], httpx2.Response]:
    """
    Raise ``exc_type`` from every request, quoting the token as h11 would.

    Args:
        exc_type: The transport error to raise.

    Returns:
        A mock transport handler.

    """

    def handler(_request: httpx2.Request) -> httpx2.Response:
        msg = f"Illegal header value b'Token {_PROBE_TOKEN}'"
        raise exc_type(msg)

    return handler


class TestProbeConnection:
    """
    The connection probe tells a wrong address and a bad setting apart.

    A redirect is paperless.url pointing somewhere paperless-ngx does not
    answer, and a URL or token the HTTP library will not send is a setting
    to correct; neither is a server error or a network fault.  The probe
    keeps where a redirect pointed, sanitised, for the log and ``doctor``.
    """

    @pytest.mark.parametrize("status_code", [301, 302, 303, 307, 308])
    def test_a_redirect_is_redirected_with_its_target(self, status_code: int) -> None:
        """Every redirect status is REDIRECTED and keeps the target."""
        probe = _probe(_redirecting("https://other.example/api/", status_code))
        assert probe.status is ConnectionStatus.REDIRECTED
        assert probe.redirect_target == "https://other.example/api/"

    def test_a_redirect_with_no_location_has_no_target(self) -> None:
        """A 3xx naming nowhere is still a wrong address, with nothing to show."""

        def handler(_request: httpx2.Request) -> httpx2.Response:
            return httpx2.Response(302)

        probe = _probe(handler)
        assert probe == ConnectionProbe(ConnectionStatus.REDIRECTED)

    @pytest.mark.parametrize(
        "location",
        [
            pytest.param("https://p.example:8000/", id="to-the-root"),
            pytest.param(
                "https://p.example:8000/api/tags/?page_size=1", id="same-path"
            ),
        ],
    )
    def test_the_same_address_on_https_is_an_upgrade(self, location: str) -> None:
        """Only the scheme changed, so the fix is to use https://."""
        probe = _probe(_redirecting(location))
        assert probe.status is ConnectionStatus.REDIRECTED
        assert probe.https_upgrade is True
        assert probe.redirect_target == location

    def test_the_default_ports_count_as_the_same_port(self) -> None:
        """Plain HTTP on port 80 to HTTPS on 443 is the usual proxy upgrade."""
        probe = _probe(
            _redirecting("https://p.example/api/tags/?page_size=1"),
            url="http://p.example",
        )
        assert probe.https_upgrade is True

    @pytest.mark.parametrize(
        "location",
        [
            pytest.param("https://other.example/", id="other-host"),
            pytest.param("https://p.example:9443/", id="other-port"),
            pytest.param("https://p.example:8000/accounts/login/", id="other-path"),
            pytest.param("http://p.example:8000/paperless/", id="same-scheme"),
        ],
    )
    def test_anything_more_than_the_scheme_is_not_an_upgrade(
        self, location: str
    ) -> None:
        """Another host, port or path is a different address, not just https."""
        probe = _probe(_redirecting(location))
        assert probe.status is ConnectionStatus.REDIRECTED
        assert probe.https_upgrade is False

    def test_a_relative_target_is_resolved_against_the_request(self) -> None:
        """A path-only Location is shown as the address it names."""
        probe = _probe(_redirecting("/accounts/login/"))
        assert probe.redirect_target == "http://p.example:8000/accounts/login/"
        assert probe.https_upgrade is False

    def test_the_target_is_sanitised_and_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        No credential and no token survives into the target or the log.

        The Location is upstream text: a proxy can echo what it was sent,
        so the token is struck out and any userinfo is cut.
        """
        location = (
            f"https://scanner:{_URL_SECRET}@other.example/login"
            f"?next=/api/&token={_PROBE_TOKEN}"
        )
        with caplog.at_level(logging.WARNING, logger="saneless.paperless"):
            probe = _probe(_redirecting(location))
        target = probe.redirect_target
        assert target == "https://other.example/login?next=/api/&token=***"
        for text in caplog.messages:
            assert _URL_SECRET not in text
            assert "scanner:" not in text
            assert _PROBE_TOKEN not in text
        assert any(
            target in message and "check paperless.url" in message
            for message in caplog.messages
        )

    def test_a_long_target_is_bounded(self) -> None:
        """A Location as long as httpx2 accepts cannot flood the log or terminal."""
        probe = _probe(_redirecting("https://other.example/" + "a" * 60_000))
        assert probe.redirect_target is not None
        assert len(probe.redirect_target) < 1_000

    def test_a_target_with_control_characters_is_one_line(self) -> None:
        """
        A CR, LF or ESC in a Location can neither forge a line nor colour one.

        httpx2's client refuses such a Location before the probe sees it, so
        the response is built by hand: the sanitising must not rely on that.
        """
        response = httpx2.Response(
            301,
            headers={
                "location": (
                    f"https://scanner:{_URL_SECRET}@other.example/?t={_PROBE_TOKEN}"
                    "\r\nX-Forged: \x1b[31mred"
                )
            },
            request=httpx2.Request("GET", f"{_PROBE_BASE_URL}/api/tags/"),
        )
        shown, target = _redirect_target(response, _PROBE_TOKEN)
        assert target is None
        assert shown.startswith("https://other.example/?t=***")
        assert "X-Forged" in shown
        assert not has_control_characters(shown)
        assert _URL_SECRET not in shown
        assert _PROBE_TOKEN not in shown

    def test_a_location_httpx2_refuses_is_a_broken_answer(self) -> None:
        """
        A Location the URL parser rejects never arrives as a redirect.

        httpx2 raises ``RemoteProtocolError`` for it while reading the
        answer, so the probe reports what it reports for any broken answer.
        """
        probe = _probe(_redirecting("https://other.example/\x1b[31m"))
        assert probe == ConnectionProbe(ConnectionStatus.UNREACHABLE)

    def test_an_unsupported_scheme_is_misconfigured(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A URL with no usable scheme is a setting to correct, not unreachable."""
        with caplog.at_level(logging.WARNING, logger="saneless.paperless"):
            probe = _probe(_refused_by(httpx2.UnsupportedProtocol))
        assert probe == ConnectionProbe(ConnectionStatus.MISCONFIGURED)
        assert all(_PROBE_TOKEN not in message for message in caplog.messages)

    def test_a_scheme_less_url_is_misconfigured_on_the_real_transport(self) -> None:
        """No mock: httpx2 itself refuses the URL before anything is sent."""
        client = PaperlessClient(url="paperless:8000", token=_PROBE_TOKEN)
        try:
            probe = client.probe_connection()
        finally:
            client.close()
        assert probe.status is ConnectionStatus.MISCONFIGURED

    def test_an_unsendable_token_refused_locally_is_misconfigured(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A token outside the load rules is what the library refused to send."""
        with caplog.at_level(logging.WARNING, logger="saneless.paperless"):
            probe = _probe(_refused_by(httpx2.LocalProtocolError), token="")
        assert probe.status is ConnectionStatus.MISCONFIGURED
        assert all(_PROBE_TOKEN not in message for message in caplog.messages)

    def test_a_local_refusal_of_a_sendable_configuration_is_unreachable(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        Settings that pass the load rules are not blamed for a local refusal.

        The same rule the upload path applies: only a configuration that
        could be at fault is reported as one.
        """
        with caplog.at_level(logging.WARNING, logger="saneless.paperless"):
            probe = _probe(_refused_by(httpx2.LocalProtocolError))
        assert probe.status is ConnectionStatus.UNREACHABLE
        assert caplog.messages
        assert all(_PROBE_TOKEN not in message for message in caplog.messages)

    @pytest.mark.parametrize(
        "handler",
        [
            pytest.param(_redirecting("https://p.example:8000/"), id="redirect"),
            pytest.param(_refused_by(httpx2.UnsupportedProtocol), id="unsupported"),
            pytest.param(_refused_by(httpx2.ConnectError), id="connect-error"),
        ],
    )
    def test_test_connection_returns_the_probe_status(
        self, handler: Callable[[httpx2.Request], httpx2.Response]
    ) -> None:
        """The wire contract is the probe's status and nothing else."""
        client = PaperlessClient(
            url=_PROBE_BASE_URL, token=_PROBE_TOKEN, transport=_make_transport(handler)
        )
        try:
            assert client.test_connection() is client.probe_connection().status
        finally:
            client.close()


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
            timing=PaperlessTiming(send_budget=0.0),
        )
        result = client.upload_document(sample_pdf, title="Existing dir test")
        copied = list(consume_dir.iterdir())
        assert len(copied) == 1
        assert result == FolderDelivery(path=consume_dir / "test.pdf")
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
            timing=PaperlessTiming(send_budget=0.0),
        )
        result = client.upload_document(sample_pdf, title="Atomic test")
        client.close()

        entries = sorted(entry.name for entry in consume_dir.iterdir())
        assert entries == ["test.pdf"]
        _assert_no_staging_files(consume_dir)
        assert result == FolderDelivery(path=consume_dir / "test.pdf")
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
            timing=PaperlessTiming(send_budget=0.0),
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
            timing=PaperlessTiming(send_budget=0.0),
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

    def test_consume_dir_is_a_file_says_so(
        self, sample_pdf: Path, tmp_path: Path
    ) -> None:
        """
        A regular file where the directory should be is not a directory.

        Reported as "does not exist -- is the volume mounted?" it would send
        the operator after a mount that is fine.
        """
        consume_dir = tmp_path / "consume"
        consume_dir.write_bytes(b"occupied")

        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            consume_dir=consume_dir,
            transport=_make_transport(_always_refused),
            timing=PaperlessTiming(send_budget=0.0),
        )
        try:
            with pytest.raises(PaperlessError) as exc_info:
                client.upload_document(sample_pdf, title="Occupied")
        finally:
            client.close()

        message = str(exc_info.value)
        assert message == (
            f"consume directory {consume_dir} is not a directory; set "
            "paperless.consume_dir to the folder paperless-ngx consumes from"
        )
        assert consume_dir.read_bytes() == b"occupied"

    def test_consume_dir_that_cannot_be_examined_says_so(
        self, sample_pdf: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A permission refusal is not "does not exist": the folder may be there."""
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()
        # The concrete class, read from an instance: the annotations' Path is
        # imported for type checking only.
        path_class = type(consume_dir)
        real_stat = path_class.stat

        def _refused(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
            if path == consume_dir:
                raise PermissionError(errno.EACCES, "Permission denied", str(path))
            return real_stat(path, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(path_class, "stat", _refused)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            consume_dir=consume_dir,
            transport=_make_transport(_always_refused),
            timing=PaperlessTiming(send_budget=0.0),
        )
        try:
            with pytest.raises(PaperlessError) as exc_info:
                client.upload_document(sample_pdf, title="Refused")
        finally:
            client.close()

        message = str(exc_info.value)
        assert message.startswith(f"consume directory {consume_dir} cannot be read: ")
        assert "Permission denied" in message
        assert "does not exist" not in message

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
        assert result == FolderDelivery(path=dest)
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


_ACCEPT_V9 = "application/json; version=9"
_ACCEPT_V10 = "application/json; version=10"

_INCOMPATIBLE_TEXT = (
    "does not accept API version 9 or 10; saneless needs paperless-ngx 2.16 or later"
)


class _VersionedServer:
    """
    Answer like a paperless-ngx that allows API versions up to ``offered``.

    Every answer is in the shape of the version the request asked for, so a
    client that asked for 10 gets a paginated, lowercase task list, and every
    answer advertises ``offered`` in ``X-Api-Version`` when there is one.
    """

    def __init__(self, offered: int | None, *, task_status: str = "SUCCESS") -> None:
        """
        Start with no requests seen.

        Args:
            offered: The highest version to advertise, or None for no header.
            task_status: The status every task poll reports for ``t1``.

        """
        self.accepts: list[str | None] = []
        self._offered = offered
        self._task_status = task_status

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Record the Accept header and answer in the version it asked for."""
        accept = request.headers.get("accept")
        self.accepts.append(accept)
        shape = _v10_payload if accept == _ACCEPT_V10 else _v9_payload
        headers = {} if self._offered is None else {"X-Api-Version": str(self._offered)}
        path = request.url.path
        body: object
        if path == DOCUMENTS_PATH:
            body = "t1"
        elif path == TASKS_PATH:
            failed = self._task_status.upper() == "FAILURE"
            body = shape(self._task_status, "disk on fire" if failed else None)
        else:
            body = {
                "count": 1,
                "next": None,
                "previous": None,
                "results": [{"id": 1, "name": "one"}],
            }
        return httpx2.Response(200, json=body, headers=headers)


class _RolledBackServer:
    """
    Answer like a paperless-ngx 3.x that is then rolled back to 2.x.

    Before the rollback every answer announces 10.  After it, a request for
    10 is refused with a 406 that names no version, as paperless-ngx sends
    it, and a request for 9 is answered and announces 9.
    """

    def __init__(self) -> None:
        """Start on 3.x with no requests seen."""
        self.rolled_back = False
        self.requests: list[tuple[str | None, str]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        """Record the Accept header and path, and answer as the version allows."""
        accept = request.headers.get("accept")
        self.requests.append((accept, request.url.path))
        if self.rolled_back and accept == _ACCEPT_V10:
            return httpx2.Response(406, json={"detail": 'Invalid version in "Accept".'})
        headers = {"X-Api-Version": "9" if self.rolled_back else "10"}
        if request.url.path == DOCUMENTS_PATH:
            return httpx2.Response(200, json="t1", headers=headers)
        body = {
            "count": 1,
            "next": None,
            "previous": None,
            "results": [{"id": 1, "name": "one"}],
        }
        return httpx2.Response(200, json=body, headers=headers)


def _advertising(*values: str | None) -> tuple[list[str | None], PaperlessClient]:
    """
    Build a client whose n-th answer advertises the n-th value.

    Args:
        values: The ``X-Api-Version`` value of each answer in turn, or None
            for an answer without the header; the last repeats.

    Returns:
        The Accept header of every request sent, and the client.

    """
    accepts: list[str | None] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        accepts.append(request.headers.get("accept"))
        value = values[min(len(accepts), len(values)) - 1]
        # As UTF-8 bytes, the way a server can send them; httpx2 refuses a
        # non-ASCII str header value but decodes such bytes on the way in.
        headers: dict[bytes, bytes] = (
            {} if value is None else {b"X-Api-Version": value.encode()}
        )
        return httpx2.Response(200, json={"count": 0, "results": []}, headers=headers)

    return accepts, _poll_client(handler)


class TestApiVersionNegotiation:
    """The client speaks API 10 to a server that offers it, and 9 otherwise."""

    def test_api_version_starts_at_9(self) -> None:
        """Before any answer the client speaks the version every server allows."""
        _accepts, client = _advertising(None)
        try:
            assert client.api_version == 9
        finally:
            client.close()

    def test_api_version_10_offered_is_sent_on_the_next_request(self) -> None:
        """An authenticated answer naming 10 moves every later request to 10."""
        accepts, client = _advertising("10")
        try:
            client.test_connection()
            assert client.api_version == 10
            client.test_connection()
            client.test_connection()
        finally:
            client.close()
        assert accepts == [_ACCEPT_V9, _ACCEPT_V10, _ACCEPT_V10]

    def test_api_version_stays_at_9_without_the_header(self) -> None:
        """A server that names no version is spoken to in 9 throughout."""
        accepts, client = _advertising(None)
        try:
            client.test_connection()
            client.test_connection()
            assert client.api_version == 9
        finally:
            client.close()
        assert accepts == [_ACCEPT_V9, _ACCEPT_V9]

    def test_api_version_9_offered_stays_at_9(self) -> None:
        """A paperless-ngx 2.x names 9, which is what the client already sends."""
        accepts, client = _advertising("9")
        try:
            client.test_connection()
            client.test_connection()
            assert client.api_version == 9
        finally:
            client.close()
        assert accepts == [_ACCEPT_V9, _ACCEPT_V9]

    @pytest.mark.parametrize("offered", ["11", "999"])
    def test_api_version_above_10_offered_is_spoken_as_10(self, offered: str) -> None:
        """A newer server still allows 10, the highest this client knows."""
        accepts, client = _advertising(offered)
        try:
            client.test_connection()
            client.test_connection()
            assert client.api_version == 10
        finally:
            client.close()
        assert accepts == [_ACCEPT_V9, _ACCEPT_V10]

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("999999", id="too-long"),
            pytest.param("1000", id="four-digits"),
            pytest.param("10\r\n", id="crlf"),
            pytest.param("1 0", id="inner-space"),
            pytest.param("ten", id="word"),
            pytest.param("", id="empty"),
            pytest.param("8", id="below-9"),
            pytest.param("-10", id="negative"),
            pytest.param("10.0", id="decimal"),
            pytest.param("\u0661\u0660", id="non-ascii-digits"),
        ],
    )
    def test_api_version_header_that_is_not_a_version_is_ignored(
        self, value: str
    ) -> None:
        """Only one to three ASCII digits naming at least 9 are ever read."""
        accepts, client = _advertising(value)
        try:
            client.test_connection()
            client.test_connection()
            assert client.api_version == 9
        finally:
            client.close()
        assert accepts == [_ACCEPT_V9, _ACCEPT_V9]

    @pytest.mark.parametrize(
        "later",
        [
            pytest.param(None, id="no-header"),
            pytest.param("8", id="below-9"),
            pytest.param("garbage", id="garbage"),
        ],
    )
    def test_api_version_10_is_kept_when_a_later_answer_names_none(
        self, later: str | None
    ) -> None:
        """An answer without a usable header does not forget what was learned."""
        accepts, client = _advertising("10", later)
        try:
            for _ in range(3):
                client.test_connection()
            assert client.api_version == 10
        finally:
            client.close()
        assert accepts == [_ACCEPT_V9, _ACCEPT_V10, _ACCEPT_V10]

    def test_api_version_follows_a_server_that_now_names_9(self) -> None:
        """
        The latest usable header counts, so a downgraded server gets 9 again.

        Sending 10 to a server that allows only 9 would be refused with 406.
        """
        accepts, client = _advertising("10", "9")
        try:
            for _ in range(3):
                client.test_connection()
            assert client.api_version == 9
        finally:
            client.close()
        assert accepts == [_ACCEPT_V9, _ACCEPT_V10, _ACCEPT_V9]

    def test_api_version_is_learned_from_an_error_answer(self) -> None:
        """
        A refused request still teaches the version its answer names.

        paperless-ngx names it on any authenticated answer, a 4xx included,
        and the next request should not repeat the old version.
        """
        calls = {"n": 0}

        def handler(_request: httpx2.Request) -> httpx2.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx2.Response(
                    400, json={"detail": "bad page"}, headers={"X-Api-Version": "10"}
                )
            return httpx2.Response(200, json={"count": 0, "results": []})

        client = _poll_client(handler)
        try:
            with pytest.raises(PaperlessError):
                client.get_tags()
            assert client.api_version == 10
        finally:
            client.close()

    @pytest.mark.parametrize(("method", "noun"), _METADATA_METHODS)
    def test_metadata_406_is_incompatible(self, method: str, noun: str) -> None:
        """A 406 on a metadata fetch names the versions and the release needed."""
        handler = _CountingHandler(
            _answering(
                httpx2.Response(406, json={"detail": 'Invalid version in "Accept".'})
            )
        )
        client = _metadata_client(handler)
        try:
            with pytest.raises(PaperlessIncompatibleError) as exc_info:
                getattr(client, method)()
        finally:
            client.close()
        assert str(exc_info.value) == (
            f"Paperless at http://paperless.test:8000 {_INCOMPATIBLE_TEXT}"
        )
        assert noun not in str(exc_info.value)
        assert "Invalid version" not in str(exc_info.value)
        assert exc_info.value.__cause__ is None
        assert handler.calls == 1

    def test_a_406_to_10_after_a_rollback_is_asked_again_with_9(
        self, sample_pdf: Path, upload_clock: FakeClock
    ) -> None:
        """
        A server that announced 10 and was then rolled back to 2.x still works.

        The rolled-back server refuses every request for 10 with a 406 that
        names no version.  Without forgetting the 10 the client would ask for
        it until the process restarted and every upload would fail as
        incompatible; instead each refused request is asked once more with 9,
        and every later one starts at 9.
        """
        server = _RolledBackServer()
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            transport=_make_transport(server),
            timing=PaperlessTiming(clock=upload_clock.now, sleep=upload_clock.sleep),
        )
        try:
            client.test_connection()
            assert client.api_version == 10
            server.rolled_back = True
            delivery = client.upload_document(sample_pdf, title="Rolled back")
            assert client.api_version == 9
            client.test_connection()
        finally:
            client.close()
        assert delivery == ApiDelivery(task_id="t1")
        assert [
            (accept, path == DOCUMENTS_PATH) for accept, path in server.requests
        ] == [
            (_ACCEPT_V9, False),
            (_ACCEPT_V10, True),
            (_ACCEPT_V9, True),
            (_ACCEPT_V9, False),
        ]

    @pytest.mark.parametrize(("method", "_noun"), _METADATA_METHODS)
    def test_a_metadata_406_to_10_after_a_rollback_is_asked_again_with_9(
        self, method: str, _noun: str
    ) -> None:
        """The metadata fetch recovers the same way, one page at a time."""
        server = _RolledBackServer()
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            transport=_make_transport(server),
        )
        try:
            client.test_connection()
            server.rolled_back = True
            items = getattr(client, method)()
        finally:
            client.close()
        assert items == [{"id": 1, "name": "one"}]
        assert [accept for accept, _path in server.requests] == [
            _ACCEPT_V9,
            _ACCEPT_V10,
            _ACCEPT_V9,
        ]

    def test_a_406_to_9_is_not_asked_again(self, sample_pdf: Path) -> None:
        """A server that refuses 9 as well is incompatible after one request."""
        handler = _CountingHandler(
            _answering(httpx2.Response(406, json={"detail": "Invalid version"}))
        )
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            transport=_make_transport(handler),
        )
        try:
            with pytest.raises(PaperlessIncompatibleError):
                client.upload_document(sample_pdf, title="Too old")
        finally:
            client.close()
        assert handler.calls == 1

    @pytest.mark.parametrize(
        ("offered", "spoken"),
        [
            pytest.param(None, _ACCEPT_V9, id="api_version-none"),
            pytest.param(9, _ACCEPT_V9, id="api_version-9"),
            pytest.param(10, _ACCEPT_V10, id="api_version-10"),
        ],
    )
    def test_whole_run_speaks_the_offered_api_version(
        self,
        offered: int | None,
        spoken: str,
        sample_pdf: Path,
        upload_clock: FakeClock,
    ) -> None:
        """Both metadata fetches, the upload and the poll work in either shape."""
        server = _VersionedServer(offered)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            transport=_make_transport(server),
            timing=PaperlessTiming(clock=upload_clock.now, sleep=upload_clock.sleep),
        )
        try:
            tags = client.get_tags()
            correspondents = client.get_correspondents()
            delivery = client.upload_document(sample_pdf, title="Versioned")
            task = _filed(client.poll_task("t1", timeout=10))
        finally:
            client.close()
        assert tags == [{"id": 1, "name": "one"}]
        assert correspondents == [{"id": 1, "name": "one"}]
        assert delivery == ApiDelivery(task_id="t1")
        assert str(task["status"]).upper() == "SUCCESS"
        assert server.accepts == [_ACCEPT_V9, spoken, spoken, spoken]

    @pytest.mark.parametrize(
        "offered",
        [
            pytest.param(9, id="api_version-9"),
            pytest.param(10, id="api_version-10"),
        ],
    )
    def test_failed_task_is_reported_in_either_api_version(self, offered: int) -> None:
        """A v10 lowercase ``failure`` fails the poll just as v9's FAILURE does."""
        server = _VersionedServer(offered, task_status="FAILURE")
        client = _poll_client(server)
        try:
            client.test_connection()
            with pytest.raises(PaperlessError, match="disk on fire"):
                client.poll_task("t1", timeout=10)
        finally:
            client.close()
        assert server.accepts[-1] == (_ACCEPT_V10 if offered == 10 else _ACCEPT_V9)

    @pytest.mark.parametrize(
        ("offered", "spoken"),
        [
            pytest.param(None, _ACCEPT_V9, id="api_version-none"),
            pytest.param(10, _ACCEPT_V10, id="api_version-10"),
        ],
    )
    def test_duplicate_is_detected_in_either_api_version(
        self,
        offered: int | None,
        spoken: str,
        sample_pdf: Path,
        upload_clock: FakeClock,
    ) -> None:
        """A duplicate is named whether it comes as v9 text or v10 result_data."""
        recorder = RecordingPaperless(api_version=offered, duplicate_of=42)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            transport=_make_transport(recorder),
            timing=PaperlessTiming(clock=upload_clock.now, sleep=upload_clock.sleep),
        )
        try:
            delivery = client.upload_document(sample_pdf, title="Twice")
            assert isinstance(delivery, ApiDelivery)
            outcome = client.poll_task(delivery.task_id, timeout=10)
        finally:
            client.close()
        assert outcome == TaskDuplicate(document_id=42, in_trash=False)
        (poll,) = recorder.polls()
        assert poll.headers["accept"] == spoken


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
            "http://paperless.invalid",
            "token",
            transport=transport,
        )
        try:
            with pytest.raises(PaperlessUncertainSendError, match="not a task ID"):
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
        timing=PaperlessTiming(send_budget=0.0),
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

        assert result == FolderDelivery(path=consume_dir / "test.pdf")
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
        assert result == FolderDelivery(path=consume_dir / "test.pdf")
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

        assert result == ApiDelivery(task_id=_HOSTILE_TASK_ID)
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
        poll_clock: FakeClock,
        caplog: pytest.LogCaptureFixture,
        respond: Callable[[int], httpx2.Response],
    ) -> None:
        """A poll timeout names a bounded id, and so does every log line."""
        sent: list[str] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            sent.append(request.url.params["task_id"])
            return respond(len(sent))

        client = _poll_client(handler, clock=poll_clock)
        try:
            with (
                caplog.at_level(logging.INFO, logger="saneless.paperless"),
                pytest.raises(PaperlessTimeoutError) as exc_info,
            ):
                client.poll_task(_HOSTILE_TASK_ID, timeout=0.05)
        finally:
            client.close()

        assert poll_clock.waits
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
