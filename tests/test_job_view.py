"""
Tests for ``saneless.web.job_view``, what one viewer may see of one job.

The owner of a job -- the browser whose token was recorded with it -- sees its
title, thumbnail and its error and warning text, with every host path and the
paperless-ngx address replaced by the name of the setting that holds it.
Everyone else sees that a scan happened and how it ended, never what the
document is: a generic title, no thumbnail and fixed sentences in place of the
stored text.  A row that recorded no owner is nobody's.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from saneless.web.job_view import JobView, build_job_view, owns_detail

from saneless.config import OutputConfig, PaperlessConfig
from saneless.job import Job, JobStore
from saneless.vocabulary import (
    HIDDEN_ERROR_DETAIL,
    HIDDEN_JOB_TITLE,
    HIDDEN_PRESERVED_ERROR,
    HIDDEN_WARNING_LINE,
    QUEUE_FULL_JOB_ERROR,
    WARNED_UPLOAD_LABEL,
    ErrorCategory,
    JobState,
    ScanOutcome,
    job_label,
    page_counts,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from saneless.config import Settings

_OWNER = "owner-token"
_OTHER = "someone-else"
_PAPERLESS_URL = "http://paperless.example:8000"
_PAPERLESS_HOST = "paperless.example"

# The attributes the templates read off a job, and so exactly the fields a
# view carries.  A field added to the view without a template needing it is
# one more thing that could leak.
_TEMPLATE_FIELDS = (
    "id",
    "profile",
    "state",
    "created_at",
    "error_category",
    "pages_scanned",
    "pages_removed",
    "pages_uploaded",
    "title",
    "thumbnail",
    "error",
    "warning",
)
_PUBLIC_FIELDS = (
    "id",
    "profile",
    "state",
    "created_at",
    "error_category",
    "pages_scanned",
    "pages_removed",
    "pages_uploaded",
)


@pytest.fixture
def settings(make_settings: Callable[..., Settings], tmp_path: Path) -> Settings:
    """Build settings whose every host path sits under ``tmp_path``."""
    return make_settings(
        output=OutputConfig(
            tmp_dir=tmp_path / "scratch",
            data_dir=tmp_path / "state",
            log_file=tmp_path / "state" / "saneless.log",
        ),
        paperless=PaperlessConfig(
            url=_PAPERLESS_URL,
            token="test-token",
            consume_dir=tmp_path / "consume",
        ),
    )


def _job(**overrides: object) -> Job:
    """Build a finished job with a title, a thumbnail, counts and an owner."""
    job = Job(
        id="job-1",
        profile="default",
        title="Tax 2025",
        state=JobState.DONE,
        created_at=datetime(2026, 3, 1, 9, 30, tzinfo=UTC),
        thumbnail="aGVsbG8=",
        outcome=ScanOutcome.DONE,
        pages_scanned=4,
        pages_removed=1,
        pages_uploaded=3,
        owner_token=_OWNER,
    )
    return dataclasses.replace(job, **overrides)


def _view(
    job: Job, *, presented: str | None, settings: Settings, tmp_path: Path
) -> JobView:
    """
    Build a view and check it carries no host path and no paperless address.

    Every view in this file goes through here, so the rule is checked on all
    of them rather than on the few built to test it.
    """
    view = build_job_view(job, presented=presented, settings=settings)
    forbidden = {str(tmp_path), str(tmp_path.resolve()), _PAPERLESS_HOST}
    for text in (view.title, view.error, view.warning):
        for needle in forbidden:
            assert needle not in (text or ""), (needle, text)
    return view


@pytest.mark.parametrize(
    ("presented", "recorded", "expected"),
    [
        pytest.param(None, None, False, id="nothing-presented-null-row"),
        pytest.param("a", None, False, id="null-row-is-nobodys"),
        pytest.param(None, "a", False, id="nothing-presented"),
        pytest.param("a", "a", True, id="same-token"),
        pytest.param("a", "b", False, id="other-token"),
        pytest.param("été", "a", False, id="non-ascii-presented"),
        pytest.param("été", "été", True, id="non-ascii-match"),
    ],
)
def test_owns_detail(
    *, presented: str | None, recorded: str | None, expected: bool
) -> None:
    """Only a presented token equal to a recorded one owns the detail."""
    assert owns_detail(presented, recorded) is expected


def test_view_fields_are_exactly_what_templates_read() -> None:
    """The view carries the template contract and nothing else."""
    assert tuple(field.name for field in dataclasses.fields(JobView)) == (
        _TEMPLATE_FIELDS
    )


@pytest.mark.parametrize(
    "state",
    [JobState.DONE, JobState.SCANNING, JobState.AWAITING_FLIP],
    ids=lambda state: state.value,
)
def test_owner_sees_every_field(
    settings: Settings, tmp_path: Path, state: JobState
) -> None:
    """The owner's view of a job with no path in it is the job itself."""
    job = _job(state=state, warning="The scanner skipped a sheet")

    view = _view(job, presented=_OWNER, settings=settings, tmp_path=tmp_path)

    for name in _TEMPLATE_FIELDS:
        assert getattr(view, name) == getattr(job, name), name
    assert view.is_active is job.is_active
    assert view.is_busy is job.is_busy


