"""
Every failure cause produces the headline and next step its cause deserves.

One table pairs each cause with the category, the exit code, the headline and
the next step it must end in.  No row builds its exception by hand: each one
drives the real raise site the way production reaches it -- the paperless-ngx
client over a mock transport, the spool, the workspace, the assembly room rule,
the PDF writer, the device pick and a whole mismatched duplex run -- and
classifies whatever that site raised.  So a change to one site, or to one arm of
``classify_error``, fails a row that names the cause.

Beyond the category, the table pins what each cause must never be called:

* a failure after the upload may have arrived is amber, and its next step sends
  the operator to paperless-ngx's document list, never straight back to the
  scanner;
* a full disk is disk space, never a scanner, PDF or configuration fault;
* finding no scanner is a scanner condition, never a configuration one.

Two outcomes are not exceptions and have tests of their own: a duplicate is a
delivered scan with a warning, and a restart while uploading is stored amber.

Nothing here waits in real time: every client runs its send budget and its
poll on a fake clock.
"""

from __future__ import annotations

import errno
import os
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, NoReturn
from unittest.mock import MagicMock

import httpx2
import pytest
from PIL import Image, ImageDraw

import saneless.pdf as pdf_module
import saneless.preservation as preservation_module
from saneless.exceptions import (
    DiskSpaceError,
    NoScannerFoundError,
    PaperlessError,
    PaperlessIncompatibleError,
    PaperlessTimeoutError,
    PaperlessUncertainSendError,
    PaperlessUnconfirmedError,
    failure_text,
)
from saneless.job import JobStore
from saneless.paperless import PaperlessClient, PaperlessTiming
from saneless.pdf import assemble_pdf
from saneless.pipeline import (
    PipelineRequest,
    _check_disk_space,
    _open_workspace,
    run_pipeline,
)
from saneless.preservation import ensure_room_to_assemble
from saneless.scanner.base import ScannerBackend
from saneless.spool import SpooledPageSink
from saneless.vocabulary import (
    RESTART_UPLOADING_REASON,
    UNCONFIRMED_SEND_LABEL,
    WARNED_UPLOAD_LABEL,
    ErrorCategory,
    ExitCode,
    JobState,
    ScanOutcome,
    classify_error,
    error_message,
    error_next_step,
    exit_code_for,
    exit_code_for_outcome,
    is_amber_category,
    job_label,
)
from tests.conftest import (
    AlwaysContinueFlipCoordinator,
    build_settings,
    spooling,
    spooling_in_turn,
)
from tests.fake_clock import FakeClock
from tests.golden_support import DOCUMENTS_PATH, TASKS_PATH, RecordingPaperless

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from saneless.config import Settings

_URL = "http://paperless:8000"
_AUTH = "headline-token"
_TASK_ID = "task-headline-1"
_TITLE = "Headline Scan"
# More than any disk this suite runs on, so the spool's per-page room check
# reports a shortfall without patching the measurement.
_IMPOSSIBLE_RESERVE_MB = 1_000_000_000

# What every amber next step must say, and what it must never say alone.
_CHECK_FIRST = "document list"
_BLIND_RESCAN = "start the scan again"

# The categories a full disk used to be filed under, one per old raise site.
_NOT_A_FULL_DISK = frozenset(
    {ErrorCategory.SCANNER, ErrorCategory.ASSEMBLY, ErrorCategory.CONFIG}
)

type _Respond = Callable[[httpx2.Request], httpx2.Response]
type _Build = Callable[[Path, pytest.MonkeyPatch], Exception]


@dataclass(frozen=True, slots=True)
class _HeadlineCase:
    """
    One failure cause and what the operator must be told about it.

    Attributes:
        label: The row's test id, naming the cause.
        build: Drives the real raise site and returns what it raised.
        category: The category the exception must classify as.
        exit_code: The CLI exit code that category must map to.
        message_fragment: Text the category's headline must contain.
        next_step_fragment: Text the category's next step must contain.
        detail_fragment: Text the raise site's own message must contain, so
            the row proves it reached the site it names.

    """

    label: str
    build: _Build
    category: ErrorCategory
    exit_code: ExitCode
    message_fragment: str
    next_step_fragment: str
    detail_fragment: str


