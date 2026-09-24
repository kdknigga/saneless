"""
The golden end-to-end test: the right document, delivered once, reported truly.

Every other end-to-end test checks that a job *finished*.  This one checks what
was delivered and what the operator was told about it:

- the right pages, byte for byte the ones the scanner spooled;
- in the right order, including a manual-duplex stack fed front-first and then
  flipped;
- carrying the operator's title, tags and correspondent, and nothing else;
- uploaded exactly once, and polled once for the task paperless-ngx issued;
- and reported truthfully, on the web page and on the command line, when the
  run was clean, when it was delivered with a warning, and when it had to fall
  back to the consume folder.

Nothing is stubbed between the form (or the command line) and the HTTP request
except the scanner hardware and paperless-ngx itself: the real app runs under
``TestClient``, the real ``saneless scan`` runs under ``CliRunner``, and a real
``PaperlessClient`` sends real multipart requests to an in-memory
paperless-ngx.

The harness lives in ``tests.golden_support`` so later end-to-end work can
reuse it.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from saneless.cli import cli
from saneless.config import (
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
)
from saneless.vocabulary import TERMINAL_STATES, JobState
from saneless.web.app import create_app
from tests.conftest import wait_for_state
from tests.golden_support import (
    DistinctPageScanner,
    RecordingPaperless,
    cli_client_builder,
    embedded_streams,
    png_idat,
    web_client_builder,
)

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import Result

    from saneless.job import Job, JobStore

_TITLE = "Quarterly Report"
_TAGS = ["3", "7"]
_CORRESPONDENT = "12"

# Exactly these parts, and each name as often as listed: a date field, a
# ``tags[]`` spelling or a dropped tag all change this multiset.
_EXPECTED_FIELD_NAMES = sorted(["title", "correspondent", "tags", "tags", "document"])

# Two scanning profiles plus the "default" the settings require.  A profile set
# of exactly one untouched "default" would make the worker generate profiles
# from the scanner at startup and swap them in under the test.
_SIMPLEX = "golden-simplex"
_DUPLEX = "golden-duplex"

# The pipeline's own sentence for one unreadable sheet, spelled out rather than
# imported: the test states what the operator must read.
_SKIPPED_SHEET = (
    "1 page(s) could not be read by the scanner and were skipped. "
    "They were not removed for being blank; rescan those sheets."
)

_WARNED_LINE = f"Uploaded with a warning: {_TITLE}"
_DONE_LINE = f"Done: {_TITLE}"
_FALLBACK_LINE = f"Saved to folder: {_TITLE}"
_NOT_UPLOADED_LINE = (
    "Not uploaded: saved to the consume folder without its title, tags or correspondent"
)
_CONSUME_FOLDER_SENTENCE = "Saved to the paperless-ngx consume directory"

# Generous next to a run that takes a fraction of a second, and far below
# pytest-timeout's ceiling, so a stuck run fails naming the job and its state.
_BUDGET = 10.0


@dataclass(frozen=True)
class _Scenario:
    """
    One scan: what the scanner feeds and what the document must turn out to be.

    Attributes:
        label: The parametrize id.
        profile: The profile the scan runs under.
        passes: The page indices each scanner pass feeds, in feed order.
        rejected: The sheets each pass reports having skipped.
        document_order: The spooled pages, in the order the PDF must hold them.
        warning: The warning the stored job must carry, or None.

    """

    label: str
    profile: str
    passes: tuple[tuple[int, ...], ...]
    rejected: tuple[int, ...]
    document_order: tuple[int, ...]
    warning: str | None

    @property
    def flips(self) -> bool:
        """Whether the run waits for the operator to flip the stack."""
        return len(self.passes) == 2


_SIMPLEX_RUN = _Scenario(
    label="simplex",
    profile=_SIMPLEX,
    passes=((0, 1, 2),),
    rejected=(),
    document_order=(0, 1, 2),
    warning=None,
)
# Fronts first; then the operator flips the stack face-down, so the last
# front's back feeds first.  Document order is the interleave, 0 through 5.
_DUPLEX_RUN = _Scenario(
    label="manual_duplex",
    profile=_DUPLEX,
    passes=((0, 2, 4), (5, 3, 1)),
    rejected=(),
    document_order=(0, 1, 2, 3, 4, 5),
    warning=None,
)
# Delivered, but one sheet in the stack could not be read.
_WARNED_RUN = _Scenario(
    label="warned",
    profile=_SIMPLEX,
    passes=((0, 1, 2),),
    rejected=(1,),
    document_order=(0, 1, 2),
    warning=_SKIPPED_SHEET,
)

_WEB_SCENARIOS = (_SIMPLEX_RUN, _DUPLEX_RUN, _WARNED_RUN)
_CLI_SCENARIOS = (_SIMPLEX_RUN, _DUPLEX_RUN)


def _settings(
    tmp_path: Path, *, profile_metadata: bool, consume_dir: Path | None = None
) -> Settings:
    """
    Build settings whose every path is under ``tmp_path``.

    Args:
        tmp_path: pytest's per-test directory.
        profile_metadata: Give both scanning profiles the golden tags and
            correspondent.  The command line has no tag or correspondent
            option, so the CLI takes them from here; the web scan leaves the
            profiles bare, so only the form can supply them.
        consume_dir: The paperless-ngx consume folder, if the run has one.

    Returns:
        Settings with a simplex profile and a manual-duplex feeder profile.

    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, object] = (
        {"default_tags": [3, 7], "default_correspondent": 12}
        if profile_metadata
        else {}
    )
    return Settings(
        scanner=ScannerConfig(device="test:device:001"),
        paperless=PaperlessConfig(
            url="http://paperless.invalid:8000",
            token="golden-token",
            consume_dir=consume_dir,
        ),
        output=OutputConfig(
            tmp_dir=str(tmp_path / "scratch"),
            data_dir=str(data_dir),
            log_file=str(tmp_path / "logs" / "saneless.log"),
        ),
        profiles={
            "default": ProfileConfig(),
            _SIMPLEX: ProfileConfig.model_validate({"source": "Flatbed", **metadata}),
            _DUPLEX: ProfileConfig.model_validate(
                {"source": "ADF", "duplex": "manual", **metadata}
            ),
        },
    )


