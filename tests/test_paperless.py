"""Tests for paperless-ngx REST client."""

from __future__ import annotations

import inspect
import logging
import re
import shutil
import time
from typing import TYPE_CHECKING

import httpx2
import pytest

from saneless.exceptions import PaperlessError, PaperlessTimeoutError, describe
from saneless.paperless import (
    PaperlessClient,
    UploadResult,
    _render_error_body,
    _without_userinfo,
)
from saneless.vocabulary import ConnectionStatus

if TYPE_CHECKING:
    from collections.abc import Callable
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
        """Upload includes repeated tag form fields."""
        captured_data: dict[str, str] = {}

        def handler(_request: httpx2.Request) -> httpx2.Response:
            content = _request.content.decode("utf-8", errors="replace")
            captured_data["content"] = content
            return httpx2.Response(200, json="task-id")

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            transport=transport,
        )
        client.upload_document(sample_pdf, title="Test", tags=[1, 2, 3])
        # Tags should appear as repeated form fields
        content = captured_data["content"]
        assert content.count("tags") >= 3
        client.close()

    def test_upload_with_correspondent(self, sample_pdf: Path) -> None:
        """Upload includes correspondent field."""
        captured_data: dict[str, str] = {}

        def handler(_request: httpx2.Request) -> httpx2.Response:
            content = _request.content.decode("utf-8", errors="replace")
            captured_data["content"] = content
            return httpx2.Response(200, json="task-id")

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            transport=transport,
        )
        client.upload_document(sample_pdf, title="Test", correspondent=5)
        assert "correspondent" in captured_data["content"]
        assert "5" in captured_data["content"]
        client.close()

    def test_upload_with_created(self, sample_pdf: Path) -> None:
        """Upload includes created date field."""
        captured_data: dict[str, str] = {}

        def handler(_request: httpx2.Request) -> httpx2.Response:
            content = _request.content.decode("utf-8", errors="replace")
            captured_data["content"] = content
            return httpx2.Response(200, json="task-id")

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            transport=transport,
        )
        client.upload_document(sample_pdf, title="Test", created="2026-03-20")
        content = captured_data["content"]
        assert "2026-03-20" in content
        # Ensure no ISO 8601 time component (T...) after the date value
        date_segment = content.split("2026-03-20")[1].split("\r\n")[0]
        assert "T" not in date_segment
        client.close()

    def test_form_fields_sent_as_data_not_files(self, sample_pdf: Path) -> None:
        """Form fields (title, created) use data= parameter, PDF uses files= parameter."""
        captured_data: dict[str, bytes] = {}

        def handler(_request: httpx2.Request) -> httpx2.Response:
            captured_data["body"] = _request.content
            return httpx2.Response(200, json="task-id")

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            transport=transport,
        )
        client.upload_document(sample_pdf, title="Test Doc", created="2026-03-22")
        body = captured_data["body"].decode("utf-8", errors="replace")
        # Document field has filename attribute (file upload via files=)
        assert 'name="document"; filename=' in body
        # Title field has NO filename attribute (form data via data=)
        assert 'name="title"' in body
        assert 'name="title"; filename=' not in body
        # Created field has NO filename attribute (form data via data=)
        assert 'name="created"' in body
        assert 'name="created"; filename=' not in body
        client.close()

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
    """D-08: a malformed paperless.url is a PaperlessError at construction."""

    def test_invalid_url_raises_a_paperless_error(self) -> None:
        """
        ``http://host:abc`` names the URL and httpx2's own text.

        The token must never reach the message (T-28-20, Pitfall 1): only the
        configured URL and the httpx2 text are interpolated.
        """
        with pytest.raises(
            PaperlessError,
            match=re.escape(
                "Paperless URL http://host:abc is not valid: Invalid port: 'abc'"
            ),
        ) as exc_info:
            PaperlessClient("http://host:abc", "tok-SECRET-5d1")
        assert isinstance(exc_info.value.__cause__, httpx2.InvalidURL)
        assert "tok-SECRET-5d1" not in str(exc_info.value)


_URL_SECRET = "pr0xy-S3CRET"


