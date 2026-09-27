"""
The scan form's "Multiple pages" choice, over the HTTP interface.

The checkbox is a per-scan choice: it is always on the form, unchecked on
every page load, disabled with a visible reason while a manual-duplex profile
is chosen, and re-rendered by ``GET /api/profiles/multi-page`` when the profile
changes.  The server refuses the manual-duplex combination on its own, before
any job row exists, because a disabled checkbox is only a convenience; an
accepted submit hands the choice to the worker as ``ScanOptions``.

The browser half -- htmx really re-rendering the field, and Firefox not
restoring a tick on reload -- lives in ``tests/test_browser.py``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from saneless.config import ProfileConfig
from saneless.vocabulary import (
    MULTI_PAGE_DISABLED_REASON,
    MULTI_PAGE_HELP,
    MULTI_PAGE_LABEL,
    RequestRejection,
    SubmitResult,
    rejection_message,
    rejection_status_code,
)
from saneless.web.app import create_app
from saneless.worker import ScanOptions
from tests.conftest import StubScannerBackend

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from fastapi import FastAPI

    from saneless.config import Settings
    from saneless.job import Job, JobStore

# Every app built here talks to a Paperless client whose requests fail inside
# the process: nothing reaches the network.
pytestmark = pytest.mark.usefixtures("offline_paperless")

# The flatbed is the default profile, which every configuration must have.
FLATBED = "default"
FEEDER = "feeder"
DUPLEX = "duplex"

# The refusal's sentence, written out rather than read back from the function
# under test, so a changed message is a failing test and not a silent drift.
_REFUSAL_SENTENCE = (
    "Multiple pages is not available with manual duplex, so the scan was not "
    "started. Untick Multiple pages or choose another profile, then try again."
)

_CHECKBOX = re.compile(r'<input[^>]*\bid="multi-page"[^>]*>')
_HELP = re.compile(
    r'<small id="multi-page-help">\s*(?P<text>.*?)\s*</small>', re.DOTALL
)
_WRAPPER = re.compile(r'<div id="multi-page-field"[^>]*>', re.DOTALL)
_ATTRIBUTE_NAME = re.compile(r"\s([a-z-]+)(?==|\s|>|$)")


@dataclass
class _Recorded:
    """A worker ``submit`` stand-in that accepts and keeps what it was handed."""

    calls: list[tuple[Job, ScanOptions]] = field(default_factory=list)

    def __call__(self, job: Job, options: ScanOptions) -> SubmitResult:
        """
        Record one submit and accept it.

        ``options`` has no default on purpose: a route that stopped passing the
        choice would fail here rather than inherit the single-pass default.
        """
        self.calls.append((job, options))
        return SubmitResult.ACCEPTED


@dataclass(frozen=True)
class _Served:
    """A test client, its app, and the recorder standing in for ``submit``."""

    client: TestClient
    app: FastAPI
    submitted: _Recorded

    @property
    def job_store(self) -> JobStore:
        """Return the app's job store."""
        store: JobStore = self.app.state.job_store
        return store


def _profiles(*, duplex_first: bool = False) -> dict[str, ProfileConfig]:
    """
    Return a flatbed, a feeder and a manual-duplex profile.

    The flatbed makes the device read as having a glass, so the select keeps
    configuration order and ``duplex_first`` decides which profile the page
    opens on.
    """
    others = {
        FLATBED: ProfileConfig(),
        FEEDER: ProfileConfig(source="ADF"),
    }
    duplex = {DUPLEX: ProfileConfig(source="ADF Front", duplex="manual")}
    return {**duplex, **others} if duplex_first else {**others, **duplex}


def _serve(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Served]:
    """Run an app for ``settings`` whose worker records every submit."""
    app = create_app(settings, StubScannerBackend())
    app.state.paperless.get_tags = list
    app.state.paperless.get_correspondents = list
    submitted = _Recorded()
    monkeypatch.setattr(app.state.worker, "submit", submitted)
    with TestClient(app) as client:
        yield _Served(client=client, app=app, submitted=submitted)


