"""
A scan delivers the right document once and reports the outcome truly.

A job that *finished* is not a job that delivered.  These tests check what
was delivered and what the operator was told about it:

- the right pages, byte for byte the ones the scanner spooled;
- in the right order, including a manual-duplex stack fed front-first and then
  flipped, and a multi-page document grown one flatbed pass at a time;
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

The harness lives in ``tests.golden_support``, shared with the other
end-to-end tests.
"""

from __future__ import annotations

import dataclasses
import html
import re
import threading
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import TYPE_CHECKING, NoReturn

import httpx2
import pikepdf
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from PIL import Image

from saneless.cli import cli
from saneless.config import (
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
)
from saneless.exceptions import PaperlessUncertainSendError
from saneless.paperless import PaperlessClient, PaperlessTiming
from saneless.scanner.base import PassCapReached
from saneless.vocabulary import (
    TERMINAL_STATES,
    ErrorCategory,
    JobState,
    PassAnswer,
    PassWait,
    ScanOutcome,
    pass_wait_state,
)
from saneless.web.app import create_app
from tests.conftest import load_the_lists, poll_until, services_of, wait_for_state
from tests.golden_support import (
    DOCUMENTS_PATH,
    GOLDEN_CORRESPONDENT_IDS,
    GOLDEN_TAG_IDS,
    DistinctPageScanner,
    RecordingPaperless,
    UploadFailure,
    cli_client_builder,
    embedded_streams,
    loopback_paperless,
    png_idat,
    web_client_builder,
)
from tests.prompt_support import readable

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from click.testing import Result

    from saneless.job import Job, JobStore
    from saneless.scanner.base import PageSink, ScanBatch, ScanSettings
    from saneless.vocabulary import PassPrompt
    from saneless.worker import ScanWorker

_TITLE = "Quarterly Report"
# The ids the recording paperless-ngx lists, so the submitted metadata exists.
_TAGS = [str(tag) for tag in GOLDEN_TAG_IDS]
_CORRESPONDENT = str(GOLDEN_CORRESPONDENT_IDS[0])

# Exactly these parts, and each name as often as listed: a date field, a
# ``tags[]`` spelling or a dropped tag all change this multiset.
_EXPECTED_FIELD_NAMES = sorted(["title", "correspondent", "tags", "tags", "document"])

# Two scanning profiles plus the "default" the settings require.  A profile set
# of exactly one untouched "default" would make the worker generate profiles
# from the scanner at startup and swap them in under the test.  ``default``
# scans as the simplex profile does: it is the one ``saneless scan`` uses when
# no ``--profile`` is named, and the one the web page opens on.
_DEFAULT = "default"
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