class TestUrlCredentialsNeverShown:
    """
    WR-08 / D-08: a ``user:password@`` in paperless.url never reaches a message.

    Messages land in ``job.error`` (rendered in the web status area), on the
    terminal and in the log, so each surface is checked for the password.
    """

    def test_unreachable_message_names_the_url_without_its_password(
        self, sample_pdf: Path, sleeps: list[float]
    ) -> None:
        """The exhausted-retry message shows the host, not the credentials."""
        handler = _CountingHandler(_raising(httpx2.ConnectError("refused")))
        client = PaperlessClient(
            url=f"https://scanner:{_URL_SECRET}@paperless.example",
            token=_MOCK_AUTH,
            transport=_make_transport(handler),
            max_retries=3,
        )
        try:
            with pytest.raises(PaperlessError) as exc_info:
                client.upload_document(sample_pdf, title="Behind a proxy")
        finally:
            client.close()
        assert str(exc_info.value) == (
            "Upload to Paperless at https://paperless.example failed after "
            "3 attempts: refused"
        )
        assert len(sleeps) == 2

    def test_credentials_are_still_sent_as_basic_auth(self) -> None:
        """Stripping the userinfo from the URL does not change the request."""
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return httpx2.Response(200, json={"results": []})

        client = PaperlessClient(
            url=f"https://scanner:{_URL_SECRET}@paperless.example/sub/",
            token=_MOCK_AUTH,
            transport=_make_transport(handler),
        )
        try:
            client.get_tags()
        finally:
            client.close()
        expected = httpx2.BasicAuth("scanner", _URL_SECRET)
        probe = next(expected.auth_flow(httpx2.Request("GET", "https://x/")))
        assert seen[0].headers["authorization"] == probe.headers["authorization"]
        assert str(seen[0].url).startswith("https://paperless.example/sub/api/tags/")
        assert _URL_SECRET not in str(seen[0].url)

    def test_no_log_record_carries_the_password(
        self, sample_pdf: Path, sleeps: list[float], caplog: pytest.LogCaptureFixture
    ) -> None:
        """Neither saneless's log lines nor httpx2's request log name it."""
        caplog.set_level(logging.DEBUG)

        def respond(call: int) -> httpx2.Response:
            if call == 1:
                return httpx2.Response(503, text="down")
            return httpx2.Response(200, json="task-id")

        client = PaperlessClient(
            url=f"https://scanner:{_URL_SECRET}@paperless.example",
            token=_MOCK_AUTH,
            transport=_make_transport(_CountingHandler(respond)),
        )
        try:
            client.upload_document(sample_pdf, title="Logged")
        finally:
            client.close()
        assert caplog.records
        assert all(_URL_SECRET not in record.getMessage() for record in caplog.records)
        assert sleeps == [1]

    def test_invalid_url_message_strips_the_password(self) -> None:
        """A URL httpx2 rejects is shown without its userinfo too."""
        with pytest.raises(PaperlessError) as exc_info:
            PaperlessClient(f"http://scanner:{_URL_SECRET}@host:abc", _MOCK_AUTH)
        assert str(exc_info.value) == (
            "Paperless URL http://host:abc is not valid: Invalid port: 'abc'"
        )

    def test_scheme_less_url_message_strips_the_password(
        self, sample_pdf: Path, sleeps: list[float], caplog: pytest.LogCaptureFixture
    ) -> None:
        """A URL with no scheme, which httpx2 cannot parse as userinfo, is cut too."""
        caplog.set_level(logging.WARNING, logger="saneless.paperless")
        client = PaperlessClient(
            url=f"scanner:{_URL_SECRET}@paperless:8000", token=_MOCK_AUTH
        )
        try:
            with pytest.raises(PaperlessError) as exc_info:
                client.upload_document(sample_pdf, title="No scheme")
        finally:
            client.close()
        assert str(exc_info.value).startswith(
            "Could not reach Paperless at paperless:8000: "
        )
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
        T-28-22: a scheme-less URL is never retried with backoff.

        UnsupportedProtocol is a TransportError, so it must be caught before
        the retrying clause.
        """
        failure = httpx2.UnsupportedProtocol(_UNSUPPORTED_PROTOCOL_TEXT)
        handler = _CountingHandler(_raising(failure))
        client = _upload_client(handler)
        try:
            with pytest.raises(
                PaperlessError, match=re.escape(_UNSUPPORTED_PROTOCOL_TEXT)
            ) as exc_info:
                client.upload_document(sample_pdf, title="No scheme")
        finally:
            client.close()
        assert handler.calls == 1
        assert sleeps == []
        assert exc_info.value.__cause__ is failure

    def test_unsupported_protocol_url_still_takes_the_fallback(
        self, sample_pdf: Path, tmp_path: Path, sleeps: list[float]
    ) -> None:
        """D-10: no retry is not no fallback -- the scan is never lost."""
        consume_dir = tmp_path / "consume"
        failure = httpx2.UnsupportedProtocol(_UNSUPPORTED_PROTOCOL_TEXT)
        handler = _CountingHandler(_raising(failure))
        client = _upload_client(handler, consume_dir=consume_dir)
        try:
            result = client.upload_document(sample_pdf, title="No scheme")
        finally:
            client.close()
        assert handler.calls == 1
        assert sleeps == []
        assert result.delivered_to_api is False
        assert result.consume_dir_path == consume_dir / "test.pdf"

    def test_unsupported_protocol_fallback_logs_the_cause(
        self,
        sample_pdf: Path,
        tmp_path: Path,
        sleeps: list[float],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        WR-04: a fallback caused by an unusable URL says so in the log.

        No attempt is logged by the backoff for it, so without this line the
        job ends "Saved to folder" and nothing anywhere names the URL.
        """
        caplog.set_level(logging.WARNING, logger="saneless.paperless")
        consume_dir = tmp_path / "consume"
        failure = httpx2.UnsupportedProtocol(_UNSUPPORTED_PROTOCOL_TEXT)
        handler = _CountingHandler(_raising(failure))
        client = _upload_client(handler, consume_dir=consume_dir)
        try:
            client.upload_document(sample_pdf, title="No scheme")
        finally:
            client.close()
        assert sleeps == []
        assert (
            "Paperless URL http://paperless:8000 cannot be used "
            f"({_UNSUPPORTED_PROTOCOL_TEXT}); not retrying"
        ) in caplog.messages

    def test_empty_url_fails_fast_through_the_real_transport(
        self, sample_pdf: Path, sleeps: list[float]
    ) -> None:
        """An empty paperless.url raises httpx2's own text on the first request."""
        client = PaperlessClient(url="", token=_MOCK_AUTH, max_retries=3)
        try:
            with pytest.raises(PaperlessError, match="protocol") as exc_info:
                client.upload_document(sample_pdf, title="Empty URL")
        finally:
            client.close()
        assert isinstance(exc_info.value.__cause__, httpx2.UnsupportedProtocol)
        assert sleeps == []

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
        assert _render_error_body(response) == expected

    def test_html_body_is_one_bounded_line(self) -> None:
        """
        T-23-16 / M-17: a 5000-character HTML page cannot flood job.error.

        Whitespace is collapsed so no newline survives, and the text is cut
        to 200 characters plus an ellipsis.
        """
        line = "<p>502 Bad Gateway from the reverse proxy</p>\n"
        html = (line * (5000 // len(line) + 1))[:5000]
        response = httpx2.Response(502, text=html)
        result = _render_error_body(response)
        assert "\n" not in result
        assert len(result) <= 201
        assert result.endswith("…")

    def test_empty_body_says_so(self) -> None:
        """An empty body renders as an explicit marker, never an empty string."""
        response = httpx2.Response(500, text="")
        assert _render_error_body(response) == "(empty response body)"

    def test_multiline_json_detail_body_is_collapsed(self) -> None:
        """No path yields a newline, including JSON-derived text (T-28-24)."""
        response = httpx2.Response(400, json={"detail": "line one\nline two"})
        assert _render_error_body(response) == "line one line two"

    def test_full_body_is_logged_at_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The whole body stays diagnosable, but only at DEBUG (T-28-21)."""
        body = "<html>" + ("x" * 900) + "</html>"
        response = httpx2.Response(502, text=body)
        with caplog.at_level(logging.DEBUG, logger="saneless.paperless"):
            _render_error_body(response)
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

    def test_consume_dir_created_if_not_exists(
        self, sample_pdf: Path, tmp_path: Path
    ) -> None:
        """Fallback creates consume_dir if it does not exist before copying."""
        consume_dir = tmp_path / "nonexistent" / "consume"
        assert not consume_dir.exists()

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
        result = client.upload_document(sample_pdf, title="Auto-create test")
        assert result.delivered_to_api is False
        assert consume_dir.exists()
        copied = list(consume_dir.iterdir())
        assert len(copied) == 1
        assert copied[0].name == "test.pdf"
        assert result.consume_dir_path == consume_dir / "test.pdf"
        client.close()

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

    def test_an_uncreatable_consume_dir_raises_a_paperless_error(
        self, sample_pdf: Path, tmp_path: Path
    ) -> None:
        """
        EXC-01: a consume directory that cannot be created is a PaperlessError.

        A regular file sits where the directory should be, so ``mkdir`` fails
        whatever user runs the tests (a permission-based setup would pass
        under root).
        """
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("occupied")
        consume_dir = blocker / "consume"

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
                match="Could not copy the PDF to the consume directory",
            ) as exc_info:
                client.upload_document(sample_pdf, title="Blocked")
        finally:
            client.close()

        assert isinstance(exc_info.value.__cause__, OSError)

    def test_consume_dir_logs_warning_on_create(
        self, sample_pdf: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A warning is logged when creating the consume directory."""
        consume_dir = tmp_path / "warn-consume"
        assert not consume_dir.exists()

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
        with caplog.at_level(logging.WARNING, logger="saneless.paperless"):
            client.upload_document(sample_pdf, title="Warning test")
        assert any("Created consume directory" in msg for msg in caplog.messages)
        client.close()


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
    """UploadResult cannot represent a destination it did not reach (CTR-03)."""

    def test_api_delivery_carries_a_task_uuid(self) -> None:
        """A result claiming API delivery must carry the task id (CTR-03)."""
        result = UploadResult(delivered_to_api=True, task_uuid="abc-123")
        assert result.task_uuid == "abc-123"
        assert result.consume_dir_path is None

    def test_consume_dir_delivery_carries_a_path(self, tmp_path: Path) -> None:
        """A result claiming consume-dir delivery must carry the path (CTR-03)."""
        dest = tmp_path / "consume" / "doc.pdf"
        result = UploadResult(delivered_to_api=False, consume_dir_path=dest)
        assert result.consume_dir_path == dest
        assert result.task_uuid is None

    def test_api_delivery_without_task_uuid_is_rejected(self) -> None:
        """
        delivered_to_api=True with no task id is unconstructible (CTR-03).

        This is the state the old "fallback" sentinel could not express and the
        typed result must not silently allow: run_pipeline reads the presence of
        a task id to decide SUCCESS vs FALLBACK, so such a result would report a
        document that reached paperless-ngx as a consume-directory fallback.
        """
        with pytest.raises(ValueError, match="requires a task_uuid"):
            UploadResult(delivered_to_api=True)

    def test_consume_dir_delivery_without_path_is_rejected(self) -> None:
        """delivered_to_api=False with no path is unconstructible (CTR-03)."""
        with pytest.raises(ValueError, match="requires a consume_dir_path"):
            UploadResult(delivered_to_api=False)

    def test_the_two_destinations_are_mutually_exclusive(self, tmp_path: Path) -> None:
        """A result cannot claim both destinations at once (CTR-03)."""
        dest = tmp_path / "consume" / "doc.pdf"
        with pytest.raises(ValueError, match="cannot carry a consume_dir_path"):
            UploadResult(
                delivered_to_api=True,
                task_uuid="abc-123",
                consume_dir_path=dest,
            )
        with pytest.raises(ValueError, match="cannot carry a task_uuid"):
            UploadResult(
                delivered_to_api=False,
                task_uuid="abc-123",
                consume_dir_path=dest,
            )

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