@pytest.mark.parametrize(
    ("presented", "recorded"),
    [
        pytest.param(_OTHER, _OWNER, id="other-token"),
        pytest.param(None, _OWNER, id="no-token"),
        pytest.param(_OWNER, None, id="null-owner-row"),
        pytest.param(None, None, id="null-owner-row-no-token"),
    ],
)
def test_non_owner_sees_generic_title_and_no_thumbnail(
    settings: Settings,
    tmp_path: Path,
    presented: str | None,
    recorded: str | None,
) -> None:
    """Everyone else sees that a scan happened and how it ended, nothing more."""
    job = _job(owner_token=recorded)

    view = _view(job, presented=presented, settings=settings, tmp_path=tmp_path)

    assert view.title == HIDDEN_JOB_TITLE
    assert view.thumbnail is None
    for name in _PUBLIC_FIELDS:
        assert getattr(view, name) == getattr(job, name), name
    assert view.is_active is job.is_active
    assert view.is_busy is job.is_busy
    assert page_counts(view) == page_counts(job)
    assert page_counts(view) is not None


def test_preserved_error_relativised_for_owner_hidden_for_others(
    settings: Settings, tmp_path: Path
) -> None:
    """The owner sees the kept file under ``failed/``; others get the pointer."""
    kept = settings.output.failed_dir / "tax-2025-x.pdf"
    job = _job(
        state=JobState.ERROR,
        error_category=ErrorCategory.UPLOAD,
        error=f"Upload failed; preserved at {kept} (Paperless at {_PAPERLESS_URL} "
        "refused)",
    )

    owner = _view(job, presented=_OWNER, settings=settings, tmp_path=tmp_path)
    other = _view(job, presented=_OTHER, settings=settings, tmp_path=tmp_path)

    assert owner.error == (
        "Upload failed; preserved at failed/tax-2025-x.pdf "
        "(Paperless at <paperless.url> refused)"
    )
    assert other.error == HIDDEN_PRESERVED_ERROR
    assert other.error_category is ErrorCategory.UPLOAD


def test_tmp_dir_error_relativised_for_owner_hidden_for_others(
    settings: Settings, tmp_path: Path
) -> None:
    """A scratch path becomes the setting's name; others get the log pointer."""
    job = _job(
        state=JobState.ERROR,
        error_category=ErrorCategory.ASSEMBLY,
        error=f"No space left on device: {settings.output.tmp_dir}/x",
    )

    owner = _view(job, presented=_OWNER, settings=settings, tmp_path=tmp_path)
    other = _view(job, presented=None, settings=settings, tmp_path=tmp_path)

    assert owner.error == "No space left on device: <output.tmp_dir>/x"
    assert other.error == HIDDEN_ERROR_DETAIL


@pytest.mark.parametrize(
    ("template", "expected"),
    [
        pytest.param(
            "Could not upload to Paperless at {url}: connection refused",
            "Could not upload to Paperless at <paperless.url>: connection refused",
            id="colon-after-url",
        ),
        pytest.param(
            "Paperless at {url}/api/documents/post_document/, then gave up.",
            "Paperless at <paperless.url>, then gave up.",
            id="comma-after-url",
        ),
        pytest.param(
            "Could not reach https://{host}.",
            "Could not reach <paperless.url>.",
            id="https-full-stop",
        ),
    ],
)
def test_owner_url_scrub_keeps_the_sentence(
    settings: Settings, tmp_path: Path, template: str, expected: str
) -> None:
    """Any web address is replaced, and punctuation after it stays in place."""
    job = _job(
        state=JobState.ERROR,
        error_category=ErrorCategory.UPLOAD,
        error=template.format(url=_PAPERLESS_URL, host=_PAPERLESS_HOST),
    )

    owner = _view(job, presented=_OWNER, settings=settings, tmp_path=tmp_path)

    assert owner.error == expected


