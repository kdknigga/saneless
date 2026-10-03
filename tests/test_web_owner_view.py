"""
What each browser is shown of a job, route by route.

Any device on the LAN can load the page and poll the status routes, so every
route that renders a job renders it through the owner-scoped view: the browser
that started a scan sees its title, its preview and its error text with the
host paths named relative to the data directory; every other browser sees a
generic title, no preview and fixed sentences.  A row that recorded no owner
is nobody's for those details, although its flip prompt still answers to
anyone.

No response of any route, for any viewer, carries a configured host directory
or the paperless-ngx address.
"""

from __future__ import annotations

import dataclasses
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jinja2
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from jinja2 import nodes

from saneless.config import PaperlessConfig, ProfileConfig
from saneless.job import JobResult
from saneless.vocabulary import (
    HIDDEN_JOB_TITLE,
    HIDDEN_PRESERVED_ERROR,
    ErrorCategory,
    JobState,
    ScanOutcome,
    SubmitResult,
    job_label,
    local_time,
    page_counts,
)
from saneless.web.app import create_app
from saneless.web.job_view import JobView
from saneless.web.routes import OWNER_COOKIE
from tests.conftest import StubScannerBackend, leaf_routes

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator

    from starlette.requests import Request
    from starlette.responses import Response

    from saneless.config import Settings
    from saneless.job import Job, JobStore

# Every app built here talks to a Paperless client whose requests fail inside
# the process, so the distinctive address below is never contacted.
pytestmark = pytest.mark.usefixtures("offline_paperless")

OWNER_TOKEN = "tok-owner"
OTHER_TOKEN = "tok-other"
OWNER_TITLE = "Owner Title Marker"
RUNNING_TITLE = "Running Marker"
THUMBNAIL = "dGVzdA=="
PREVIEW = "data:image/jpeg"
PAPERLESS_HOST = "paperless-sweep-marker.example"
PAPERLESS_URL = f"http://{PAPERLESS_HOST}:8000"
TEMPLATES = Path(__file__).parent.parent / "src" / "saneless" / "web" / "templates"

# The routes that render one job's status for this browser.
_STATUS_ROUTES = ("/", "/api/jobs/current/status", "/api/jobs/history")


def _app(client: TestClient) -> FastAPI:
    """Extract the FastAPI app from a TestClient, helping the type checker."""
    app: Any = client.app
    if not isinstance(app, FastAPI):
        msg = "Expected FastAPI app"
        raise TypeError(msg)
    return app


@pytest.fixture
def view_settings(make_settings: Callable[..., Settings], tmp_path: Path) -> Settings:
    """Build settings whose host paths and paperless address are easy to find."""
    consume = tmp_path / "consume"
    consume.mkdir()
    return make_settings(
        paperless=PaperlessConfig(
            url=PAPERLESS_URL, token="test-token", consume_dir=consume
        ),
        profiles={
            "default": ProfileConfig(),
            "duplex": ProfileConfig(source="ADF Duplex"),
        },
    )


@pytest.fixture
def app(view_settings: Settings) -> FastAPI:
    """Create the app over the stub scanner, with fixed tag and correspondent lists."""
    built = create_app(view_settings, StubScannerBackend())
    built.state.paperless.get_tags = lambda *, timeout=None: [
        {"id": 1, "name": "receipt"}
    ]
    built.state.paperless.get_correspondents = lambda *, timeout=None: [
        {"id": 1, "name": "ACME"}
    ]
    return built


@pytest.fixture
def owner(app: FastAPI) -> Iterator[TestClient]:
    """Return the browser holding ``OWNER_TOKEN``, with the app started."""
    with TestClient(app) as client:
        client.cookies.set(OWNER_COOKIE, OWNER_TOKEN)
        yield client


@pytest.fixture(params=[None, OTHER_TOKEN], ids=["no-cookie", "other-cookie"])
def other(owner: TestClient, request: pytest.FixtureRequest) -> TestClient:
    """
    Return a second browser over the same running app.

    It presents either no owner cookie or a token of its own; neither is the
    owner's.  The app is already started by ``owner``, so this client is used
    without entering its lifespan.
    """
    client = TestClient(_app(owner))
    token: str | None = request.param
    if token is not None:
        client.cookies.set(OWNER_COOKIE, token)
    return client


def _store(client: TestClient) -> JobStore:
    """Return the job store of the client's app."""
    store: JobStore = _app(client).state.job_store
    return store


