"""Tests for paperless-ngx REST client."""

from __future__ import annotations

import logging
import shutil
import time
from typing import TYPE_CHECKING

import httpx
import pytest

from saneless.exceptions import PaperlessError, PaperlessTimeoutError
from saneless.paperless import PaperlessClient, UploadResult
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
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.MockTransport:
    """Create an httpx.MockTransport from a handler function."""
    return httpx.MockTransport(handler)


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
    handler: Callable[[httpx.Request], httpx.Response],
) -> PaperlessClient:
    """Build a client wired to the given mock handler."""
    return PaperlessClient(
        url="http://paperless:8000",
        token=_MOCK_AUTH,
        _transport=_make_transport(handler),
    )


def _connection_result_for_status(status_code: int) -> ConnectionStatus:
    """Run test_connection against a server that answers with one status code."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, text="")

    client = _poll_client(handler)
    try:
        return client.test_connection()
    finally:
        client.close()


def _connection_result_for_exception(
    exc_type: type[httpx.TransportError],
) -> ConnectionStatus:
    """Run test_connection against a transport that raises."""

    def handler(_request: httpx.Request) -> httpx.Response:
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
    pytest.param(httpx.ConnectError, id="connect-error"),
    pytest.param(httpx.ConnectTimeout, id="connect-timeout"),
    pytest.param(httpx.ReadTimeout, id="read-timeout"),
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


def _always_refused(_request: httpx.Request) -> httpx.Response:
    """Refuse every connection, forcing the consume-directory fallback."""
    msg = "connection refused"
    raise httpx.ConnectError(msg)


# ---------------------------------------------------------------------------
# Upload tests
# ---------------------------------------------------------------------------


class TestUploadDocument:
    """Document upload tests."""

    def test_upload_document(self, sample_pdf: Path) -> None:
        """Upload returns task UUID on success."""
        task_uuid = "abc-123-def"

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=task_uuid)

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            _transport=transport,
        )
        result = client.upload_document(sample_pdf, title="Test Doc")
        assert result.delivered_to_api is True
        assert result.task_uuid == task_uuid
        assert result.consume_dir_path is None
        client.close()

    def test_upload_with_tags(self, sample_pdf: Path) -> None:
        """Upload includes repeated tag form fields."""
        captured_data: dict[str, str] = {}

        def handler(_request: httpx.Request) -> httpx.Response:
            content = _request.content.decode("utf-8", errors="replace")
            captured_data["content"] = content
            return httpx.Response(200, json="task-id")

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            _transport=transport,
        )
        client.upload_document(sample_pdf, title="Test", tags=[1, 2, 3])
        # Tags should appear as repeated form fields
        content = captured_data["content"]
        assert content.count("tags") >= 3
        client.close()

    def test_upload_with_correspondent(self, sample_pdf: Path) -> None:
        """Upload includes correspondent field."""
        captured_data: dict[str, str] = {}

        def handler(_request: httpx.Request) -> httpx.Response:
            content = _request.content.decode("utf-8", errors="replace")
            captured_data["content"] = content
            return httpx.Response(200, json="task-id")

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            _transport=transport,
        )
        client.upload_document(sample_pdf, title="Test", correspondent=5)
        assert "correspondent" in captured_data["content"]
        assert "5" in captured_data["content"]
        client.close()

    def test_upload_with_created(self, sample_pdf: Path) -> None:
        """Upload includes created date field."""
        captured_data: dict[str, str] = {}

        def handler(_request: httpx.Request) -> httpx.Response:
            content = _request.content.decode("utf-8", errors="replace")
            captured_data["content"] = content
            return httpx.Response(200, json="task-id")

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            _transport=transport,
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

        def handler(_request: httpx.Request) -> httpx.Response:
            captured_data["body"] = _request.content
            return httpx.Response(200, json="task-id")

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            _transport=transport,
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

    def test_upload_retry_on_network_error(self, sample_pdf: Path) -> None:
        """Upload retries on ConnectError and eventually succeeds."""
        call_count = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if call_count["n"] <= 2:
                msg = "connection refused"
                raise httpx.ConnectError(msg)
            return httpx.Response(200, json="task-id-ok")

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            _transport=transport,
            max_retries=3,
        )
        result = client.upload_document(sample_pdf, title="Retry Test")
        assert result.delivered_to_api is True
        assert result.task_uuid == "task-id-ok"
        assert call_count["n"] == 3
        client.close()

    def test_upload_no_retry_on_4xx(self, sample_pdf: Path) -> None:
        """Upload does not retry on 4xx errors."""
        call_count = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(400, text="Bad Request")

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            _transport=transport,
        )
        with pytest.raises(PaperlessError, match="rejected"):
            client.upload_document(sample_pdf, title="Bad")
        assert call_count["n"] == 1
        client.close()

    def test_upload_retry_exhausted_no_fallback(self, sample_pdf: Path) -> None:
        """Upload raises PaperlessError when retries are exhausted without fallback."""

        def handler(_request: httpx.Request) -> httpx.Response:
            msg = "connection refused"
            raise httpx.ConnectError(msg)

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            _transport=transport,
            max_retries=3,
        )
        with pytest.raises(PaperlessError, match="retries"):
            client.upload_document(sample_pdf, title="Fail")
        client.close()

    def test_upload_retry_exhausted_with_fallback(
        self, sample_pdf: Path, tmp_path: Path
    ) -> None:
        """Upload falls back to consume directory when retries are exhausted."""
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()

        def handler(_request: httpx.Request) -> httpx.Response:
            msg = "connection refused"
            raise httpx.ConnectError(msg)

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            consume_dir=str(consume_dir),
            _transport=transport,
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
        client.close()


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

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=build_payload("SUCCESS", None))

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

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=build_payload("FAILURE", "disk on fire"))

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

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=build_payload("REVOKED", "cancelled"))

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

        def handler(_request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return httpx.Response(200, json=build_no_task())
            return httpx.Response(200, json=build_payload("SUCCESS", None))

        client = _poll_client(handler)
        try:
            result = client.poll_task("t1", timeout=30)
            assert str(result["status"]).upper() == "SUCCESS"
            assert call_count["n"] >= 2
        finally:
            client.close()

    def test_failure_without_a_message_still_raises(self) -> None:
        """A failure carrying neither field raises with a stand-in message."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"status": "FAILURE", "task_id": "t1"}])

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

        def handler(_request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(401, text="Invalid token")

        client = _poll_client(handler)
        try:
            with pytest.raises(PaperlessError, match="401"):
                client.poll_task("t1", timeout=30)
            assert call_count["n"] == 1
        finally:
            client.close()

    def test_non_200_body_is_truncated(self) -> None:
        """T-23-16: a huge error body cannot flood job.error or the status area."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text="x" * 10_000)

        client = _poll_client(handler)
        try:
            with pytest.raises(PaperlessError) as exc_info:
                client.poll_task("t1", timeout=30)
            assert len(str(exc_info.value)) < 1000
            assert "truncated" in str(exc_info.value)
        finally:
            client.close()

    def test_timeout_raises_naming_the_task(self) -> None:
        """A deadline-expired poll raises and names the task id."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"status": "PENDING", "task_id": "t1"}])

        client = _poll_client(handler)
        try:
            with pytest.raises(PaperlessTimeoutError, match="t1"):
                client.poll_task("t1", timeout=0.05)
        finally:
            client.close()

    def test_timeout_is_catchable_as_a_paperless_error(self) -> None:
        """D-11: `except PaperlessError` catches the timeout subclass too."""
        assert issubclass(PaperlessTimeoutError, PaperlessError)

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"status": "PENDING", "task_id": "t1"}])

        client = _poll_client(handler)
        try:
            with pytest.raises(PaperlessError):
                client.poll_task("t1", timeout=0.05)
        finally:
            client.close()

    def test_timeout_does_not_sleep_past_its_own_deadline(self) -> None:
        """
        A 0.05 s budget costs 0.05 s, not the 0.5 s first sleep.

        0.5 s is exactly what the unclamped `time.sleep(delay)` cost
        unconditionally, so that threshold is the regression this asserts.
        """

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[{"status": "PENDING", "task_id": "t1"}])

        client = _poll_client(handler)
        try:
            start = time.monotonic()
            with pytest.raises(PaperlessTimeoutError):
                client.poll_task("t1", timeout=0.05)
            assert time.monotonic() - start < 0.5
        finally:
            client.close()


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
        self, exc_type: type[httpx.TransportError]
    ) -> None:
        """
        Every transport-level failure is UNREACHABLE, not an escaped exception.

        ConnectTimeout and ReadTimeout used to propagate past the narrow
        `except httpx.ConnectError` and hit routes.py's blanket handler,
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
        produced.add(_connection_result_for_exception(httpx.ConnectError))
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
        assert _connection_result_for_exception(httpx.ConnectError) == "unreachable"

    def test_connection_test_hits_the_tags_endpoint(self) -> None:
        """The probe is a one-row GET against /api/tags/."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert "/api/tags/" in str(request.url)
            assert "page_size=1" in str(request.url)
            return httpx.Response(200, json={"count": 0, "results": []})

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            _transport=transport,
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

        def handler(_request: httpx.Request) -> httpx.Response:
            msg = "connection refused"
            raise httpx.ConnectError(msg)

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            consume_dir=str(consume_dir),
            _transport=transport,
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

        def handler(_request: httpx.Request) -> httpx.Response:
            msg = "connection refused"
            raise httpx.ConnectError(msg)

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            consume_dir=str(consume_dir),
            _transport=transport,
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
            consume_dir=str(consume_dir),
            _transport=_make_transport(_always_refused),
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
            consume_dir=str(consume_dir),
            _transport=_make_transport(_always_refused),
            max_retries=1,
        )
        try:
            with pytest.raises(OSError, match="no space left"):
                client.upload_document(sample_pdf, title="Doomed")
        finally:
            client.close()

        assert sorted(entry.name for entry in consume_dir.iterdir()) == []

    def test_consume_dir_logs_warning_on_create(
        self, sample_pdf: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A warning is logged when creating the consume directory."""
        consume_dir = tmp_path / "warn-consume"
        assert not consume_dir.exists()

        def handler(_request: httpx.Request) -> httpx.Response:
            msg = "connection refused"
            raise httpx.ConnectError(msg)

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            consume_dir=str(consume_dir),
            _transport=transport,
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

        def handler(_request: httpx.Request) -> httpx.Response:
            captured_headers["auth"] = _request.headers.get("authorization")
            return httpx.Response(200, json={"status": "ok"})

        transport = _make_transport(handler)
        auth = "my-secret-token"
        client = PaperlessClient(
            url="http://paperless:8000",
            token=auth,
            _transport=transport,
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

        def handler(request: httpx.Request) -> httpx.Response:
            captured_headers["accept"] = request.headers.get("accept")
            return httpx.Response(200, json={"count": 0, "results": []})

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            _transport=transport,
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

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=b"null",
                headers={"content-type": "application/json"},
            )

        transport = httpx.MockTransport(handler)
        client = PaperlessClient(
            "http://localhost:8000",
            "token",
            _transport=transport,
        )
        try:
            with pytest.raises(PaperlessError, match="no task ID"):
                client.upload_document(sample_pdf, "Null Task")
        finally:
            client.close()