def _values(fields: list[tuple[str, str | bytes]], name: str) -> list[str | bytes]:
    """
    Return every value one form field carried, in wire order.

    Args:
        fields: An upload's parsed form fields.
        name: The field to select.

    Returns:
        That field's values, one per part.

    """
    return [value for field, value in fields if field == name]


def _assert_operator_metadata(
    fields: list[tuple[str, str | bytes]], title: str = _TITLE
) -> None:
    """
    Assert an upload carried exactly the operator's metadata and a document.

    Args:
        fields: The upload's parsed form fields.
        title: The title this upload must carry.

    """
    assert sorted(name for name, _ in fields) == _EXPECTED_FIELD_NAMES
    assert _values(fields, "title") == [title]
    assert _values(fields, "tags") == _TAGS
    assert _values(fields, "correspondent") == [_CORRESPONDENT]


def _task_ids(recorder: RecordingPaperless) -> list[str]:
    """
    Return the task id each poll asked about, in the order it asked.

    Args:
        recorder: The in-memory paperless-ngx.

    Returns:
        One id per ``GET /api/tasks/`` request.

    """
    return [str(poll.url.params.get("task_id", "")) for poll in recorder.polls()]


# --------------------------------------------------------------------------
# The web: the real app, a real client, the in-memory paperless-ngx.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _WebRun:
    """
    Everything one web scan left behind.

    Attributes:
        recorder: The in-memory paperless-ngx, with every request it was sent.
        scanner: The scanner, with a copy of every page it spooled.
        job: The stored job row once it reached a terminal state.
        status_html: The followed job's status partial, entity-decoded.
        index_html: The index page, entity-decoded.
        history_html: The job history rows, entity-decoded.
        scratch: Whatever was left in ``tmp_dir`` after the app stopped.

    """

    recorder: RecordingPaperless
    scanner: DistinctPageScanner
    job: Job
    status_html: str
    index_html: str
    history_html: str
    scratch: list[Path]