@pytest.fixture
def served(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> Iterator[_Served]:
    """Serve the three profiles, opening on the flatbed one."""
    yield from _serve(make_settings(profiles=_profiles()), monkeypatch)


@pytest.fixture
def duplex_first(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> Iterator[_Served]:
    """Serve the three profiles, opening on the manual-duplex one."""
    yield from _serve(make_settings(profiles=_profiles(duplex_first=True)), monkeypatch)


def _checkbox_tag(markup: str) -> str:
    """Return the one Multiple pages checkbox's opening tag in ``markup``."""
    found = _CHECKBOX.findall(markup)
    assert len(found) == 1, found
    return found[0]


def _checkbox(markup: str) -> set[str]:
    """Return the attribute names on the one Multiple pages checkbox in ``markup``."""
    return set(_ATTRIBUTE_NAME.findall(_checkbox_tag(markup)))


def _help_line(markup: str) -> str:
    """Return the text of the checkbox's help slot in ``markup``."""
    match = _HELP.search(markup)
    assert match is not None, markup
    return match.group("text")


class TestTheFormCarriesTheCheckbox:
    """The checkbox on the full page, as the page opens."""

    def test_the_page_opens_with_the_checkbox_unticked_and_explained(
        self, served: _Served
    ) -> None:
        """
        Always present, never ticked on load, described by its help line.

        ``autocomplete="off"`` is pinned by string as well as by the Firefox
        reload test: it is what stops a browser restoring a tick on reload, and
        a page reload is the one place the server cannot see.
        """
        page = served.client.get("/").text

        tag = _checkbox_tag(page)
        attributes = _checkbox(page)
        assert 'name="multi_page"' in tag
        assert 'autocomplete="off"' in tag
        assert "checked" not in attributes
        assert "disabled" not in attributes
        assert MULTI_PAGE_LABEL in page
        assert _help_line(page) == MULTI_PAGE_HELP
        assert "aria-disabled" not in page

    def test_the_field_sits_between_the_profile_help_and_the_title(
        self, served: _Served
    ) -> None:
        """The control that decides whether it is enabled sits right above it."""
        page = served.client.get("/").text

        description = page.index('id="profile-description"')
        field_at = page.index('id="multi-page-field"')
        title = page.index('for="title-input"')
        assert description < field_at < title

    def test_a_page_opening_on_manual_duplex_disables_it_with_the_reason(
        self, duplex_first: _Served
    ) -> None:
        """The real ``disabled`` attribute, and the reason as visible text."""
        page = duplex_first.client.get("/").text

        attributes = _checkbox(page)
        assert "disabled" in attributes
        assert "checked" not in attributes
        assert _help_line(page) == MULTI_PAGE_DISABLED_REASON
        assert "aria-disabled" not in page


class TestTheRefreshRoute:
    """``GET /api/profiles/multi-page`` re-renders the field for a profile."""

    def test_a_flatbed_profile_renders_it_enabled_and_unticked(
        self, served: _Served
    ) -> None:
        """No tick asked for, none given; the wrapper owns its own swap."""
        response = served.client.get(
            "/api/profiles/multi-page", params={"profile": FLATBED}
        )

        assert response.status_code == 200
        wrapper = _WRAPPER.search(response.text)
        assert wrapper is not None, response.text
        assert 'hx-trigger="change from:#profile-select"' in wrapper.group(0)
        assert 'hx-swap="outerHTML"' in wrapper.group(0)
        attributes = _checkbox(response.text)
        assert "checked" not in attributes
        assert "disabled" not in attributes
        assert _help_line(response.text) == MULTI_PAGE_HELP

    @pytest.mark.parametrize("profile", [FLATBED, FEEDER])
    def test_a_tick_survives_a_change_to_a_profile_that_allows_it(
        self, served: _Served, profile: str
    ) -> None:
        """A profile switch must not silently drop a choice."""
        response = served.client.get(
            "/api/profiles/multi-page",
            params={"profile": profile, "multi_page": "on"},
        )

        assert response.status_code == 200
        attributes = _checkbox(response.text)
        assert "checked" in attributes
        assert "disabled" not in attributes

    def test_a_manual_duplex_profile_disables_it_and_drops_the_tick(
        self, served: _Served
    ) -> None:
        """Disabled, unticked, and the reason in place of the help line."""
        response = served.client.get(
            "/api/profiles/multi-page",
            params={"profile": DUPLEX, "multi_page": "on"},
        )

        assert response.status_code == 200
        attributes = _checkbox(response.text)
        assert "disabled" in attributes
        assert "checked" not in attributes
        assert _help_line(response.text) == MULTI_PAGE_DISABLED_REASON
        assert "aria-disabled" not in response.text

    def test_an_unknown_profile_is_refused(self, served: _Served) -> None:
        """The same 422 the profile description route gives."""
        response = served.client.get(
            "/api/profiles/multi-page", params={"profile": "nope"}
        )

        assert response.status_code == 422
        assert response.json() == {
            "status": "error",
            "detail": rejection_message(RequestRejection.UNKNOWN_PROFILE),
        }


class TestTheScanSubmit:
    """``POST /api/scan`` carries the choice to the worker, or refuses it."""

    def test_manual_duplex_with_multiple_pages_is_refused_before_any_row(
        self, served: _Served
    ) -> None:
        """
        A forged submit gets the refusal and leaves nothing behind.

        The disabled checkbox is a convenience; this is the enforcement.
        """
        before = served.job_store.list_recent(limit=50)

        response = served.client.post(
            "/api/scan", data={"profile": DUPLEX, "multi_page": "on"}
        )

        assert response.status_code == 422
        assert response.json() == {
            "status": "error",
            "detail": rejection_message(RequestRejection.MULTI_PAGE_MANUAL_DUPLEX),
        }
        assert served.job_store.list_recent(limit=50) == before
        assert served.submitted.calls == []

    @pytest.mark.parametrize("profile", [FLATBED, FEEDER])
    def test_a_ticked_submit_reaches_the_worker_as_multi_page(
        self, served: _Served, profile: str
    ) -> None:
        """The first per-scan option, handed over beside the job."""
        response = served.client.post(
            "/api/scan", data={"profile": profile, "multi_page": "on"}
        )

        assert response.status_code == 200
        assert len(served.submitted.calls) == 1
        job, options = served.submitted.calls[0]
        assert job.profile == profile
        assert options == ScanOptions(multi_page=True)

    def test_an_unticked_submit_is_a_single_pass_scan(self, served: _Served) -> None:
        """An unticked checkbox sends nothing, and nothing means one pass."""
        response = served.client.post("/api/scan", data={"profile": FLATBED})

        assert response.status_code == 200
        assert [options for _, options in served.submitted.calls] == [
            ScanOptions(multi_page=False)
        ]

    def test_manual_duplex_without_multiple_pages_is_accepted_as_before(
        self, served: _Served
    ) -> None:
        """The refusal is about the combination, never about the profile."""
        response = served.client.post("/api/scan", data={"profile": DUPLEX})

        assert response.status_code == 200
        assert [(job.profile, options) for job, options in served.submitted.calls] == [
            (DUPLEX, ScanOptions(multi_page=False))
        ]


class TestTheRefusalVocabulary:
    """The new rejection's sentence and status."""

    def test_the_refusal_is_a_422_with_its_sentence(self) -> None:
        """Unprocessable, like an unknown profile, and says what to change."""
        rejection = RequestRejection.MULTI_PAGE_MANUAL_DUPLEX

        assert rejection_status_code(rejection) == 422
        assert rejection_message(rejection) == _REFUSAL_SENTENCE