# The document the recording paperless-ngx says it already holds, and the
# warning a scan refused as its duplicate must end with, spelled out rather
# than imported: the test states what the operator must read.
_EXISTING_DOCUMENT = 42
_DUPLICATE_SENTENCE = (
    f"paperless-ngx already holds this file as document #{_EXISTING_DOCUMENT}; "
    "it was not stored again, and this scan's title and tags were not applied "
    "to it."
)

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
        answers: For a multi-page scan, the operator's answer to each
            between-pass question, in order; empty for any other scan.

    """

    label: str
    profile: str
    passes: tuple[tuple[int, ...], ...]
    rejected: tuple[int, ...]
    document_order: tuple[int, ...]
    warning: str | None
    answers: tuple[PassAnswer, ...] = ()

    @property
    def multi_page(self) -> bool:
        """Whether the scan grows its document pass by pass."""
        return bool(self.answers)

    @property
    def flips(self) -> bool:
        """Whether the run waits for the operator to flip the stack."""
        return len(self.passes) == 2 and not self.multi_page


_SIMPLEX_RUN = _Scenario(
    label="simplex",
    profile=_SIMPLEX,
    passes=((0, 1, 2),),
    rejected=(),
    document_order=(0, 1, 2),
    warning=None,
)
# The same simplex scan under ``default``, the profile no one has to name.
_DEFAULT_RUN = _Scenario(
    label="default",
    profile=_DEFAULT,
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

# Four flatbed passes of one sheet each: "Scan next" three times, then
# "Finish".  Document order is scan order.
_MULTI_PAGE_RUN = _Scenario(
    label="multi_page",
    profile=_SIMPLEX,
    passes=((0,), (1,), (2,), (3,)),
    rejected=(),
    document_order=(0, 1, 2, 3),
    warning=None,
    answers=(PassAnswer.NEXT, PassAnswer.NEXT, PassAnswer.NEXT, PassAnswer.FINISH),
)

# The letter the terminal prompt takes for each answer a scenario scripts.
_CLI_LETTERS = {PassAnswer.NEXT: "n", PassAnswer.FINISH: "f"}

_WEB_SCENARIOS = (_SIMPLEX_RUN, _DUPLEX_RUN, _WARNED_RUN)
_CLI_SCENARIOS = (_SIMPLEX_RUN, _DUPLEX_RUN)


def _settings(
    tmp_path: Path, *, profile_metadata: bool, consume_dir: Path | None = None
) -> Settings:
    """
    Build settings whose every path is under ``tmp_path``.

    Args:
        tmp_path: pytest's per-test directory.
        profile_metadata: Give ``default`` and both scanning profiles the
            golden tags and correspondent.  The command line has no tag or
            correspondent option, so the CLI takes them from here; the web
            scan leaves the profiles bare, so only the form can supply them.
        consume_dir: The paperless-ngx consume folder, if the run has one.

    Returns:
        Settings with a simplex profile, a manual-duplex feeder profile, and a
        ``default`` that scans as the simplex one does.

    """
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, object] = (
        {
            "default_tags": list(GOLDEN_TAG_IDS),
            "default_correspondent": GOLDEN_CORRESPONDENT_IDS[0],
        }
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
            _SIMPLEX: ProfileConfig.model_validate({"source": "Flatbed", **metadata}),
            _DUPLEX: ProfileConfig.model_validate(
                {"source": "ADF", "duplex": "manual", **metadata}
            ),
            # Listed last, so a page that opened on its first option would
            # submit the simplex profile rather than this one.
            _DEFAULT: ProfileConfig.model_validate({"source": "Flatbed", **metadata}),
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
        prompts: The multi-page questions the operator answered, in order.

    """

    recorder: RecordingPaperless
    scanner: DistinctPageScanner
    job: Job
    status_html: str
    index_html: str
    history_html: str
    scratch: list[Path]
    prompts: tuple[PassPrompt, ...]


def _answer_passes(
    worker: ScanWorker,
    store: JobStore,
    job_id: str,
    answers: tuple[PassAnswer, ...],
) -> tuple[PassPrompt, ...]:
    """
    Answer a multi-page job's questions through the real worker, one at a time.

    Each answer waits for a question newer than the last one answered: the
    worker publishes a fresh prompt per wait, and an answer naming any other
    prompt is dropped, not queued.  While the question is open, the stored row
    must already say what the job is waiting on.

    Args:
        worker: The app's worker, running the job.
        store: The app's job store.
        job_id: The multi-page job.
        answers: The operator's answers, in order.

    Returns:
        The questions answered, in the order they were asked.

    """
    asked: list[PassPrompt] = []
    for answer in answers:
        answered = asked[-1].number if asked else 0

        def newer_prompt(after: int = answered) -> bool:
            """Report whether a question newer than ``after`` is open."""
            prompt = worker.pass_prompt(job_id)
            return prompt is not None and prompt.number > after

        assert poll_until(newer_prompt, _BUDGET), f"no question after {answered}"
        prompt = worker.pass_prompt(job_id)
        assert prompt is not None
        row = store.get_job(job_id)
        assert row is not None
        assert row.state is pass_wait_state(prompt.wait)
        assert worker.answer_pass(job_id, prompt.number, answer)
        asked.append(prompt)
    return tuple(asked)