@contextmanager
def _running(client: TestClient, job_id: str) -> Generator[None]:
    """Make the worker report ``job_id`` as the job in flight, then clear it."""
    worker = _app(client).state.worker
    worker._current_job_id = job_id
    try:
        yield
    finally:
        worker._current_job_id = None


def _done_row(store: JobStore, *, owner_token: str | None) -> Job:
    """Record a finished, counted scan with a preview, owned by ``owner_token``."""
    job = store.create_job(
        profile="default", title=OWNER_TITLE, owner_token=owner_token
    )
    store.update_thumbnail(job.id, THUMBNAIL)
    store.finish_job(
        job.id,
        JobState.DONE,
        JobResult(
            outcome=ScanOutcome.SUCCESS,
            warning=None,
            pages_scanned=3,
            pages_removed=1,
            pages_uploaded=2,
        ),
    )
    finished = store.get_job(job.id)
    assert finished is not None
    return finished


def _job_status_routes(job_id: str) -> tuple[str, ...]:
    """Every GET route that renders this job's status, the followed one included."""
    return (*_STATUS_ROUTES, f"/api/jobs/{job_id}/status")


class TestOwnedRow:
    """A row the owner started: its detail is the owner's alone."""

    def test_the_owner_sees_the_title_and_preview_on_every_route(
        self, owner: TestClient
    ) -> None:
        """
        The status area, both polls and history name the owner's own scan.

        The preview rides with the live outcome on the polls.  The page reports
        a finished scan as the last one, by title and with no preview, and
        history never carries one.
        """
        job = _done_row(_store(owner), owner_token=OWNER_TOKEN)

        for path in _job_status_routes(job.id):
            text = owner.get(path).text
            assert OWNER_TITLE in text, path
            previewed = path not in {"/", "/api/jobs/history"}
            assert (f"{PREVIEW};base64,{THUMBNAIL}" in text) is previewed, path

    def test_another_browser_sees_the_generic_title_and_no_preview(
        self, owner: TestClient, other: TestClient
    ) -> None:
        """Nothing the owner typed or scanned reaches another browser."""
        job = _done_row(_store(owner), owner_token=OWNER_TOKEN)

        for path in _job_status_routes(job.id):
            text = other.get(path).text
            assert OWNER_TITLE not in text, path
            assert PREVIEW not in text, path
            assert HIDDEN_JOB_TITLE in text, path

    def test_history_keeps_what_everyone_may_see(
        self, owner: TestClient, other: TestClient
    ) -> None:
        """Time, profile, outcome label and counts stay visible to every browser."""
        job = _done_row(_store(owner), owner_token=OWNER_TOKEN)
        counts = page_counts(job)
        assert counts is not None

        text = other.get("/api/jobs/history").text

        assert local_time(job.created_at) in text
        assert "<td>default</td>" in text
        assert job_label(JobState.DONE, None) in text
        assert counts in text
        assert HIDDEN_JOB_TITLE in text
        assert OWNER_TITLE not in text

    def test_history_names_only_this_browsers_rows(
        self, owner: TestClient, other: TestClient
    ) -> None:
        """Each browser sees its own row's title and the generic title for the rest."""
        store = _store(owner)
        _done_row(store, owner_token=OWNER_TOKEN)
        theirs = store.create_job(
            profile="default", title="Other Title Marker", owner_token=OTHER_TOKEN
        )
        store.finish_job(theirs.id, JobState.DONE)

        owner_text = owner.get("/api/jobs/history").text
        assert OWNER_TITLE in owner_text
        assert "Other Title Marker" not in owner_text
        assert HIDDEN_JOB_TITLE in owner_text

        other_text = other.get("/api/jobs/history").text
        assert OWNER_TITLE not in other_text
        assert HIDDEN_JOB_TITLE in other_text

    def test_the_scan_response_names_only_the_submitters_own_scan(
        self,
        owner: TestClient,
        other: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A submit queued behind the owner's scan names it to the owner alone."""
        monkeypatch.setattr(
            _app(owner).state.worker,
            "submit",
            lambda _job, _options: SubmitResult.ACCEPTED,
        )
        store = _store(owner)
        running = store.create_job(
            profile="default", title=RUNNING_TITLE, owner_token=OWNER_TOKEN
        )
        store.update_state(running.id, JobState.SCANNING)

        with _running(owner, running.id):
            mine = owner.post("/api/scan", data={"profile": "default", "title": "A"})
            theirs = other.post("/api/scan", data={"profile": "default", "title": "B"})

        assert mine.status_code == 200
        assert theirs.status_code == 200
        assert RUNNING_TITLE in mine.text
        assert RUNNING_TITLE not in theirs.text
        assert HIDDEN_JOB_TITLE in theirs.text


class TestUnownedRow:
    """A row that recorded no owner: its detail is nobody's, its flip is anyone's."""

    def test_nobody_sees_an_unowned_rows_title_or_preview(
        self, owner: TestClient, other: TestClient
    ) -> None:
        """Holding a token proves nothing about a row that recorded none."""
        job = _done_row(_store(owner), owner_token=None)

        for client in (owner, other):
            for path in _job_status_routes(job.id):
                text = client.get(path).text
                assert OWNER_TITLE not in text, path
                assert PREVIEW not in text, path

    def test_an_unowned_flip_still_offers_its_buttons_to_anyone(
        self, owner: TestClient, other: TestClient
    ) -> None:
        """An unowned row's flip prompt offers its buttons to any browser."""
        store = _store(owner)
        job = store.create_job(profile="duplex", title=OWNER_TITLE)
        store.update_thumbnail(job.id, THUMBNAIL)
        store.update_state(job.id, JobState.AWAITING_FLIP)

        with _running(owner, job.id):
            text = other.get("/api/jobs/current/status").text

        assert 'hx-post="/api/flip/continue"' in text
        assert 'hx-post="/api/flip/abort"' in text
        assert OWNER_TITLE not in text
        assert PREVIEW not in text


class TestQueuedLine:
    """The line a queued job shows names the running job only to its owner."""

    def test_a_queued_browser_is_not_told_whose_scan_is_running(
        self, owner: TestClient, other: TestClient
    ) -> None:
        """Another person's running scan is named by the generic title."""
        store = _store(owner)
        running = store.create_job(
            profile="default", title=RUNNING_TITLE, owner_token=OWNER_TOKEN
        )
        store.update_state(running.id, JobState.SCANNING)
        queued = store.create_job(
            profile="default", title="Queued", owner_token=OTHER_TOKEN
        )

        with _running(owner, running.id):
            text = other.get(f"/api/jobs/{queued.id}/status").text

        assert "Waiting for" in text
        assert RUNNING_TITLE not in text
        assert HIDDEN_JOB_TITLE in text

    def test_the_owner_of_both_jobs_sees_the_running_title(
        self, owner: TestClient
    ) -> None:
        """A browser queued behind its own scan is told which one it is waiting on."""
        store = _store(owner)
        running = store.create_job(
            profile="default", title=RUNNING_TITLE, owner_token=OWNER_TOKEN
        )
        store.update_state(running.id, JobState.SCANNING)
        queued = store.create_job(
            profile="default", title="Queued", owner_token=OWNER_TOKEN
        )

        with _running(owner, running.id):
            text = owner.get(f"/api/jobs/{queued.id}/status").text

        assert "Waiting for" in text
        assert RUNNING_TITLE in text


class TestNoHostPathOnTheWeb:
    """No response of any route carries a configured directory or the address."""

    @staticmethod
    def _seed(store: JobStore, settings: Settings) -> tuple[Job, Job]:
        """Record an ERROR row and a FALLBACK row whose text names every secret."""
        data_dir = settings.output.data_dir
        tmp_dir = settings.output.tmp_dir
        consume_dir = settings.paperless.consume_dir
        failed = store.create_job(
            profile="default", title="Failed Marker", owner_token=OWNER_TOKEN
        )
        store.finish_job(
            failed.id,
            JobState.ERROR,
            error=(
                f"Paperless at {PAPERLESS_URL}: refused. The PDF was preserved at "
                f"{data_dir}/failed/marker.pdf; the workspace was {tmp_dir}/w"
            ),
            error_category=ErrorCategory.UPLOAD,
        )
        fallback = store.create_job(
            profile="default", title="Fallback Marker", owner_token=OWNER_TOKEN
        )
        store.finish_job(
            fallback.id,
            JobState.FALLBACK,
            JobResult(
                outcome=ScanOutcome.FALLBACK,
                warning=f"The PDF was copied to {consume_dir}/doc.pdf",
                pages_scanned=1,
                pages_removed=0,
                pages_uploaded=1,
            ),
        )
        return failed, fallback

    @staticmethod
    def _secrets(settings: Settings) -> tuple[str, ...]:
        """Return every string no web response may contain."""
        consume_dir = settings.paperless.consume_dir
        assert consume_dir is not None
        return (
            str(settings.output.data_dir),
            str(settings.output.tmp_dir),
            str(consume_dir),
            PAPERLESS_HOST,
        )

    @staticmethod
    def _get_paths(app: FastAPI, job_ids: tuple[str, ...]) -> list[str]:
        """Every parameter-free GET route, plus the followed status of each job."""
        paths = [
            route.path
            for route in leaf_routes(app)
            if isinstance(route, APIRoute)
            and "GET" in (route.methods or ())
            and "{" not in route.path
        ]
        assert "/" in paths
        assert "/api/jobs/history" in paths
        return [*paths, *(f"/api/jobs/{job_id}/status" for job_id in job_ids)]

    def test_no_response_names_a_host_path_or_the_paperless_address(
        self, owner: TestClient, other: TestClient, view_settings: Settings
    ) -> None:
        """No GET route or flip answer names a host path, for owner or anyone else."""
        failed, fallback = self._seed(_store(owner), view_settings)
        forbidden = self._secrets(view_settings)
        paths = self._get_paths(_app(owner), (failed.id, fallback.id))

        for job in (failed, fallback):
            with _running(owner, job.id):
                for client in (owner, other):
                    responses = [(path, client.get(path)) for path in paths]
                    responses.extend(
                        (path, client.post(path, data={"job_id": job.id}))
                        for path in ("/api/flip/continue", "/api/flip/abort")
                    )
                    for path, response in responses:
                        for secret in forbidden:
                            assert secret not in response.text, (path, secret)

    def test_the_owner_sees_the_kept_file_relative_to_the_data_directory(
        self, owner: TestClient, other: TestClient, view_settings: Settings
    ) -> None:
        """The owner learns the file name; everyone else learns only that it was kept."""
        failed, _ = self._seed(_store(owner), view_settings)

        path = f"/api/jobs/{failed.id}/status"
        owner_text = owner.get(path).text
        other_text = other.get(path).text

        assert "failed/marker.pdf" in owner_text
        assert "failed/marker.pdf" not in other_text
        assert HIDDEN_PRESERVED_ERROR in other_text


class TestTemplateContract:
    """Templates receive a JobView and read nothing a JobView does not carry."""

    def test_templates_read_only_job_view_attributes(self) -> None:
        """
        Every job attribute a template reads is one a JobView carries.

        A template reading a Job-only attribute would bypass the owner gate.
        """
        allowed = {field.name for field in dataclasses.fields(JobView)}
        allowed |= {"is_active", "is_busy"}
        environment = jinja2.Environment(autoescape=True)
        read: set[tuple[str, str]] = set()
        templates = sorted(TEMPLATES.rglob("*.html"))
        assert templates
        for template in templates:
            tree = environment.parse(template.read_text(encoding="utf-8"))
            # ``last_job`` is the page's finished job, a view like ``job``.
            read.update(
                (template.name, node.attr)
                for node in tree.find_all(nodes.Getattr)
                if isinstance(node.node, nodes.Name)
                and node.node.name in {"job", "last_job"}
            )

        assert read, "no template reads a job attribute; the pattern has broken"
        assert {entry for entry in read if entry[1] not in allowed} == set()

    def test_every_route_hands_templates_job_views(
        self, owner: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The status context's job and every history row are views, not rows.

        The page hands a finished job over as ``last_job`` and sets ``job`` to
        None, so both keys are read, and the one that is set must be a view.
        """
        job = _done_row(_store(owner), owner_token=OWNER_TOKEN)
        templates = _app(owner).state.templates
        original = templates.TemplateResponse
        contexts: list[dict[str, object]] = []

        # Every route passes the request, the template name and the context
        # positionally, so the third argument is the context.
        def recording(
            request: Request, name: str, context: dict[str, object]
        ) -> Response:
            contexts.append(context)
            response: Response = original(request, name, context)
            return response

        monkeypatch.setattr(templates, "TemplateResponse", recording)

        for path in _job_status_routes(job.id):
            owner.get(path)

        jobs = [
            context[key]
            for context in contexts
            for key in ("job", "last_job")
            if context.get(key) is not None
        ]
        rows: list[object] = []
        for context in contexts:
            listed = context.get("jobs")
            if isinstance(listed, list):
                rows.extend(listed)
        assert jobs
        assert rows
        assert all(isinstance(value, JobView) for value in jobs)
        assert all(isinstance(value, JobView) for value in rows)