def _run_web(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: _Scenario
) -> _WebRun:
    """
    Scan once through the web app, exactly as a browser would.

    Args:
        tmp_path: pytest's per-test directory.
        monkeypatch: Swaps the app's ``PaperlessClient`` for the recording one.
        scenario: What the scanner feeds.

    Returns:
        What the run left behind.

    """
    settings = _settings(tmp_path, profile_metadata=False)
    recorder = RecordingPaperless()
    scanner = DistinctPageScanner(passes=scenario.passes, rejected=scenario.rejected)
    monkeypatch.setattr(
        "saneless.web.app.PaperlessClient", web_client_builder(recorder)
    )
    app = create_app(settings, scanner)
    with TestClient(app) as client:
        submitted = client.post(
            "/api/scan",
            data={
                "profile": scenario.profile,
                "title": _TITLE,
                "tags": _TAGS,
                "correspondent": _CORRESPONDENT,
            },
        )
        assert submitted.status_code == 200, submitted.text
        store: JobStore = app.state.job_store
        job_id = store.list_recent(limit=1)[0].id
        if scenario.flips:
            # The coordinator accepts an answer only once armed, and the worker
            # arms it as it records AWAITING_FLIP: a Continue sent earlier is
            # dropped, not queued.
            wait_for_state(store, job_id, JobState.AWAITING_FLIP, _BUDGET)
            flipped = client.post("/api/flip/continue", data={"job_id": job_id})
            assert flipped.status_code == 200, flipped.text
        job = wait_for_state(store, job_id, TERMINAL_STATES, _BUDGET)
        status_html = html.unescape(client.get(f"/api/jobs/{job_id}/status").text)
        index_html = html.unescape(client.get("/").text)
        history_html = html.unescape(client.get("/api/jobs/history").text)
    return _WebRun(
        recorder=recorder,
        scanner=scanner,
        job=job,
        status_html=status_html,
        index_html=index_html,
        history_html=history_html,
        scratch=sorted(settings.output.tmp_dir.iterdir()),
    )