# ---------------------------------------------------------------------------
# The paperless-ngx client, over a mock transport and a fake clock
# ---------------------------------------------------------------------------


def _client(
    respond: _Respond, *, consume_dir: Path | None = None, send_budget: float = 60.0
) -> PaperlessClient:
    """
    Build a real client answered by ``respond``, on a fake clock.

    The default send budget is production's 60 s, spent on the fake clock, so
    a before-send failure reads exactly as it does in production and costs no
    real time.

    Args:
        respond: The transport handler.
        consume_dir: The fallback folder, or None for none.
        send_budget: How long before-send failures are retried for.

    Returns:
        The client.

    """
    clock = FakeClock()
    return PaperlessClient(
        url=_URL,
        token=_AUTH,
        consume_dir=consume_dir,
        transport=httpx2.MockTransport(respond),
        timing=PaperlessTiming(
            send_budget=send_budget, clock=clock.now, sleep=clock.sleep
        ),
    )


def _raising(exc: Exception) -> _Respond:
    """Build a handler that raises ``exc`` for every request."""

    def respond(_request: httpx2.Request) -> httpx2.Response:
        raise exc

    return respond


def _answering(status: int, *, json: object = None, text: str = "") -> _Respond:
    """
    Build a handler that answers every request with one response.

    Args:
        status: The status code.
        json: The JSON body, or None to send ``text`` instead.
        text: The plain-text body, used when ``json`` is None.

    Returns:
        The handler.

    """

    def respond(_request: httpx2.Request) -> httpx2.Response:
        if json is None:
            return httpx2.Response(status, text=text)
        return httpx2.Response(status, json=json)

    return respond


def _pdf(tmp_path: Path) -> Path:
    """Write a small stand-in PDF to upload."""
    pdf_path = tmp_path / "scan.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 headline content")
    return pdf_path


def _upload_failing[E: Exception](
    respond: _Respond,
    expected: type[E],
    *,
    consume_dir_name: str | None = None,
) -> _Build:
    """
    Build a row that uploads through ``respond`` and catches ``expected``.

    Args:
        respond: How paperless-ngx answers the upload.
        expected: The class the upload must raise.
        consume_dir_name: The consume folder's name under the test's tmp_path,
            which is never created, or None for no consume folder.

    Returns:
        The row's builder.

    """

    def build(tmp_path: Path, _monkeypatch: pytest.MonkeyPatch) -> Exception:
        consume_dir = None if consume_dir_name is None else tmp_path / consume_dir_name
        client = _client(respond, consume_dir=consume_dir)
        try:
            with pytest.raises(expected) as excinfo:
                client.upload_document(_pdf(tmp_path), _TITLE)
        finally:
            client.close()
        return excinfo.value

    return build


def _poll_failing[E: Exception](
    respond: _Respond, expected: type[E], *, timeout: float = 30.0
) -> _Build:
    """
    Build a row that polls an accepted task through ``respond``.

    Args:
        respond: How paperless-ngx answers the task poll.
        expected: The class the poll must raise.
        timeout: The poll's deadline, on the fake clock.

    Returns:
        The row's builder.

    """

    def build(_tmp_path: Path, _monkeypatch: pytest.MonkeyPatch) -> Exception:
        client = _client(respond)
        try:
            with pytest.raises(expected) as excinfo:
                client.poll_task(_TASK_ID, timeout=timeout)
        finally:
            client.close()
        return excinfo.value

    return build


def _task(status: str, **fields: object) -> _Respond:
    """Build a handler answering the v9 task list with our task in ``status``."""
    return _answering(200, json=[{"task_id": _TASK_ID, "status": status, **fields}])


# ---------------------------------------------------------------------------
# The disk, the workspace and the device pick
# ---------------------------------------------------------------------------


def _content_page() -> Image.Image:
    """Return a page with ink on it, so no blank filter drops it."""
    image = Image.new("RGB", (200, 300), "white")
    ImageDraw.Draw(image).rectangle((20, 20, 180, 280), fill="black")
    return image


def _out_of_space(*_args: object, **_kwargs: object) -> NoReturn:
    """Fail the way a write to a full disk fails."""
    raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))


