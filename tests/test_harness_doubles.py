"""
Tests of the test doubles themselves.

A double that encodes the wrong wire behaviour makes every test built on it
prove nothing, so each capability the delivery tests lean on is pinned here:
the fake clock, the three ways an upload can fail, the golden metadata ids,
the advertised API version, the duplicate task answer and the late-answering
loopback server.
"""

from __future__ import annotations

import threading

import httpx2
import pytest

from tests.fake_clock import FakeClock
from tests.golden_support import (
    CORRESPONDENTS_PATH,
    DOCUMENTS_PATH,
    GOLDEN_CORRESPONDENT_IDS,
    GOLDEN_TAG_IDS,
    TAGS_PATH,
    TASKS_PATH,
    RecordingPaperless,
    UploadFailure,
    loopback_paperless,
)

_BASE = "http://paperless.test"

# How long the gated-upload test lets the client wait for an answer.  Far
# shorter than the gate's own five-second hold, so the timeout is the client's.
_CLIENT_READ_TIMEOUT = 0.3


def _client(recorder: RecordingPaperless) -> httpx2.Client:
    """
    Build a client whose every request goes to ``recorder``.

    Args:
        recorder: The in-memory paperless-ngx.

    Returns:
        A client that never opens a socket.

    """
    return httpx2.Client(base_url=_BASE, transport=httpx2.MockTransport(recorder))


def _upload(client: httpx2.Client) -> httpx2.Response:
    """
    Send one minimal upload.

    Args:
        client: The client to send it with.

    Returns:
        The upload's answer.

    """
    return client.post(DOCUMENTS_PATH, files={"document": ("a.pdf", b"%PDF-1.7")})


def _ids(response: httpx2.Response) -> list[int]:
    """
    Read the ids out of one page of a paginated collection.

    Args:
        response: A tag or correspondent list answer.

    Returns:
        Each result's id, in order.

    """
    body = response.json()
    assert set(body) == {"count", "next", "previous", "results"}
    assert body["count"] == len(body["results"])
    return [item["id"] for item in body["results"]]


def test_fake_clock_records_each_wait_and_advances_by_it() -> None:
    """Sleeping returns at once, moves time on exactly, and is remembered."""
    clock = FakeClock(start=100.0)

    clock.sleep(1.5)
    clock.sleep(2.0)
    clock.advance(10.0)

    assert clock.waits == [1.5, 2.0]
    assert clock.now() == pytest.approx(113.5)


@pytest.mark.parametrize("step", ["sleep", "advance"])
def test_fake_clock_refuses_a_negative_step(step: str) -> None:
    """Neither a wait nor an advance may move time backwards."""
    clock = FakeClock()

    with pytest.raises(ValueError, match="negative"):
        getattr(clock, step)(-0.1)

    assert clock.now() == 0.0
    assert clock.waits == []


def test_before_send_failure_raises_connect_error_and_is_recorded() -> None:
    """A refused connection still counts as an upload attempt."""
    recorder = RecordingPaperless(upload_failure=UploadFailure.BEFORE_SEND)

    with _client(recorder) as client, pytest.raises(httpx2.ConnectError):
        _upload(client)

    assert len(recorder.uploads()) == 1
    assert recorder.issued == []


def test_after_send_failure_answers_500() -> None:
    """A received upload is answered as a restarting paperless-ngx answers it."""
    recorder = RecordingPaperless(upload_failure=UploadFailure.AFTER_SEND)

    with _client(recorder) as client:
        response = _upload(client)

    assert response.status_code == 500
    assert "restarting" in response.text
    assert len(recorder.uploads()) == 1
    assert recorder.issued == []


def test_read_timeout_failure_raises_read_timeout_and_is_recorded() -> None:
    """An upload that is never answered in time is still an attempt."""
    recorder = RecordingPaperless(upload_failure=UploadFailure.READ_TIMEOUT)

    with _client(recorder) as client, pytest.raises(httpx2.ReadTimeout):
        _upload(client)

    assert len(recorder.uploads()) == 1
    assert recorder.issued == []


def test_an_accepted_upload_is_issued_a_task_id() -> None:
    """Without a failure mode the upload is answered with a fresh task id."""
    recorder = RecordingPaperless()

    with _client(recorder) as client:
        response = _upload(client)

    assert response.status_code == 200
    assert response.json() == "golden-task-1"
    assert recorder.issued == ["golden-task-1"]
    assert recorder.upload_failure is None