@pytest.mark.parametrize(
    "scenario", _WEB_SCENARIOS, ids=[scenario.label for scenario in _WEB_SCENARIOS]
)
def test_web_upload_carries_the_operators_metadata_once(
    scenario: _Scenario, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One upload, carrying the title, tags and correspondent typed in the form."""
    run = _run_web(tmp_path, monkeypatch, scenario)

    assert len(run.recorder.uploads()) == 1
    _assert_operator_metadata(run.recorder.upload_fields(0))


@pytest.mark.parametrize(
    "scenario", _WEB_SCENARIOS, ids=[scenario.label for scenario in _WEB_SCENARIOS]
)
def test_web_pages_arrive_in_document_order(
    scenario: _Scenario, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every uploaded page is a spooled page, byte for byte, in document order."""
    run = _run_web(tmp_path, monkeypatch, scenario)

    assert len(run.recorder.uploads()) == 1
    assert embedded_streams(run.recorder.document(0)) == [
        png_idat(run.scanner.spooled[index]) for index in scenario.document_order
    ]


@pytest.mark.parametrize(
    "scenario", _WEB_SCENARIOS, ids=[scenario.label for scenario in _WEB_SCENARIOS]
)
def test_web_polls_once_and_stores_the_outcome(
    scenario: _Scenario, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One poll for the issued task, a truthful row, and no scratch left over."""
    run = _run_web(tmp_path, monkeypatch, scenario)

    assert run.recorder.issued == ["golden-task-1"]
    assert _task_ids(run.recorder) == run.recorder.issued
    assert run.job.state is JobState.DONE
    assert run.job.pages_uploaded == len(scenario.document_order)
    assert run.job.warning == scenario.warning
    assert run.scratch == []


@pytest.mark.parametrize(
    "scenario", _WEB_SCENARIOS, ids=[scenario.label for scenario in _WEB_SCENARIOS]
)
def test_web_status_renders_the_outcome_and_warning(
    scenario: _Scenario, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean run reads Done; a warned one says so, and says why, everywhere."""
    run = _run_web(tmp_path, monkeypatch, scenario)

    if scenario.warning is None:
        for page in (run.status_html, run.index_html):
            assert _DONE_LINE in page
        assert re.search(
            r'<td class="status-done">\s*Complete\s*</td>', run.history_html
        )
        return
    for page in (run.status_html, run.index_html):
        assert _WARNED_LINE in page
        assert scenario.warning in page
        assert _DONE_LINE not in page
    assert re.search(
        r'<td class="status-fallback">\s*Uploaded with a warning\s*</td>',
        run.history_html,
    )


# --------------------------------------------------------------------------
# The command line: the real `saneless scan`, the same in-memory paperless-ngx.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _CliRun:
    """
    Everything one ``saneless scan`` left behind.

    Attributes:
        recorder: The in-memory paperless-ngx, with every request it was sent.
        scanner: The scanner, with a copy of every page it spooled.
        result: The command's exit code, stdout and stderr.
        scratch: Whatever was left in ``tmp_dir`` after the command returned.
        consumed: The PDFs in the consume folder, if the run had one.

    """

    recorder: RecordingPaperless
    scanner: DistinctPageScanner
    result: Result
    scratch: list[Path]
    consumed: list[Path]


def _run_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: _Scenario,
    *,
    refuse_uploads: bool = False,
) -> _CliRun:
    """
    Run ``saneless scan --title "Quarterly Report"`` against the golden fakes.

    Only what a test cannot have is replaced: the configuration file, the
    logging setup, the python-sane import check, the scanner hardware and
    paperless-ngx.  The command, the pipeline and the client are the real ones.

    Args:
        tmp_path: pytest's per-test directory.
        monkeypatch: Replaces those five names in ``saneless.cli``.
        scenario: What the scanner feeds.
        refuse_uploads: Answer every upload with a 500 and give the run a
            consume folder, so it falls back to it.

    Returns:
        What the run left behind.

    """
    consume_dir = tmp_path / "consume" if refuse_uploads else None
    if consume_dir is not None:
        consume_dir.mkdir()
    settings = _settings(tmp_path, profile_metadata=True, consume_dir=consume_dir)
    recorder = RecordingPaperless(refuse_uploads=refuse_uploads)
    scanner = DistinctPageScanner(passes=scenario.passes, rejected=scenario.rejected)

    def load_settings(config_path: str | None = None) -> Settings:
        """Return the golden settings whatever ``--config`` says."""
        return settings

    def configure_logging(*_args: object, **_kwargs: object) -> bool:
        """Attach no handler; report that no log file was attached."""
        return False

    def require_sane() -> None:
        """Skip the python-sane import check; no hardware is involved."""

    def sane_backend(host: str = "") -> DistinctPageScanner:
        """Hand the command the prepared scanner."""
        return scanner

    monkeypatch.setattr("saneless.cli.load_settings", load_settings)
    monkeypatch.setattr("saneless.cli.configure_logging", configure_logging)
    monkeypatch.setattr("saneless.cli.require_sane", require_sane)
    monkeypatch.setattr("saneless.cli.SaneBackend", sane_backend)
    monkeypatch.setattr("saneless.cli.PaperlessClient", cli_client_builder(recorder))
    if scenario.flips:
        # CliRunner is not a terminal; the flip prompt needs one, and "y" on
        # stdin is the operator confirming the stack is flipped.
        monkeypatch.setattr("saneless.cli._stdin_is_interactive", lambda: True)

    result = CliRunner().invoke(
        cli,
        ["scan", "--profile", scenario.profile, "--title", _TITLE],
        input="y\n" if scenario.flips else None,
    )
    consumed = sorted(consume_dir.glob("*.pdf")) if consume_dir is not None else []
    return _CliRun(
        recorder=recorder,
        scanner=scanner,
        result=result,
        scratch=sorted(settings.output.tmp_dir.iterdir()),
        consumed=consumed,
    )


def _warning_lines(stderr: str) -> list[str]:
    """
    Return the stderr lines that announce a warning.

    Args:
        stderr: The command's standard error.

    Returns:
        Every line starting ``Warning: ``.

    """
    return [line for line in stderr.splitlines() if line.startswith("Warning: ")]


@pytest.mark.parametrize(
    "scenario", _CLI_SCENARIOS, ids=[scenario.label for scenario in _CLI_SCENARIOS]
)
def test_cli_upload_carries_the_profile_metadata_once(
    scenario: _Scenario, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One upload, carrying the typed title and the profile's tags and correspondent."""
    run = _run_cli(tmp_path, monkeypatch, scenario)

    assert len(run.recorder.uploads()) == 1
    _assert_operator_metadata(run.recorder.upload_fields(0))


@pytest.mark.parametrize(
    "scenario", _CLI_SCENARIOS, ids=[scenario.label for scenario in _CLI_SCENARIOS]
)
def test_cli_pages_arrive_in_document_order(
    scenario: _Scenario, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every uploaded page is a spooled page, byte for byte, in document order."""
    run = _run_cli(tmp_path, monkeypatch, scenario)

    assert len(run.recorder.uploads()) == 1
    assert embedded_streams(run.recorder.document(0)) == [
        png_idat(run.scanner.spooled[index]) for index in scenario.document_order
    ]


@pytest.mark.parametrize(
    "scenario", _CLI_SCENARIOS, ids=[scenario.label for scenario in _CLI_SCENARIOS]
)
def test_cli_clean_scan_prints_done_and_exits_zero(
    scenario: _Scenario, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean run says Done, warns about nothing, exits 0 and leaves no scratch."""
    run = _run_cli(tmp_path, monkeypatch, scenario)

    assert run.result.exit_code == 0, run.result.output
    assert _DONE_LINE in run.result.stdout
    assert _warning_lines(run.result.stderr) == []
    assert run.recorder.issued == ["golden-task-1"]
    assert _task_ids(run.recorder) == run.recorder.issued
    assert run.scratch == []


def test_cli_skipped_sheet_is_reported_and_exits_7(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delivered scan with an unreadable sheet is not reported as Done."""
    run = _run_cli(tmp_path, monkeypatch, _WARNED_RUN)

    assert run.result.exit_code == 7, run.result.output
    assert _WARNED_LINE in run.result.stdout
    assert "Done:" not in run.result.stdout
    assert _warning_lines(run.result.stderr) == [f"Warning: {_SKIPPED_SHEET}"]
    assert len(run.recorder.uploads()) == 1


def test_cli_duplex_count_mismatch_is_reported_and_exits_7(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unequal passes are delivered as two halves and reported as a warning."""
    mismatch = _Scenario(
        label="mismatch",
        profile=_DUPLEX,
        passes=((0, 2, 4), (3, 1)),
        rejected=(),
        document_order=(),
        warning=None,
    )

    run = _run_cli(tmp_path, monkeypatch, mismatch)

    assert run.result.exit_code == 7, run.result.output
    assert len(run.recorder.uploads()) == 2
    _assert_operator_metadata(run.recorder.upload_fields(0), f"{_TITLE} (fronts)")
    _assert_operator_metadata(run.recorder.upload_fields(1), f"{_TITLE} (backs)")
    assert _WARNED_LINE in run.result.stdout
    assert "Done:" not in run.result.stdout
    assert any(
        line.startswith("Warning: Page count mismatch")
        for line in _warning_lines(run.result.stderr)
    )


def test_cli_consume_folder_fallback_is_reported_and_exits_6(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scan saved to the consume folder says so, and says what it lost."""
    run = _run_cli(tmp_path, monkeypatch, _SIMPLEX_RUN, refuse_uploads=True)

    assert run.result.exit_code == 6, run.result.output
    assert _FALLBACK_LINE in run.result.stdout
    assert "Done:" not in run.result.stdout
    assert _NOT_UPLOADED_LINE in run.result.stderr.splitlines()
    warnings = _warning_lines(run.result.stderr)
    assert len(warnings) == 1
    assert _CONSUME_FOLDER_SENTENCE in warnings[0]
    assert len(run.consumed) == 1


def test_cli_fallback_with_a_skipped_sheet_exits_6(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback outranks the warning, and neither sentence is dropped."""
    run = _run_cli(tmp_path, monkeypatch, _WARNED_RUN, refuse_uploads=True)

    assert run.result.exit_code == 6, run.result.output
    assert _FALLBACK_LINE in run.result.stdout
    assert "Done:" not in run.result.stdout
    assert _NOT_UPLOADED_LINE in run.result.stderr.splitlines()
    warned = "\n".join(_warning_lines(run.result.stderr))
    assert _CONSUME_FOLDER_SENTENCE in warned
    assert _SKIPPED_SHEET in warned
    assert len(run.consumed) == 1