def _pre_check_short(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Exception:
    """Run the pipeline's free-space pre-check on a disk with nothing free."""
    real_usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr(
        "saneless.pipeline.shutil.disk_usage",
        lambda _path: real_usage._replace(free=0),
    )
    with pytest.raises(DiskSpaceError) as excinfo:
        _check_disk_space(tmp_path, 10)
    return excinfo.value


def _spool_short(tmp_path: Path, _monkeypatch: pytest.MonkeyPatch) -> Exception:
    """Spool page 1 with too little room for it."""
    sink = SpooledPageSink(tmp_path, "a", _IMPOSSIBLE_RESERVE_MB)
    with pytest.raises(DiskSpaceError) as excinfo:
        sink.add(_content_page(), dpi=300)
    return excinfo.value


def _spool_write_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Exception:
    """Spool page 1 onto a disk that fills between the check and the write."""
    monkeypatch.setattr(Image.Image, "save", _out_of_space)
    sink = SpooledPageSink(tmp_path, "a", 0)
    with pytest.raises(DiskSpaceError) as excinfo:
        sink.add(_content_page(), dpi=300)
    return excinfo.value


def _workspace_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Exception:
    """Open the job workspace in tmp_dir on a full disk."""
    monkeypatch.setattr("saneless.workspace.tempfile.mkdtemp", _out_of_space)
    request = PipelineRequest(profile_name="default", title=_TITLE)
    with (
        pytest.raises(DiskSpaceError) as excinfo,
        _open_workspace(tmp_path / "work", 0, request),
    ):
        pass
    return excinfo.value


def _tmp_dir_create_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Exception:
    """Create a missing tmp_dir on a full disk."""
    monkeypatch.setattr("saneless.private_dirs.make_private_dir", _out_of_space)
    request = PipelineRequest(profile_name="default", title=_TITLE)
    with (
        pytest.raises(DiskSpaceError) as excinfo,
        _open_workspace(tmp_path / "work", 0, request),
    ):
        pass
    return excinfo.value


def _spooled_page(tmp_path: Path) -> SpooledPageSink:
    """Spool one real page into a directory of its own."""
    spool_dir = tmp_path / "spool"
    spool_dir.mkdir()
    sink = SpooledPageSink(spool_dir, "a", 0)
    sink.add(_content_page(), dpi=300)
    return sink


def _assembly_room_short(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Exception:
    """Apply the assembly room rule on a disk with nothing free."""
    sink = _spooled_page(tmp_path)
    monkeypatch.setattr(preservation_module, "_free_bytes", lambda _directory: 0)
    with pytest.raises(DiskSpaceError) as excinfo:
        ensure_room_to_assemble(sink.records, tmp_path, reserve_mb=10)
    return excinfo.value


def _assembly_write_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Exception:
    """Assemble the PDF on a disk that fills while the pages convert."""
    sink = _spooled_page(tmp_path)
    monkeypatch.setattr(pdf_module.img2pdf, "convert", _out_of_space)
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    with pytest.raises(DiskSpaceError) as excinfo:
        assemble_pdf(sink.records, output_dir, "scan.pdf", title=_TITLE)
    return excinfo.value


def _no_scanner(tmp_path: Path, _monkeypatch: pytest.MonkeyPatch) -> Exception:
    """Start a scan that asks for discovery, with no scanner answering."""
    settings = build_settings(tmp_path)
    settings.scanner.device = ""
    scanner = MagicMock(spec=ScannerBackend)
    scanner.get_devices.return_value = []
    with pytest.raises(NoScannerFoundError) as excinfo:
        run_pipeline(
            scanner=scanner,
            paperless=MagicMock(spec=PaperlessClient),
            settings=settings,
            request=PipelineRequest(profile_name="default", title=_TITLE),
        )
    scanner.scan_pages.assert_not_called()
    return excinfo.value


# ---------------------------------------------------------------------------
# A mismatched manual duplex run: the fronts accepted, the backs refused
# ---------------------------------------------------------------------------


def _manual_duplex_settings(tmp_path: Path) -> Settings:
    """Return settings whose default profile is manual duplex from the feeder."""
    settings = build_settings(tmp_path)
    settings.profiles["default"].source = "ADF"
    settings.profiles["default"].duplex = "manual"
    return settings


def _fronts_accepted_backs_refused() -> _Respond:
    """Accept the first upload with a task id and refuse the second with a 400."""
    uploads: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == DOCUMENTS_PATH:
            uploads.append(request)
            if len(uploads) == 1:
                return httpx2.Response(200, json="fronts-task")
            return httpx2.Response(400, json={"document": ["Refused by the test."]})
        if request.url.path == TASKS_PATH:
            return httpx2.Response(
                200, json=[{"task_id": "fronts-task", "status": "SUCCESS"}]
            )
        return httpx2.Response(418, text=f"unexpected {request.url.path}")

    return respond


def _half_delivered(tmp_path: Path, _monkeypatch: pytest.MonkeyPatch) -> Exception:
    """Scan three fronts and two backs; only the fronts reach paperless-ngx."""
    scanner = MagicMock(spec=ScannerBackend)
    scanner.scan_pages.side_effect = spooling_in_turn(
        [_content_page() for _ in range(3)], [_content_page() for _ in range(2)]
    )
    client = _client(_fronts_accepted_backs_refused())
    try:
        with pytest.raises(PaperlessUnconfirmedError) as excinfo:
            run_pipeline(
                scanner=scanner,
                paperless=client,
                settings=_manual_duplex_settings(tmp_path),
                request=PipelineRequest(
                    profile_name="default",
                    title=_TITLE,
                    job_id="job-headline-half",
                    flip_coordinator=AlwaysContinueFlipCoordinator(),
                ),
            )
    finally:
        client.close()
    return excinfo.value


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------

_SEND_HEADLINE = "may have reached paperless-ngx"
_FILING_HEADLINE = "received the document but did not confirm"
_UPLOAD_HEADLINE = "could not be sent to paperless-ngx"
_DISK_HEADLINE = "ran out of disk space"
_AMBER_NEXT = "Check paperless-ngx's document list before scanning again"
_UPLOAD_NEXT = "paperless.url or paperless.token is wrong"
_DISK_NEXT = "Free space on the server"

_CASES = [
    _HeadlineCase(
        label="poll-deadline-after-acceptance",
        build=_poll_failing(_task("PENDING"), PaperlessTimeoutError, timeout=0.0),
        category=ErrorCategory.UNCONFIRMED_FILING,
        exit_code=ExitCode.UNCONFIRMED,
        message_fragment=_FILING_HEADLINE,
        next_step_fragment=_AMBER_NEXT,
        detail_fragment=f"Paperless task {_TASK_ID} did not finish within",
    ),
    _HeadlineCase(
        label="poll-task-failure-not-a-duplicate",
        build=_poll_failing(
            _task("FAILURE", result="The consumer crashed."),
            PaperlessUnconfirmedError,
        ),
        category=ErrorCategory.UNCONFIRMED_FILING,
        exit_code=ExitCode.UNCONFIRMED,
        message_fragment=_FILING_HEADLINE,
        next_step_fragment=_AMBER_NEXT,
        detail_fragment="ended FAILURE: The consumer crashed.",
    ),
    _HeadlineCase(
        label="poll-task-revoked",
        build=_poll_failing(_task("REVOKED"), PaperlessUnconfirmedError),
        category=ErrorCategory.UNCONFIRMED_FILING,
        exit_code=ExitCode.UNCONFIRMED,
        message_fragment=_FILING_HEADLINE,
        next_step_fragment=_AMBER_NEXT,
        detail_fragment="the document was not filed",
    ),
    _HeadlineCase(
        label="poll-401-after-acceptance",
        build=_poll_failing(
            _answering(401, json={"detail": "Invalid token."}),
            PaperlessUnconfirmedError,
        ),
        category=ErrorCategory.UNCONFIRMED_FILING,
        exit_code=ExitCode.UNCONFIRMED,
        message_fragment=_FILING_HEADLINE,
        next_step_fragment=_AMBER_NEXT,
        detail_fragment="task poll failed (401 Unauthorized)",
    ),
    _HeadlineCase(
        label="poll-406-after-acceptance",
        build=_poll_failing(_answering(406, text="nope"), PaperlessUnconfirmedError),
        category=ErrorCategory.UNCONFIRMED_FILING,
        exit_code=ExitCode.UNCONFIRMED,
        message_fragment=_FILING_HEADLINE,
        next_step_fragment=_AMBER_NEXT,
        detail_fragment=f"Could not confirm Paperless task {_TASK_ID}: ",
    ),
    _HeadlineCase(
        label="poll-proxy-502-until-the-deadline",
        build=_poll_failing(
            _answering(502, text="Bad Gateway"), PaperlessTimeoutError, timeout=3.0
        ),
        category=ErrorCategory.UNCONFIRMED_FILING,
        exit_code=ExitCode.UNCONFIRMED,
        message_fragment=_FILING_HEADLINE,
        next_step_fragment=_AMBER_NEXT,
        detail_fragment="last error: 502 Bad Gateway",
    ),
    _HeadlineCase(
        label="upload-read-timeout-after-send",
        build=_upload_failing(
            _raising(httpx2.ReadTimeout("timed out")), PaperlessUncertainSendError
        ),
        category=ErrorCategory.UNCONFIRMED_SEND,
        exit_code=ExitCode.UNCONFIRMED,
        message_fragment=_SEND_HEADLINE,
        next_step_fragment=_AMBER_NEXT,
        detail_fragment="got no answer within 30s",
    ),
    _HeadlineCase(
        label="upload-connection-dropped-after-send",
        build=_upload_failing(
            _raising(httpx2.RemoteProtocolError("peer closed connection")),
            PaperlessUncertainSendError,
            consume_dir_name="consume",
        ),
        category=ErrorCategory.UNCONFIRMED_SEND,
        exit_code=ExitCode.UNCONFIRMED,
        message_fragment=_SEND_HEADLINE,
        next_step_fragment=_AMBER_NEXT,
        detail_fragment="peer closed connection; it may have reached paperless-ngx",
    ),
    _HeadlineCase(
        label="upload-502-from-a-proxy",
        build=_upload_failing(
            _answering(502, text="Bad Gateway"), PaperlessUncertainSendError
        ),
        category=ErrorCategory.UNCONFIRMED_SEND,
        exit_code=ExitCode.UNCONFIRMED,
        message_fragment=_SEND_HEADLINE,
        next_step_fragment=_AMBER_NEXT,
        detail_fragment="got no usable answer: 502 Bad Gateway",
    ),
    _HeadlineCase(
        label="upload-504-from-a-proxy",
        build=_upload_failing(
            _answering(504, text="Gateway Timeout"), PaperlessUncertainSendError
        ),
        category=ErrorCategory.UNCONFIRMED_SEND,
        exit_code=ExitCode.UNCONFIRMED,
        message_fragment=_SEND_HEADLINE,
        next_step_fragment=_AMBER_NEXT,
        detail_fragment="got no usable answer: 504 Gateway Timeout",
    ),
    _HeadlineCase(
        label="upload-200-without-a-task-id",
        build=_upload_failing(_answering(200, json=42), PaperlessUncertainSendError),
        category=ErrorCategory.UNCONFIRMED_SEND,
        exit_code=ExitCode.UNCONFIRMED,
        message_fragment=_SEND_HEADLINE,
        next_step_fragment=_AMBER_NEXT,
        detail_fragment="answered the upload with 42, which is not a task ID",
    ),
    _HeadlineCase(
        label="upload-406-api-version-refused",
        build=_upload_failing(
            _answering(406, text="Not Acceptable"), PaperlessIncompatibleError
        ),
        category=ErrorCategory.PAPERLESS_VERSION,
        exit_code=ExitCode.PAPERLESS,
        message_fragment="does not speak an API version saneless supports",
        next_step_fragment="paperless-ngx 2.16 or later",
        detail_fragment="does not accept API version 9 or 10",
    ),
    _HeadlineCase(
        label="upload-400-refused",
        build=_upload_failing(
            _answering(400, json={"document": ["Not a PDF."]}), PaperlessError
        ),
        category=ErrorCategory.UPLOAD,
        exit_code=ExitCode.PAPERLESS,
        message_fragment=_UPLOAD_HEADLINE,
        next_step_fragment=_UPLOAD_NEXT,
        detail_fragment="rejected the upload (400 Bad Request): document: Not a PDF.",
    ),
    _HeadlineCase(
        label="upload-401-token-refused",
        build=_upload_failing(
            _answering(401, json={"detail": "Invalid token."}), PaperlessError
        ),
        category=ErrorCategory.UPLOAD,
        exit_code=ExitCode.PAPERLESS,
        message_fragment=_UPLOAD_HEADLINE,
        next_step_fragment=_UPLOAD_NEXT,
        detail_fragment="rejected the upload (401 Unauthorized): Invalid token.",
    ),
    _HeadlineCase(
        label="upload-never-connected-no-consume-dir",
        build=_upload_failing(
            _raising(httpx2.ConnectError("Connection refused")), PaperlessError
        ),
        category=ErrorCategory.UPLOAD,
        exit_code=ExitCode.PAPERLESS,
        message_fragment=_UPLOAD_HEADLINE,
        next_step_fragment=_UPLOAD_NEXT,
        detail_fragment="could not connect for 60s",
    ),
    _HeadlineCase(
        label="upload-never-connected-consume-dir-missing",
        build=_upload_failing(
            _raising(httpx2.ConnectError("Connection refused")),
            PaperlessError,
            consume_dir_name="unmounted",
        ),
        category=ErrorCategory.UPLOAD,
        exit_code=ExitCode.PAPERLESS,
        message_fragment=_UPLOAD_HEADLINE,
        next_step_fragment=_UPLOAD_NEXT,
        detail_fragment="does not exist — is the paperless-ngx volume mounted?",
    ),
    _HeadlineCase(
        label="duplex-fronts-delivered-backs-refused",
        build=_half_delivered,
        category=ErrorCategory.UNCONFIRMED_FILING,
        exit_code=ExitCode.UNCONFIRMED,
        message_fragment=_FILING_HEADLINE,
        next_step_fragment=_AMBER_NEXT,
        detail_fragment=(
            "The (fronts) half reached paperless-ngx; the (backs) half failed: "
        ),
    ),
    _HeadlineCase(
        label="disk-pre-check-short",
        build=_pre_check_short,
        category=ErrorCategory.DISK_SPACE,
        exit_code=ExitCode.DISK_SPACE,
        message_fragment=_DISK_HEADLINE,
        next_step_fragment=_DISK_NEXT,
        detail_fragment="Insufficient disk space: 0 MB free",
    ),
    _HeadlineCase(
        label="disk-spool-shortfall",
        build=_spool_short,
        category=ErrorCategory.DISK_SPACE,
        exit_code=ExitCode.DISK_SPACE,
        message_fragment=_DISK_HEADLINE,
        next_step_fragment=_DISK_NEXT,
        detail_fragment="Insufficient disk space for page 1",
    ),
    _HeadlineCase(
        label="disk-spool-write-enospc",
        build=_spool_write_full,
        category=ErrorCategory.DISK_SPACE,
        exit_code=ExitCode.DISK_SPACE,
        message_fragment=_DISK_HEADLINE,
        next_step_fragment=_DISK_NEXT,
        detail_fragment="Could not write page 1",
    ),
    _HeadlineCase(
        label="disk-tmp-dir-workspace-enospc",
        build=_workspace_full,
        category=ErrorCategory.DISK_SPACE,
        exit_code=ExitCode.DISK_SPACE,
        message_fragment=_DISK_HEADLINE,
        next_step_fragment=_DISK_NEXT,
        detail_fragment="Could not prepare the working directory",
    ),
    _HeadlineCase(
        label="disk-tmp-dir-creation-enospc",
        build=_tmp_dir_create_full,
        category=ErrorCategory.DISK_SPACE,
        exit_code=ExitCode.DISK_SPACE,
        message_fragment=_DISK_HEADLINE,
        next_step_fragment=_DISK_NEXT,
        detail_fragment="Could not prepare the working directory",
    ),
    _HeadlineCase(
        label="disk-assembly-room-rule",
        build=_assembly_room_short,
        category=ErrorCategory.DISK_SPACE,
        exit_code=ExitCode.DISK_SPACE,
        message_fragment=_DISK_HEADLINE,
        next_step_fragment=_DISK_NEXT,
        detail_fragment="Not enough free disk space to assemble 1 page(s)",
    ),
    _HeadlineCase(
        label="disk-assembly-write-enospc",
        build=_assembly_write_full,
        category=ErrorCategory.DISK_SPACE,
        exit_code=ExitCode.DISK_SPACE,
        message_fragment=_DISK_HEADLINE,
        next_step_fragment=_DISK_NEXT,
        detail_fragment="Could not assemble 1 page(s)",
    ),
    _HeadlineCase(
        label="no-scanner-found",
        build=_no_scanner,
        category=ErrorCategory.SCANNER,
        exit_code=ExitCode.SCAN,
        message_fragment="The scanner could not complete the scan.",
        next_step_fragment="switched on and connected",
        detail_fragment="No scanner found: ",
    ),
]

_HEADLINE_CASES = [pytest.param(case, id=case.label) for case in _CASES]


@pytest.mark.parametrize("case", _HEADLINE_CASES)
def test_headline_follows_cause(
    case: _HeadlineCase, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cause, raised by its real site, gets its category, code and advice."""
    exc = case.build(tmp_path, monkeypatch)

    category = classify_error(exc)
    # What each cause used to be misfiled as is ruled out by name first, so a
    # regression says which wrong category it fell back to.
    if case.category is ErrorCategory.DISK_SPACE:
        assert category not in _NOT_A_FULL_DISK, f"a full disk filed as {category}"
    if case.category is ErrorCategory.SCANNER:
        assert category is not ErrorCategory.CONFIG
    assert category is case.category
    assert exit_code_for(category) is case.exit_code
    assert case.message_fragment in error_message(category)
    next_step = error_next_step(category)
    assert case.next_step_fragment in next_step
    assert case.detail_fragment in failure_text(exc)
    if is_amber_category(category):
        # The document may already be in paperless-ngx: check before scanning.
        assert _CHECK_FIRST in next_step
        assert _BLIND_RESCAN not in next_step
    else:
        assert _CHECK_FIRST not in next_step


def test_a_duplicate_is_a_warned_delivery(tmp_path: Path) -> None:
    """
    paperless-ngx already holding the file ends DONE with a warning, exit 7.

    The poll answers that the task failed as a duplicate of document 42.
    That is not an exception at all: the scan was delivered, the warning
    names the document it duplicates, and nothing is kept in ``failed/``.
    """
    settings = build_settings(tmp_path)
    recorder = RecordingPaperless(duplicate_of=42)
    scanner = MagicMock(spec=ScannerBackend)
    scanner.scan_pages.side_effect = spooling([_content_page()])
    client = _client(recorder, send_budget=0.0)
    try:
        result = run_pipeline(
            scanner=scanner,
            paperless=client,
            settings=settings,
            request=PipelineRequest(profile_name="default", title=_TITLE),
        )
    finally:
        client.close()

    assert result.outcome is ScanOutcome.SUCCESS
    assert result.warning is not None
    assert "#42" in result.warning
    assert "was not stored again" in result.warning
    assert (
        exit_code_for_outcome(result.outcome, result.warning)
        is ExitCode.UPLOADED_WITH_WARNING
    )
    assert job_label(JobState.DONE, result.warning) == WARNED_UPLOAD_LABEL
    failed_dir = settings.output.failed_dir
    assert not failed_dir.exists() or list(failed_dir.iterdir()) == []


def test_a_restart_while_uploading_is_stored_amber() -> None:
    """
    A job the server lost while uploading may be in paperless-ngx.

    Startup recovery fails the row with the uploading text and the amber
    category, so it exits 9, is labelled as maybe delivered, and its next
    step sends the operator to the document list rather than the scanner.
    """
    store = JobStore()
    try:
        job = store.create_job("default", _TITLE)
        store.update_state(job.id, JobState.UPLOADING)

        assert store.fail_active_jobs() == 1

        failed = store.get_job(job.id)
    finally:
        store.close()

    assert failed is not None
    assert failed.state is JobState.ERROR
    assert failed.error == RESTART_UPLOADING_REASON
    category = failed.error_category
    assert category is ErrorCategory.UNCONFIRMED_SEND
    assert exit_code_for(category) is ExitCode.UNCONFIRMED
    assert job_label(failed.state, failed.warning, category) == UNCONFIRMED_SEND_LABEL
    next_step = error_next_step(category)
    assert _CHECK_FIRST in next_step
    assert _BLIND_RESCAN not in next_step