def _run_web(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: _Scenario,
    *,
    recorder: RecordingPaperless | None = None,
    tags: list[str] | None = None,
) -> _WebRun:
    """
    Scan once through the web app, exactly as a browser would.

    Args:
        tmp_path: pytest's per-test directory.
        monkeypatch: Swaps the app's ``PaperlessClient`` for the recording one.
        scenario: What the scanner feeds.
        recorder: The in-memory paperless-ngx; None is one that files every
            upload.  One whose uploads fail also gives the run a consume
            folder.  Only a before-send failure -- the request never reached
            paperless-ngx -- may fall back to the folder.
        tags: The tag ids the form ticks; None ticks the golden ones.

    Returns:
        What the run left behind.

    """
    if recorder is None:
        recorder = RecordingPaperless()
    failing = recorder.upload_failure is not None
    consume_dir = tmp_path / "consume" if failing else None
    if consume_dir is not None:
        consume_dir.mkdir()
    settings = _settings(tmp_path, profile_metadata=False, consume_dir=consume_dir)
    scanner = DistinctPageScanner(passes=scenario.passes, rejected=scenario.rejected)
    monkeypatch.setattr(
        "saneless.web.app.PaperlessClient", web_client_builder(recorder)
    )
    app = create_app(settings, scanner)
    form: dict[str, str | list[str]] = {
        "profile": scenario.profile,
        "title": _TITLE,
        "tags": _TAGS if tags is None else tags,
        "correspondent": _CORRESPONDENT,
    }
    if scenario.multi_page:
        # The ticked "Multiple pages" checkbox, as a browser posts it.
        form["multi_page"] = "on"
    with TestClient(app) as client:
        submitted = client.post("/api/scan", data=form)
        assert submitted.status_code == 200, submitted.text
        store: JobStore = services_of(app).job_store
        job_id = store.list_recent(limit=1)[0].id
        prompts = _answer_passes(
            services_of(app).worker, store, job_id, scenario.answers
        )
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
        prompts=prompts,
    )