def test_metadata_lists_carry_the_golden_ids_by_default() -> None:
    """The tag and correspondent lists hold what a golden run submits."""
    recorder = RecordingPaperless()

    with _client(recorder) as client:
        tags = client.get(TAGS_PATH)
        correspondents = client.get(CORRESPONDENTS_PATH)

    assert _ids(tags) == list(GOLDEN_TAG_IDS)
    assert _ids(correspondents) == list(GOLDEN_CORRESPONDENT_IDS)
    assert "X-Api-Version" not in tags.headers


def test_metadata_lists_can_be_given_other_ids() -> None:
    """A test can list ids other than the golden ones, or none at all."""
    recorder = RecordingPaperless(tags=[5], correspondents=[])

    with _client(recorder) as client:
        tags = client.get(TAGS_PATH)
        correspondents = client.get(CORRESPONDENTS_PATH)

    assert _ids(tags) == [5]
    assert _ids(correspondents) == []


def test_api_version_is_advertised_on_answers() -> None:
    """Metadata, upload and poll answers all carry the version header."""
    recorder = RecordingPaperless(api_version=10)

    with _client(recorder) as client:
        tags = client.get(TAGS_PATH)
        upload = _upload(client)
        poll = client.get(TASKS_PATH, params={"task_id": upload.json()})

    assert tags.headers["X-Api-Version"] == "10"
    assert upload.headers["X-Api-Version"] == "10"
    assert poll.headers["X-Api-Version"] == "10"


def test_duplicate_poll_answers_in_the_version_9_shape() -> None:
    """Before version 10 the duplicate is named in the failure text."""
    recorder = RecordingPaperless(duplicate_of=42)

    with _client(recorder) as client:
        task_id = _upload(client).json()
        poll = client.get(TASKS_PATH, params={"task_id": task_id})

    (task,) = poll.json()
    assert task["task_id"] == task_id
    assert task["status"] == "FAILURE"
    assert "(#42)" in task["result"]
    assert task["related_document"] == "42"


def test_duplicate_poll_answers_in_the_version_10_shape() -> None:
    """From version 10 the duplicate is a structured id in a paginated list."""
    recorder = RecordingPaperless(api_version=10, duplicate_of=42)

    with _client(recorder) as client:
        task_id = _upload(client).json()
        poll = client.get(TASKS_PATH, params={"task_id": task_id})

    body = poll.json()
    assert set(body) == {"count", "next", "previous", "results"}
    (task,) = body["results"]
    assert task["task_id"] == task_id
    assert task["status"] == "failure"
    assert task["result_data"]["duplicate_of"] == 42
    assert task["result_data"]["duplicate_in_trash"] is False


def test_loopback_lists_the_golden_ids() -> None:
    """The real-socket server lists the same metadata as the in-memory one."""
    with loopback_paperless() as server, httpx2.Client(base_url=server.url) as client:
        tags = client.get(TAGS_PATH)
        correspondents = client.get(CORRESPONDENTS_PATH)

    assert _ids(tags) == list(GOLDEN_TAG_IDS)
    assert _ids(correspondents) == list(GOLDEN_CORRESPONDENT_IDS)


def test_gated_loopback_holds_its_upload_answer() -> None:
    """The upload arrives, and its answer does not until the gate is set."""
    gate = threading.Event()
    failures: list[BaseException] = []

    with loopback_paperless(answer_gate=gate) as server:

        def post() -> None:
            """Upload once with a read timeout far shorter than the hold."""
            with httpx2.Client(
                base_url=server.url,
                timeout=httpx2.Timeout(5.0, read=_CLIENT_READ_TIMEOUT),
            ) as client:
                try:
                    _upload(client)
                except httpx2.ReadTimeout as error:
                    failures.append(error)

        sender = threading.Thread(target=post, name="gated-upload")
        sender.start()
        sender.join(timeout=5.0)
        assert not sender.is_alive()
        hits = list(server.hits)

    assert gate.is_set()
    assert len(failures) == 1
    assert isinstance(failures[0], httpx2.ReadTimeout)
    assert [(hit.method, hit.path) for hit in hits] == [("POST", DOCUMENTS_PATH)]