def test_owner_sees_bare_data_dir_by_setting_name(
    settings: Settings, tmp_path: Path
) -> None:
    """``data_dir`` named on its own, not as a prefix, is named by its setting."""
    job = _job(
        state=JobState.ERROR,
        error_category=ErrorCategory.CONFIG,
        error=f"Cannot write to {settings.output.data_dir}",
    )

    owner = _view(job, presented=_OWNER, settings=settings, tmp_path=tmp_path)

    assert owner.error == "Cannot write to <output.data_dir>"


def test_symlinked_data_dir_is_relativised_in_both_spellings(
    make_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    """Text naming the resolved ``data_dir`` is relativised like the configured."""
    real = tmp_path / "real-state"
    real.mkdir()
    link = tmp_path / "state-link"
    link.symlink_to(real)
    settings = make_settings(
        output=OutputConfig(
            tmp_dir=tmp_path / "scratch",
            data_dir=link,
            log_file=link / "saneless.log",
        ),
    )
    assert settings.output.data_dir.resolve() != settings.output.data_dir
    job = _job(
        state=JobState.ERROR,
        error_category=ErrorCategory.UPLOAD,
        error=f"The scan was preserved at {real / 'failed' / 'tax.pdf'}",
    )

    owner = _view(job, presented=_OWNER, settings=settings, tmp_path=tmp_path)
    other = _view(job, presented=_OTHER, settings=settings, tmp_path=tmp_path)

    assert owner.error == "The scan was preserved at failed/tax.pdf"
    assert other.error == HIDDEN_PRESERVED_ERROR


def test_consume_dir_warning_relativised_for_owner_hidden_for_others(
    settings: Settings, tmp_path: Path
) -> None:
    """A fallback's consume-folder path becomes the setting's name for the owner."""
    consume_dir = settings.paperless.consume_dir
    assert consume_dir is not None
    job = _job(
        state=JobState.FALLBACK,
        outcome=ScanOutcome.FALLBACK,
        warning=f"Saved to {consume_dir}/doc.pdf",
    )

    owner = _view(job, presented=_OWNER, settings=settings, tmp_path=tmp_path)
    other = _view(job, presented=_OTHER, settings=settings, tmp_path=tmp_path)

    assert owner.warning == "Saved to <paperless.consume_dir>/doc.pdf"
    assert other.warning == HIDDEN_WARNING_LINE


def test_non_owner_warned_upload_still_reads_as_warned(
    settings: Settings, tmp_path: Path
) -> None:
    """The hidden warning is non-empty, so the outcome label keeps its warning."""
    job = _job(warning="The front and back page counts differed")

    other = _view(job, presented=_OTHER, settings=settings, tmp_path=tmp_path)

    assert other.warning == HIDDEN_WARNING_LINE
    assert job_label(other.state, other.warning) == WARNED_UPLOAD_LABEL


@pytest.mark.parametrize(
    "presented", [_OWNER, _OTHER, None], ids=["owner", "other", "no-token"]
)
def test_absent_text_stays_absent(
    settings: Settings, tmp_path: Path, presented: str | None
) -> None:
    """No stored error or warning is no error or warning, for every viewer."""
    job = _job()

    view = _view(job, presented=presented, settings=settings, tmp_path=tmp_path)

    assert view.error is None
    assert view.warning is None
    assert job_label(view.state, view.warning) == job_label(job.state, job.warning)


def test_hidden_sentences_carry_no_configured_value(settings: Settings) -> None:
    """The four fixed sentences are developer constants, never a setting."""
    hidden = (
        HIDDEN_JOB_TITLE,
        HIDDEN_WARNING_LINE,
        HIDDEN_PRESERVED_ERROR,
        HIDDEN_ERROR_DETAIL,
    )
    assert all(hidden)
    for sentence in hidden:
        assert "/" not in sentence
        assert "://" not in sentence
        assert str(settings.output.data_dir) not in sentence
    assert "saneless jobs --json" in HIDDEN_PRESERVED_ERROR


@pytest.mark.parametrize(
    "presented", [_OWNER, _OTHER, None], ids=["any-token", "other", "no-token"]
)
def test_refused_row_is_nobodys(
    settings: Settings, tmp_path: Path, presented: str | None
) -> None:
    """A refused submit's row records no owner, so every viewer sees it generic."""
    store = JobStore()
    try:
        job = store.create_rejected_job(
            "default", "Tax 2025", error=QUEUE_FULL_JOB_ERROR
        )
    finally:
        store.close()
    assert job.owner_token is None

    view = _view(job, presented=presented, settings=settings, tmp_path=tmp_path)

    assert view.title == HIDDEN_JOB_TITLE
    assert view.thumbnail is None
    assert view.error_category is ErrorCategory.REJECTED