@pytest.mark.parametrize(
    "scenario", _WEB_SCENARIOS, ids=[scenario.label for scenario in _WEB_SCENARIOS]
)
def test_web_upload_carries_the_operators_metadata_once(
    scenario: _Scenario, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A web scan uploads once, carrying the title, tags and correspondent typed."""
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
    """A web scan polls its task once, stores a truthful row, leaves no scratch."""
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


def test_web_consume_folder_fallback_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scan saved to the consume folder says so on every web surface."""
    run = _run_web(
        tmp_path,
        monkeypatch,
        _SIMPLEX_RUN,
        recorder=RecordingPaperless(upload_failure=UploadFailure.BEFORE_SEND),
    )

    assert run.job.state is JobState.FALLBACK
    assert run.job.warning is not None
    assert _CONSUME_FOLDER_SENTENCE in run.job.warning
    assert len(run.recorder.uploads()) == 1
    assert run.recorder.issued == []
    assert run.recorder.polls() == []
    assert len(list((tmp_path / "consume").glob("*.pdf"))) == 1
    # The browser that submitted the scan is shown its warning with the host
    # folder named by the setting that holds it: no web page carries a host
    # path, and the full text stays in the log and in `saneless jobs`.
    consume = str(tmp_path / "consume")
    shown = run.job.warning.replace(consume, "<paperless.consume_dir>")
    assert shown != run.job.warning
    for page in (run.status_html, run.index_html):
        assert _FALLBACK_LINE in page
        assert shown in page
        assert consume not in page
        assert _DONE_LINE not in page
        assert _WARNED_LINE not in page
    assert re.search(
        r'<td class="status-fallback">\s*Saved to folder\s*</td>', run.history_html
    )
    assert "Complete" not in run.history_html


def test_web_after_send_failure_is_unconfirmed_sent_once_and_not_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A 500 on the upload ends the job as "may have reached paperless-ngx".

    paperless-ngx answered, so it may have read and stored the body: the scan
    is sent once, never copied to the consume folder that is configured, and
    kept in ``failed/`` for the operator to import only if it is missing.
    """
    run = _run_web(
        tmp_path,
        monkeypatch,
        _SIMPLEX_RUN,
        recorder=RecordingPaperless(upload_failure=UploadFailure.AFTER_SEND),
    )

    assert run.job.state is JobState.ERROR
    assert run.job.error_category is ErrorCategory.UNCONFIRMED_SEND
    assert len(run.recorder.uploads()) == 1
    assert run.recorder.polls() == []
    assert list((tmp_path / "consume").iterdir()) == []
    assert len(sorted((tmp_path / "data" / "failed").glob("*.pdf"))) == 1


def test_web_duplicate_is_done_with_a_warning_and_nothing_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A scan paperless-ngx refuses as a duplicate ends delivered, not failed.

    paperless-ngx already holds the file, so the job is DONE with the warning
    naming the document, amber on every surface, and nothing goes to
    ``failed/``: there is nothing to import.
    """
    run = _run_web(
        tmp_path,
        monkeypatch,
        _SIMPLEX_RUN,
        recorder=RecordingPaperless(duplicate_of=_EXISTING_DOCUMENT),
    )

    assert run.job.state is JobState.DONE
    assert run.job.outcome is ScanOutcome.SUCCESS
    assert run.job.error is None
    assert run.job.warning == _DUPLICATE_SENTENCE
    assert len(run.recorder.uploads()) == 1
    assert _task_ids(run.recorder) == ["golden-task-1"]
    assert not (tmp_path / "data" / "failed").exists()
    for page in (run.status_html, run.index_html):
        assert _WARNED_LINE in page
        assert _DUPLICATE_SENTENCE in page
        assert _DONE_LINE not in page
    assert re.search(
        r'<td class="status-fallback">\s*Uploaded with a warning\s*</td>',
        run.history_html,
    )


def test_multi_page_web_job_uploads_every_pass_in_scan_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Four flatbed passes answered through the worker become one four-page PDF.

    The form ticks "Multiple pages"; the real worker asks after every pass and
    is told "Scan next" three times and then "Finish".  Every page of the one
    upload is the page that pass spooled, byte for byte, in the order the
    passes ran, and a finish the operator pressed is a plain Done.
    """
    run = _run_web(tmp_path, monkeypatch, _MULTI_PAGE_RUN)

    assert [prompt.wait for prompt in run.prompts] == [PassWait.NEXT_PASS] * 4
    assert [prompt.pages_kept for prompt in run.prompts] == [1, 2, 3, 4]
    assert run.scanner.calls == 4
    assert len(run.recorder.uploads()) == 1
    _assert_operator_metadata(run.recorder.upload_fields(0))
    assert embedded_streams(run.recorder.document(0)) == [
        png_idat(run.scanner.spooled[index]) for index in _MULTI_PAGE_RUN.document_order
    ]
    assert run.job.state is JobState.DONE
    assert run.job.warning is None
    assert (
        run.job.pages_scanned,
        run.job.pages_removed,
        run.job.pages_uploaded,
    ) == (4, 0, 4)
    assert run.recorder.issued == ["golden-task-1"]
    assert run.scratch == []
    for page in (run.status_html, run.index_html):
        assert _DONE_LINE in page


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
    recorder: RecordingPaperless | None = None,
    profile_fields: Mapping[str, object] | None = None,
) -> _CliRun:
    """
    Run ``saneless scan --title "Quarterly Report"`` against the golden fakes.

    The scenario's profile is named with ``--profile``, except ``default``:
    a scenario on ``default`` names none, which is how the command reaches it.

    Only what a test cannot have is replaced: the configuration file, the
    logging setup, the python-sane import check, the scanner hardware and
    paperless-ngx.  The command, the pipeline and the client are the real ones.

    Args:
        tmp_path: pytest's per-test directory.
        monkeypatch: Replaces those five names in ``saneless.cli``.
        scenario: What the scanner feeds.
        recorder: The in-memory paperless-ngx; None is one that files every
            upload.  One whose uploads fail also gives the run a consume
            folder.  Only a before-send failure -- the request never reached
            paperless-ngx -- may fall back to the folder.
        profile_fields: Settings to change on the scenario's profile, by
            field name, such as a blank-page threshold other than the
            shipped default or other default tags.

    Returns:
        What the run left behind.

    """
    if recorder is None:
        recorder = RecordingPaperless()
    failing = recorder.upload_failure is not None
    consume_dir = tmp_path / "consume" if failing else None
    if consume_dir is not None:
        consume_dir.mkdir()
    settings = _settings(tmp_path, profile_metadata=True, consume_dir=consume_dir)
    profile = settings.profiles[scenario.profile]
    for name, value in (profile_fields or {}).items():
        setattr(profile, name, value)
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
    named = [] if scenario.profile == _DEFAULT else ["--profile", scenario.profile]
    args = ["scan", *named, "--title", _TITLE]
    typed: str | None = None
    if scenario.flips:
        # "y" on stdin is the operator confirming the stack is flipped.
        typed = "y\n"
    if scenario.multi_page:
        # One letter per between-pass question, as the operator types them.
        args.append("--multi-page")
        typed = "".join(f"{_CLI_LETTERS[answer]}\n" for answer in scenario.answers)
    if typed is not None:
        # CliRunner is not a terminal, and both prompts need one; its stdin
        # has no descriptor to wait on either, so the wait reports it readable.
        monkeypatch.setattr("saneless.cli._stdin_is_interactive", lambda: True)
        readable(monkeypatch)

    result = CliRunner().invoke(cli, args, input=typed)
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
    """A CLI scan uploads once, with the typed title and the profile's metadata."""
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
    assert "Warning:" not in run.result.stdout
    assert _warning_lines(run.result.stderr) == []
    assert run.recorder.issued == ["golden-task-1"]
    assert _task_ids(run.recorder) == run.recorder.issued
    assert run.scratch == []


def test_multi_page_cli_scan_uploads_every_pass_in_scan_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    ``saneless scan --multi-page`` answered n, n, n, f uploads four pages in order.

    The same four flatbed passes as the web job, answered at the terminal.
    The one upload holds every pass's page in scan order, the run says Done,
    and it exits 0.
    """
    run = _run_cli(tmp_path, monkeypatch, _MULTI_PAGE_RUN)

    assert run.result.exit_code == 0, run.result.output
    assert run.scanner.calls == 4
    assert len(run.recorder.uploads()) == 1
    _assert_operator_metadata(run.recorder.upload_fields(0))
    assert embedded_streams(run.recorder.document(0)) == [
        png_idat(run.scanner.spooled[index]) for index in _MULTI_PAGE_RUN.document_order
    ]
    assert _DONE_LINE in run.result.stdout
    assert _warning_lines(run.result.stderr) == []
    assert run.recorder.issued == ["golden-task-1"]
    assert run.scratch == []


def test_cli_skipped_sheet_is_reported_and_exits_7(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delivered scan with an unreadable sheet is not reported as Done."""
    run = _run_cli(tmp_path, monkeypatch, _WARNED_RUN)

    assert run.result.exit_code == 7, run.result.output
    assert _WARNED_LINE in run.result.stdout
    assert "Done:" not in run.result.stdout
    assert "Warning:" not in run.result.stdout
    assert _warning_lines(run.result.stderr) == [f"Warning: {_SKIPPED_SHEET}"]
    assert len(run.recorder.uploads()) == 1


def test_cli_pass_cap_is_reported_and_exits_7(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scan that stopped at its per-pass cap uploads its pages, warned."""
    original = DistinctPageScanner.scan_pages

    def capped(
        self: DistinctPageScanner,
        device_id: str,
        settings: ScanSettings,
        sink: PageSink,
    ) -> ScanBatch:
        batch = original(self, device_id, settings, sink)
        return dataclasses.replace(
            batch, cap_reached=PassCapReached(500, 501, auto_source=False)
        )

    monkeypatch.setattr(DistinctPageScanner, "scan_pages", capped)

    run = _run_cli(tmp_path, monkeypatch, _SIMPLEX_RUN)

    assert run.result.exit_code == 7, run.result.output
    assert len(run.recorder.uploads()) == 1
    assert _WARNED_LINE in run.result.stdout
    assert "Done:" not in run.result.stdout
    (line,) = _warning_lines(run.result.stderr)
    assert "sheet 501 was fed but not kept" in line


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
    assert "Warning:" not in run.result.stdout
    assert any(
        line.startswith("Warning: Page count mismatch")
        for line in _warning_lines(run.result.stderr)
    )


def test_cli_duplicate_is_reported_and_exits_7(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A duplicate refusal is an upload with a warning: exit 7, nothing kept."""
    run = _run_cli(
        tmp_path,
        monkeypatch,
        _SIMPLEX_RUN,
        recorder=RecordingPaperless(duplicate_of=_EXISTING_DOCUMENT),
    )

    assert run.result.exit_code == 7, run.result.output
    assert _WARNED_LINE in run.result.stdout
    assert "Done:" not in run.result.stdout
    assert _warning_lines(run.result.stderr) == [f"Warning: {_DUPLICATE_SENTENCE}"]
    assert "Paperless error" not in run.result.stderr
    assert len(run.recorder.uploads()) == 1
    assert not (tmp_path / "data" / "failed").exists()
    assert run.scratch == []


def test_cli_consume_folder_fallback_is_reported_and_exits_6(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scan saved to the consume folder says so, and says what it lost."""
    run = _run_cli(
        tmp_path,
        monkeypatch,
        _SIMPLEX_RUN,
        recorder=RecordingPaperless(upload_failure=UploadFailure.BEFORE_SEND),
    )

    assert run.result.exit_code == 6, run.result.output
    assert _FALLBACK_LINE in run.result.stdout
    assert "Done:" not in run.result.stdout
    assert "Warning:" not in run.result.stdout
    assert "Not uploaded" not in run.result.stdout
    assert _NOT_UPLOADED_LINE in run.result.stderr.splitlines()
    warnings = _warning_lines(run.result.stderr)
    assert len(warnings) == 1
    assert _CONSUME_FOLDER_SENTENCE in warnings[0]
    assert len(run.consumed) == 1


def test_cli_fallback_with_a_skipped_sheet_exits_6(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback outranks the warning, and neither sentence is dropped."""
    run = _run_cli(
        tmp_path,
        monkeypatch,
        _WARNED_RUN,
        recorder=RecordingPaperless(upload_failure=UploadFailure.BEFORE_SEND),
    )

    assert run.result.exit_code == 6, run.result.output
    assert _FALLBACK_LINE in run.result.stdout
    assert "Done:" not in run.result.stdout
    assert "Warning:" not in run.result.stdout
    assert "Not uploaded" not in run.result.stdout
    assert _NOT_UPLOADED_LINE in run.result.stderr.splitlines()
    warned = "\n".join(_warning_lines(run.result.stderr))
    assert _CONSUME_FOLDER_SENTENCE in warned
    assert _SKIPPED_SHEET in warned
    assert len(run.consumed) == 1


def test_cli_upload_read_timeout_exits_9_sent_once_and_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An upload that is never answered in time exits 9, the "check first" code.

    The body went out, so paperless-ngx may hold the document: one upload, no
    copy in the consume folder that is configured, and the PDF in ``failed/``.
    """
    run = _run_cli(
        tmp_path,
        monkeypatch,
        _SIMPLEX_RUN,
        recorder=RecordingPaperless(upload_failure=UploadFailure.READ_TIMEOUT),
    )

    assert run.result.exit_code == 9, run.result.output
    errors = [
        line
        for line in run.result.stderr.splitlines()
        if line.startswith("Paperless error: ")
    ]
    assert len(errors) == 1
    assert "may have reached paperless-ngx" in errors[0]
    assert len(run.recorder.uploads()) == 1
    assert run.consumed == []
    assert len(sorted((tmp_path / "data" / "failed").glob("*.pdf"))) == 1


def test_upload_late_answer_is_sent_once_and_not_copied(tmp_path: Path) -> None:
    """
    A server that reads the whole upload and answers late gets it exactly once.

    Over a real socket: the loopback server reads every byte of the body, then
    holds its answer past the client's read timeout.  The document may be
    stored, so it is neither sent again nor copied to the consume folder, and
    the error says it may already be in paperless-ngx.
    """
    consume = tmp_path / "consume"
    consume.mkdir()
    pdf = tmp_path / "late.pdf"
    pdf.write_bytes(b"%PDF-1.4 answered too late")
    gate = threading.Event()
    with loopback_paperless(answer_gate=gate) as server:
        client = PaperlessClient(
            url=server.url,
            token="loopback-token",
            consume_dir=consume,
            timing=PaperlessTiming(
                upload_timeout=lambda _size: httpx2.Timeout(5.0, read=0.3)
            ),
        )
        try:
            with pytest.raises(PaperlessUncertainSendError) as exc_info:
                client.upload_document(pdf, title="Late")
        finally:
            client.close()
        hits = list(server.hits)

    assert [hit.path for hit in hits if hit.method == "POST"] == [DOCUMENTS_PATH]
    assert list(consume.iterdir()) == []
    assert str(exc_info.value).endswith("; it may have reached paperless-ngx")


def _blank_page(_index: int) -> Image.Image:
    """
    Stand in for ``distinct_page``: a sheet with nothing on it.

    Args:
        _index: Ignored; every blank sheet is the same.

    Returns:
        A white page the size of a distinct one.

    """
    return Image.new("RGB", (120, 160), "white")


def test_cli_all_blank_scan_keeps_a_pdf_and_exits_8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A scan judged all blank uploads nothing, keeps the pages as one PDF, exits 8.

    The scanner feeds three blank sheets.  Empty-page detection removes all
    of them, which is its own failure rather than a scanner fault: the advice
    is to tune the detection, and the unfiltered pages are in ``failed/``.
    """
    monkeypatch.setattr("tests.golden_support.distinct_page", _blank_page)

    run = _run_cli(tmp_path, monkeypatch, _SIMPLEX_RUN)

    assert run.result.exit_code == 8, run.result.output
    kept = sorted((tmp_path / "data" / "failed").glob("*.pdf"))
    assert len(kept) == 1
    lines = run.result.stderr.splitlines()
    assert lines[0].startswith("Empty-page detection: ")
    assert f"preserved at {kept[0]}" in lines[0]
    assert lines[1].startswith("Try: ")
    assert "empty_page_coverage_threshold" in lines[1]
    assert run.recorder.uploads() == []
    assert run.scratch == []


def test_cli_the_coverage_knob_decides_the_all_blank_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The profile's threshold is what judges the pages, end to end.

    The scanner feeds its ordinary inked pages, which the shipped default
    keeps.  With ``empty_page_coverage_threshold`` at the top of its range
    every page light enough to be paper is judged blank, so the same run
    fails as all-blank: nothing uploaded, the unfiltered pages kept as one
    PDF, and exit 8.
    """
    run = _run_cli(
        tmp_path,
        monkeypatch,
        _SIMPLEX_RUN,
        profile_fields={"empty_page_coverage_threshold": 100.0},
    )

    assert run.result.exit_code == 8, run.result.output
    kept = sorted((tmp_path / "data" / "failed").glob("*.pdf"))
    assert len(kept) == 1
    with pikepdf.open(kept[0]) as pdf:
        assert len(pdf.pages) == len(_SIMPLEX_RUN.document_order)
    assert "empty_page_coverage_threshold" in run.result.stderr
    assert run.recorder.uploads() == []
    assert run.scratch == []


def test_cli_coding_error_in_the_upload_exits_5_with_the_pdf_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A bug in saneless's own upload code is an unexpected error, not paperless-ngx's.

    The ``TypeError`` keeps its type all the way out, so the command exits 5
    and says ``Unexpected error (TypeError)``, and the assembled PDF is kept
    and named on that line.  Reported as a ``PaperlessError`` it would exit
    3, blaming a server that was never reached.
    """

    def broken_upload(*_args: object, **_kwargs: object) -> NoReturn:
        """Fail the way a call with the wrong arguments fails."""
        msg = "upload_document() got an unexpected keyword argument 'tittle'"
        raise TypeError(msg)

    monkeypatch.setattr(PaperlessClient, "upload_document", broken_upload)

    run = _run_cli(tmp_path, monkeypatch, _SIMPLEX_RUN)

    assert run.result.exit_code == 5, run.result.output
    kept = sorted((tmp_path / "data" / "failed").glob("*.pdf"))
    assert len(kept) == 1
    unexpected = [
        line
        for line in run.result.stderr.splitlines()
        if line.startswith("Unexpected error (TypeError): ")
    ]
    assert len(unexpected) == 1
    assert f"preserved at {kept[0]}" in unexpected[0]
    assert run.recorder.uploads() == []
    assert run.scratch == []


# --------------------------------------------------------------------------
# A tag paperless-ngx no longer has: dropped before scanning, on both surfaces.
# --------------------------------------------------------------------------

# Tag 9 is asked for and paperless-ngx lists only tag 3.  The sentence is
# spelled out rather than imported: the test states what the operator reads.
_STALE_TAG_IDS = (3, 9)
_STALE_TAG_SENTENCE = "tag 9 no longer exists in paperless-ngx and was not applied."


def test_cli_stale_tag_is_dropped_and_exits_7(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A profile tag paperless-ngx lost is skipped, named, and exits 7."""
    run = _run_cli(
        tmp_path,
        monkeypatch,
        _SIMPLEX_RUN,
        recorder=RecordingPaperless(tags=(3,)),
        profile_fields={"default_tags": list(_STALE_TAG_IDS)},
    )

    assert run.result.exit_code == 7, run.result.output
    assert _WARNED_LINE in run.result.stdout
    assert _warning_lines(run.result.stderr) == [f"Warning: {_STALE_TAG_SENTENCE}"]
    assert len(run.recorder.uploads()) == 1
    fields = run.recorder.upload_fields(0)
    assert _values(fields, "tags") == ["3"]
    assert _values(fields, "correspondent") == [_CORRESPONDENT]


def test_web_stale_tag_is_dropped_and_the_job_is_warned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale tag ticked in the form is dropped: DONE, warned, tag 3 only."""
    run = _run_web(
        tmp_path,
        monkeypatch,
        _SIMPLEX_RUN,
        recorder=RecordingPaperless(tags=(3,)),
        tags=[str(tag) for tag in _STALE_TAG_IDS],
    )

    assert run.job.state is JobState.DONE
    assert run.job.warning == _STALE_TAG_SENTENCE
    assert _WARNED_LINE in run.status_html
    assert len(run.recorder.uploads()) == 1
    fields = run.recorder.upload_fields(0)
    assert _values(fields, "tags") == ["3"]
    assert _values(fields, "correspondent") == [_CORRESPONDENT]


# --------------------------------------------------------------------------
# The twin: an untouched web form and ``saneless scan --profile`` agree.
# --------------------------------------------------------------------------


class _UntouchedForm(HTMLParser):
    """
    Read the metadata an untouched scan form submits, as a browser would.

    A ticked ``tags`` box submits its value.  A select submits the option
    marked ``selected`` (the last one, if several are), and the first option
    when none is marked.

    Attributes:
        tags: Every ticked ``tags`` value, in document order.

    """

    def __init__(self) -> None:
        """Start with nothing read."""
        super().__init__()
        self.tags: list[str] = []
        self._select: str | None = None
        self._first: dict[str, str] = {}
        self._selected: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """
        Note a ticked tag box, an open select, or one of its options.

        Args:
            tag: The element name.
            attrs: Its attributes; a bare attribute's value is None.

        """
        found = dict(attrs)
        if tag == "input" and found.get("name") == "tags" and "checked" in found:
            self.tags.append(found.get("value") or "")
        elif tag == "select":
            self._select = found.get("name")
        elif tag == "option" and self._select is not None:
            value = found.get("value") or ""
            self._first.setdefault(self._select, value)
            if "selected" in found:
                self._selected[self._select] = value

    def handle_endtag(self, tag: str) -> None:
        """
        Close the open select.

        Args:
            tag: The element name.

        """
        if tag == "select":
            self._select = None

    def choice(self, name: str) -> str:
        """
        Return the value a select submits untouched.

        Args:
            name: The select's ``name``.

        Returns:
            The selected option's value, or the first option's.

        """
        return self._selected.get(name, self._first[name])


def _untouched_web_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[RecordingPaperless, dict[str, str | list[str]]]:
    """
    Scan once through the web app, submitting the form exactly as it opened.

    The page opens on ``default``, which carries the golden defaults though it
    is not the only profile.  Only the title is typed; the profile, the ticked
    tags and the chosen correspondent are read from the rendered page and the
    lists its loader brings once it has rendered, in that order, as a browser
    shows them.

    Args:
        tmp_path: The directory this run keeps its files under.
        monkeypatch: Swaps the app's ``PaperlessClient`` for the recording one.

    Returns:
        The in-memory paperless-ngx, and the form that was posted.

    """
    recorder = RecordingPaperless()
    settings = _settings(tmp_path, profile_metadata=True)
    scanner = DistinctPageScanner(passes=_DEFAULT_RUN.passes, rejected=())
    monkeypatch.setattr(
        "saneless.web.app.PaperlessClient", web_client_builder(recorder)
    )
    app = create_app(settings, scanner)
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 200, page.text
        form_state = _UntouchedForm()
        form_state.feed(page.text)
        form_state.feed(load_the_lists(client, page.text))
        form: dict[str, str | list[str]] = {
            "profile": form_state.choice("profile"),
            "title": _TITLE,
            "tags": form_state.tags,
            "correspondent": form_state.choice("correspondent"),
        }
        submitted = client.post("/api/scan", data=form)
        assert submitted.status_code == 200, submitted.text
        store: JobStore = services_of(app).job_store
        job_id = store.list_recent(limit=1)[0].id
        job = wait_for_state(store, job_id, TERMINAL_STATES, _BUDGET)
    assert job.state is JobState.DONE, job
    return recorder, form


def test_twin_metadata_untouched_form_matches_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The form as it opens sends what ``saneless scan`` with no profile sends.

    Both reach ``default`` without anyone naming it: the page by opening on
    it, the command line by using it when no ``--profile`` is given.  Its
    ``default_tags`` and ``default_correspondent`` reach the web upload only
    if the page renders them ticked and selected; the command line takes them
    from the profile.  Everything but the document itself is the same, part
    for part.
    """
    web, form = _untouched_web_upload(tmp_path / "web", monkeypatch)
    cli_run = _run_cli(tmp_path / "cli", monkeypatch, _DEFAULT_RUN)

    assert cli_run.result.exit_code == 0, cli_run.result.output
    assert form["profile"] == _DEFAULT
    assert len(web.uploads()) == 1
    assert len(cli_run.recorder.uploads()) == 1
    web_fields = Counter(
        field for field in web.upload_fields(0) if field[0] != "document"
    )
    cli_fields = Counter(
        field for field in cli_run.recorder.upload_fields(0) if field[0] != "document"
    )
    assert web_fields == cli_fields
    assert web_fields == Counter(
        [("title", _TITLE), ("tags", "3"), ("tags", "7"), ("correspondent", "12")]
    )
