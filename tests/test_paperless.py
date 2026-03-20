"""Tests for paperless-ngx REST client."""

import httpx
import pytest

from saneless.exceptions import PaperlessError
from saneless.paperless import PaperlessClient

_MOCK_AUTH = "testtoken"


@pytest.fixture
def sample_pdf(tmp_path):
    """Create a minimal PDF file for upload tests."""
    pdf_path = tmp_path / "test.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake content")
    return pdf_path


def _make_transport(handler):
    """Create an httpx.MockTransport from a handler function."""
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# Upload tests
# ---------------------------------------------------------------------------


class TestUploadDocument:
    """Document upload tests."""

    def test_upload_document(self, sample_pdf) -> None:
        """Upload returns task UUID on success."""
        task_uuid = "abc-123-def"

        def handler(_request):
            return httpx.Response(200, json=task_uuid)

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            _transport=transport,
        )
        result = client.upload_document(sample_pdf, title="Test Doc")
        assert result == task_uuid
        client.close()

    def test_upload_with_tags(self, sample_pdf) -> None:
        """Upload includes repeated tag form fields."""
        captured_data = {}

        def handler(_request):
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

    def test_upload_with_correspondent(self, sample_pdf) -> None:
        """Upload includes correspondent field."""
        captured_data = {}

        def handler(_request):
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

    def test_upload_with_created(self, sample_pdf) -> None:
        """Upload includes created date field."""
        captured_data = {}

        def handler(_request):
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
        assert "2026-03-20" in captured_data["content"]
        client.close()

    def test_upload_retry_on_network_error(self, sample_pdf) -> None:
        """Upload retries on ConnectError and eventually succeeds."""
        call_count = {"n": 0}

        def handler(_request):
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
        assert result == "task-id-ok"
        assert call_count["n"] == 3
        client.close()

    def test_upload_no_retry_on_4xx(self, sample_pdf) -> None:
        """Upload does not retry on 4xx errors."""
        call_count = {"n": 0}

        def handler(_request):
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

    def test_upload_retry_exhausted_no_fallback(self, sample_pdf) -> None:
        """Upload raises PaperlessError when retries are exhausted without fallback."""

        def handler(_request):
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

    def test_upload_retry_exhausted_with_fallback(self, sample_pdf, tmp_path) -> None:
        """Upload falls back to consume directory when retries are exhausted."""
        consume_dir = tmp_path / "consume"
        consume_dir.mkdir()

        def handler(_request):
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
        assert result == "fallback"
        # PDF should have been copied to consume dir
        copied = list(consume_dir.iterdir())
        assert len(copied) == 1
        assert copied[0].name == "test.pdf"
        client.close()


# ---------------------------------------------------------------------------
# Poll task tests
# ---------------------------------------------------------------------------


class TestPollTask:
    """Task polling tests."""

    def test_poll_task_success(self) -> None:
        """Polling returns SUCCESS when task completes."""

        def handler(_request):
            return httpx.Response(200, json=[{"status": "SUCCESS", "task_id": "t1"}])

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            _transport=transport,
        )
        result = client.poll_task("t1", timeout=10)
        assert result["status"] == "SUCCESS"
        client.close()

    def test_poll_task_failure(self) -> None:
        """Polling returns FAILURE when task fails."""

        def handler(_request):
            return httpx.Response(
                200, json=[{"status": "FAILURE", "task_id": "t1", "result": "error"}]
            )

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            _transport=transport,
        )
        result = client.poll_task("t1", timeout=10)
        assert result["status"] == "FAILURE"
        client.close()

    def test_poll_task_timeout(self) -> None:
        """Polling returns TIMEOUT when deadline is exceeded."""

        def handler(_request):
            return httpx.Response(200, json=[{"status": "PENDING", "task_id": "t1"}])

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            _transport=transport,
        )
        # Very short timeout to trigger timeout quickly
        result = client.poll_task("t1", timeout=0.1)
        assert result["status"] == "TIMEOUT"
        client.close()

    def test_poll_task_not_found_first_retry(self) -> None:
        """Pitfall #8: task not found on first poll, succeeds on second."""
        call_count = {"n": 0}

        def handler(_request):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[{"status": "SUCCESS", "task_id": "t1"}])

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            _transport=transport,
        )
        result = client.poll_task("t1", timeout=30)
        assert result["status"] == "SUCCESS"
        assert call_count["n"] >= 2
        client.close()


# ---------------------------------------------------------------------------
# Connection test
# ---------------------------------------------------------------------------


class TestConnectionTest:
    """Connection test method tests."""

    def test_test_connection_connected(self) -> None:
        """Connection test returns 'connected' on 200 response."""

        def handler(_request):
            return httpx.Response(200, json={"status": "ok"})

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            _transport=transport,
        )
        assert client.test_connection() == "connected"
        client.close()

    def test_test_connection_token_rejected(self) -> None:
        """Connection test returns 'token_rejected' on 401 response."""

        def handler(_request):
            return httpx.Response(401, text="Unauthorized")

        transport = _make_transport(handler)
        auth = "badtoken"
        client = PaperlessClient(
            url="http://paperless:8000",
            token=auth,
            _transport=transport,
        )
        assert client.test_connection() == "token_rejected"
        client.close()

    def test_test_connection_unreachable(self) -> None:
        """Connection test returns 'unreachable' on ConnectError."""

        def handler(_request):
            msg = "connection refused"
            raise httpx.ConnectError(msg)

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            _transport=transport,
        )
        assert client.test_connection() == "unreachable"
        client.close()


# ---------------------------------------------------------------------------
# Auth header test
# ---------------------------------------------------------------------------


class TestAuthHeader:
    """Authentication header tests."""

    def test_auth_header(self) -> None:
        """Authorization header contains Token prefix and credential."""
        captured_headers = {}

        def handler(_request):
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
