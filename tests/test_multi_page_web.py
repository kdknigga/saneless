"""
The scan form's "Multiple pages" choice, over the HTTP interface.

The checkbox is a per-scan choice: it is always on the form, unchecked on
every page load, disabled with a visible reason while a manual-duplex profile
is chosen, and re-rendered by ``GET /api/profiles/multi-page`` when the profile
changes.  The server refuses the manual-duplex combination on its own, before
any job row exists, because a disabled checkbox is only a convenience; an
accepted submit hands the choice to the worker as ``ScanOptions``.

Once a multi-page job is waiting, the status area says what it waits on.  The
browser that started the scan sees the open question with its buttons, each
posting to ``POST /api/multi-page/answer``; every other browser sees one plain
waiting line and no buttons at all.  The answer route passes an answer on only
from the owning browser, for the open prompt, and only an answer that prompt
offers; anything else re-renders the status area and nothing more.

The browser half -- htmx really re-rendering the field, and Firefox not
restoring a tick on reload -- lives in ``tests/test_browser.py``.
"""

from __future__ import annotations

import html
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from saneless.config import ProfileConfig
from saneless.vocabulary import (
    MULTI_PAGE_DISABLED_REASON,
    MULTI_PAGE_HELP,
    MULTI_PAGE_LABEL,
    NOTHING_TO_FINISH,
    PASS_WAIT_STATES,
    JobState,
    PassAnswer,
    PassPrompt,
    PassWait,
    RequestRejection,
    SubmitResult,
    abort_question,
    busy_line,
    local_time,
    non_owner_wait_line,
    pass_answer_label,
    pass_heading,
    pass_prompt_copy,
    pass_wait_state,
    progress_label,
    rejection_message,
    rejection_status_code,
)
from saneless.web.app import create_app
from saneless.web.owner import OWNER_COOKIE
from saneless.worker import ScanOptions, WorkerPassCoordinator
from tests.conftest import StubScannerBackend, poll_until, services_of, stand_in

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from fastapi import FastAPI

    from saneless.config import Settings
    from saneless.job import Job, JobStore


# The flatbed is the default profile, which every configuration must have and
# the page opens on.
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
        store: JobStore = services_of(self.app).job_store
        return store


def _profiles(*, duplex_opens: bool = False) -> dict[str, ProfileConfig]:
    """
    Return a flatbed, a feeder and a manual-duplex profile.

    The page opens on ``default``, wherever it is listed, so ``duplex_opens``
    decides which of the flatbed and the manual-duplex profile carries that
    name.  The other one keeps a name of its own, and the feeder is the same
    either way.
    """
    flatbed = ProfileConfig()
    feeder = ProfileConfig(source="ADF")
    duplex = ProfileConfig(source="ADF Front", duplex="manual")
    if duplex_opens:
        return {"glass": flatbed, FEEDER: feeder, "default": duplex}
    return {FLATBED: flatbed, FEEDER: feeder, DUPLEX: duplex}


def _serve(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Served]:
    """Run an app for ``settings`` whose worker records every submit."""
    app = create_app(settings, StubScannerBackend())
    stand_in(services_of(app).paperless, "get_tags", lambda *, timeout=None: [])
    stand_in(
        services_of(app).paperless, "get_correspondents", lambda *, timeout=None: []
    )
    submitted = _Recorded()
    monkeypatch.setattr(services_of(app).worker, "submit", submitted)
    with TestClient(app) as client:
        yield _Served(client=client, app=app, submitted=submitted)


@pytest.fixture
def served(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> Iterator[_Served]:
    """Serve the three profiles, opening on the flatbed, which is ``default``."""
    yield from _serve(make_settings(profiles=_profiles()), monkeypatch)


@pytest.fixture
def duplex_opens(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> Iterator[_Served]:
    """Serve the three profiles, the manual-duplex one named ``default``."""
    yield from _serve(make_settings(profiles=_profiles(duplex_opens=True)), monkeypatch)


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
        The checkbox is always present, unticked on load, with its help line.

        ``autocomplete="off"`` is what stops a browser restoring a tick on
        reload, and a page reload is the one place the server cannot see.
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
        self, duplex_opens: _Served
    ) -> None:
        """The checkbox carries a real ``disabled`` and the reason as visible text."""
        page = duplex_opens.client.get("/").text

        assert '<option value="default" selected>' in page

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
        """With no tick asked for, none is given; the wrapper owns its own swap."""
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
        """A switch to a profile that allows the choice keeps the tick."""
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
        """The field is disabled and unticked, the reason in place of its help line."""
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
        """An unknown profile gets the 422 the profile description route gives."""
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
        """A ticked submit hands the worker ``multi_page=True`` beside the job."""
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
    """The manual-duplex multi-page refusal's sentence and status."""

    def test_the_refusal_is_a_422_with_its_sentence(self) -> None:
        """The refusal is a 422, like an unknown profile, and says what to change."""
        rejection = RequestRejection.MULTI_PAGE_MANUAL_DUPLEX

        assert rejection_status_code(rejection) == 422
        assert rejection_message(rejection) == _REFUSAL_SENTENCE


# ---------------------------------------------------------------------------
# A waiting multi-page job: the status area, the answer route, the Scan button.

_ANSWER_ROUTE = "/api/multi-page/answer"

# The browser that loaded the paper, and one that did not.  Every waiting row
# staged below records the first.
_OWNER = "the-browser-that-loaded-the-paper"
_STRANGER = "a-browser-that-did-not"

# How long a test waits for a helper thread before calling it stuck.  The
# prompts themselves wait far longer, so only an answer or the teardown's
# interrupt ends them.
_BUDGET = 5.0
_PROMPT_TIMEOUT = 600.0

_NEXT_OFFERED = frozenset(
    {PassAnswer.NEXT, PassAnswer.FINISH, PassAnswer.RESCAN, PassAnswer.ABORT}
)
_NEXT_OFFERED_NOTHING_KEPT = frozenset(
    {PassAnswer.NEXT, PassAnswer.RESCAN, PassAnswer.ABORT}
)
_BLANK_OFFERED = frozenset(
    {PassAnswer.SKIP_BLANKS, PassAnswer.KEEP_BLANKS, PassAnswer.RESCAN}
)
_RETRY_OFFERED = frozenset({PassAnswer.NEXT, PassAnswer.FINISH, PassAnswer.ABORT})

# An error naming a device node: an absolute path no setting holds.
_DEVICE_ERROR = "Error during device I/O on /dev/bus/usb/001/004"

# Every status response renders the status area first and the Scan button
# out-of-band after it, so the button's opening tag is where the area ends.
# The button is found by its id, whatever order its attributes are in.
_SCAN_BUTTON = re.compile(
    r'<button(?=[^>]*\sid="scan-btn")(?P<attrs>\s[^>]*)>(?P<text>.*?)</button>',
    re.DOTALL,
)
_BUTTON = re.compile(r"<button\b(?P<attrs>[^>]*)>(?P<text>.*?)</button>", re.DOTALL)
_BUTTON_ID = re.compile(r'\bid="(?P<id>[^"]+)"')
_HX_VALS = re.compile(r"hx-vals='(?P<vals>[^']*)'")
_HX_CONFIRM = re.compile(r'hx-confirm="(?P<question>[^"]*)"')


def _next_pass(*, number: int = 3, kept: int = 4) -> PassPrompt:
    """Return the between-pass question; Finish is offered once a page is kept."""
    return PassPrompt(
        number=number,
        wait=PassWait.NEXT_PASS,
        pages_kept=kept,
        offered=_NEXT_OFFERED if kept else _NEXT_OFFERED_NOTHING_KEPT,
        timeout_seconds=_PROMPT_TIMEOUT,
        last_pass_pages=1,
        last_pass_kept=1,
    )


def _blank(*, number: int = 2) -> PassPrompt:
    """Return the blank-page question about a one-page pass."""
    return PassPrompt(
        number=number,
        wait=PassWait.BLANK_DECISION,
        pages_kept=1,
        offered=_BLANK_OFFERED,
        timeout_seconds=_PROMPT_TIMEOUT,
        pass_pages=1,
        blank_positions=(1,),
    )


def _retry(error: str, *, number: int = 4) -> PassPrompt:
    """Return the failed-pass question, two pages already kept."""
    return PassPrompt(
        number=number,
        wait=PassWait.RETRY,
        pages_kept=2,
        offered=_RETRY_OFFERED,
        timeout_seconds=_PROMPT_TIMEOUT,
        error=error,
    )


def _prompt_for(wait: PassWait) -> PassPrompt:
    """Return an open question of kind ``wait``."""
    match wait:
        case PassWait.NEXT_PASS:
            return _next_pass()
        case PassWait.BLANK_DECISION:
            return _blank()
        case PassWait.RETRY:
            return _retry(_DEVICE_ERROR)


class _Asker:
    """
    Run one ``ask`` on a thread of its own, as the worker thread would.

    Args:
        coordinator: The coordinator to ask.
        prompt: The prompt to ask it.

    """

    def __init__(self, coordinator: WorkerPassCoordinator, prompt: PassPrompt) -> None:
        """Start the ask."""
        self._coordinator = coordinator
        self._prompt = prompt
        self._answers: list[PassAnswer] = []
        self._thread = threading.Thread(target=self._ask, daemon=True)
        self._thread.start()

    def _ask(self) -> None:
        """Ask, and keep the answer."""
        self._answers.append(self._coordinator.ask(self._prompt))

    def result(self) -> PassAnswer:
        """
        Wait for the ask to return, and return its answer.

        Returns:
            What ``ask`` returned.

        """
        self._thread.join(_BUDGET)
        assert not self._thread.is_alive(), "ask never returned"
        assert len(self._answers) == 1
        return self._answers[0]

    def join(self) -> None:
        """Wait for the ask to return, whatever it returned."""
        self._thread.join(_BUDGET)


@dataclass(frozen=True)
class _Waiting:
    """One staged multi-page job with its prompt open."""

    job_id: str
    prompt: PassPrompt
    coordinator: WorkerPassCoordinator
    asker: _Asker


class _Stager:
    """
    Stage waiting multi-page jobs on the served worker without running a scan.

    The worker's current job and pass coordinator are set directly, the same
    deliberate reach-through the flip tests use: driving a real multi-page
    pipeline would scan, and these tests are about what the page says and
    accepts.  ``close`` interrupts every prompt still open, joins its thread
    and clears both attributes, so no helper thread outlives its test.
    """

    def __init__(self, served: _Served) -> None:
        """Stage on ``served``'s worker and store."""
        self._served = served
        self._askers: list[_Asker] = []
        self._coordinators: list[WorkerPassCoordinator] = []

    def job(self, state: JobState, *, owner: str | None = _OWNER) -> str:
        """
        Create a row owned by ``owner`` in ``state``, as the worker's current job.

        Args:
            state: The row's state.
            owner: The owner token the row records; None for an unowned job.

        Returns:
            The job's id.

        """
        store = self._served.job_store
        job = store.create_job(profile=FLATBED, title="Multi-page", owner_token=owner)
        store.update_state(job.id, state)
        services_of(self._served.app).worker._current_job_id = job.id
        return job.id

    def prompt(self, prompt: PassPrompt, *, owner: str | None = _OWNER) -> _Waiting:
        """
        Stage a job waiting on ``prompt``, with the prompt published.

        Args:
            prompt: The open question.
            owner: The owner token the row records; None for an unowned job.

        Returns:
            The staged job, its prompt, its coordinator and the asking thread.

        """
        job_id = self.job(pass_wait_state(prompt.wait), owner=owner)
        coordinator = WorkerPassCoordinator(job_id, stopping=threading.Event())
        self._coordinators.append(coordinator)
        services_of(self._served.app).worker._pass_coordinator = coordinator
        asker = _Asker(coordinator, prompt)
        self._askers.append(asker)
        assert poll_until(lambda: coordinator.open_prompt == prompt, _BUDGET), (
            "the prompt was never published"
        )
        return _Waiting(
            job_id=job_id, prompt=prompt, coordinator=coordinator, asker=asker
        )

    def close(self) -> None:
        """End every open prompt and unbind the worker's staged job."""
        for coordinator in self._coordinators:
            coordinator.interrupt_for_shutdown()
        for asker in self._askers:
            asker.join()
        worker = services_of(self._served.app).worker
        worker._pass_coordinator = None
        worker._current_job_id = None


@pytest.fixture
def stager(served: _Served) -> Iterator[_Stager]:
    """Present the owner's cookie, and stage waiting jobs that are cleaned up."""
    served.client.cookies.set(OWNER_COOKIE, _OWNER)
    staged = _Stager(served)
    try:
        yield staged
    finally:
        staged.close()


def _status(served: _Served) -> str:
    """Return the status poll's whole response body."""
    response = served.client.get("/api/jobs/current/status")
    assert response.status_code == 200
    return response.text


def _status_area(text: str) -> str:
    """Return the status area of a status response, without the Scan button."""
    button = _SCAN_BUTTON.search(text)
    assert button is not None, text
    return text[: button.start()]


def _buttons(area: str) -> list[tuple[str, str]]:
    """Return each button's attributes and stripped label, in page order."""
    return [
        (match.group("attrs"), match.group("text").strip())
        for match in _BUTTON.finditer(area)
    ]


def _button_id(attrs: str) -> str:
    """Return the id a button's attributes carry."""
    match = _BUTTON_ID.search(attrs)
    assert match is not None, attrs
    return match.group("id")


def _attribute_names(attrs: str) -> set[str]:
    """Return the attribute names in one tag's attribute text."""
    return set(_ATTRIBUTE_NAME.findall(attrs))


def _vals(attrs: str) -> dict[str, object]:
    """Return the values a button posts, decoded from its ``hx-vals``."""
    match = _HX_VALS.search(attrs)
    assert match is not None, attrs
    decoded: dict[str, object] = json.loads(html.unescape(match.group("vals")))
    return decoded


def _answer(
    served: _Served, job_id: str, number: int | str, answer: PassAnswer | str
) -> str:
    """Post one answer as a prompt button would, and return the 200 body."""
    response = served.client.post(
        _ANSWER_ROUTE,
        data={"job_id": job_id, "prompt": str(number), "answer": str(answer)},
    )
    assert response.status_code == 200, response.text
    return response.text


def _waiting_line(state: JobState) -> str:
    """Return the plain line the owner sees before the question is published."""
    return f"<p>{escape(progress_label(state))}</p>"


def _non_owner_line(waiting: _Waiting) -> str:
    """
    Return the plain line anyone but the prompt's owner sees for ``waiting``.

    The question is published, so the worker knows when it gives up, and the
    line names that time.
    """
    deadline = waiting.coordinator.open_deadline
    assert deadline is not None
    state = pass_wait_state(waiting.prompt.wait)
    return f"<p>{escape(non_owner_wait_line(state, deadline=deadline))}</p>"


class TestThePromptTheOwnerSees:
    """The owner of a waiting job sees its open question and its buttons."""

    def test_the_next_page_prompt_offers_four_answers_in_order(
        self, served: _Served, stager: _Stager
    ) -> None:
        """
        It offers Scan next page, Finish document, Re-scan, Abort scan, in order.

        Only Abort confirms: the others are the loop's normal moves.
        """
        waiting = stager.prompt(_next_pass(kept=4))
        copy = pass_prompt_copy(
            waiting.prompt, deadline=waiting.coordinator.open_deadline
        )

        area = _status_area(_status(served))

        assert 'class="pages-prompt"' in area
        buttons = _buttons(area)
        assert [_button_id(attrs) for attrs, _ in buttons] == [
            "mp-next",
            "mp-finish",
            "mp-rescan",
            "mp-abort",
        ]
        assert [label for _, label in buttons] == [label for _, label in copy.buttons]
        for (attrs, _), answer in zip(
            buttons,
            [PassAnswer.NEXT, PassAnswer.FINISH, PassAnswer.RESCAN, PassAnswer.ABORT],
            strict=True,
        ):
            assert f'hx-post="{_ANSWER_ROUTE}"' in attrs
            assert 'hx-target="#status-area"' in attrs
            assert 'hx-swap="outerHTML"' in attrs
            assert _vals(attrs) == {
                "job_id": waiting.job_id,
                "prompt": waiting.prompt.number,
                "answer": answer.value,
            }
        assert _HX_CONFIRM.findall(area) == [str(escape(abort_question(4)))]
        assert "hx-confirm" in buttons[3][0]
        assert "disabled" not in _attribute_names(buttons[1][0])

        assert copy.instruction is not None
        assert "<p><strong>4 pages kept so far.</strong></p>" in area
        assert f"<p><strong>{escape(copy.headline)}</strong></p>" in area
        assert f"<p>{escape(copy.instruction)}</p>" in area
        for note in copy.notes:
            assert f'<small class="prompt-note">{escape(note)}</small>' in area
        for forbidden in ("aria-busy", 'role="group"', "<details", "aria-disabled"):
            assert forbidden not in area

    def test_finish_is_disabled_with_its_reason_when_nothing_is_kept(
        self, served: _Served, stager: _Stager
    ) -> None:
        """Finish carries a real ``disabled``, pointing at the visible reason."""
        stager.prompt(_next_pass(kept=0))

        area = _status_area(_status(served))

        finish = next(
            attrs for attrs, _ in _buttons(area) if _button_id(attrs) == "mp-finish"
        )
        assert "disabled" in _attribute_names(finish)
        assert 'aria-describedby="finish-blocked-reason"' in finish
        assert (
            '<small id="finish-blocked-reason" class="prompt-note">'
            f"{escape(NOTHING_TO_FINISH)}</small>"
        ) in area
        assert "aria-disabled" not in area

    def test_the_blank_page_prompt_offers_skip_keep_and_rescan(
        self, served: _Served, stager: _Stager
    ) -> None:
        """It offers three answers, Skip first, with no Abort and no confirmation."""
        waiting = stager.prompt(_blank())
        copy = pass_prompt_copy(waiting.prompt)

        area = _status_area(_status(served))

        buttons = _buttons(area)
        assert [_button_id(attrs) for attrs, _ in buttons] == [
            "mp-skip",
            "mp-keep",
            "mp-rescan",
        ]
        assert [_vals(attrs)["answer"] for attrs, _ in buttons] == [
            "SKIP_BLANKS",
            "KEEP_BLANKS",
            "RESCAN",
        ]
        assert "mp-abort" not in area
        assert "hx-confirm" not in area
        assert f"<p><strong>{escape(copy.headline)}</strong></p>" in area

    def test_the_failed_pass_prompt_shows_the_scrubbed_error(
        self, served: _Served, stager: _Stager
    ) -> None:
        """
        The owner reads what the scanner said, with no host path in it.

        The prompt is Scan again, Finish, Abort: the failed pass added nothing,
        so there is nothing to re-scan.
        """
        settings = services_of(served.app).settings
        kept_file = Path(settings.output.data_dir) / "failed" / "pass-3.pnm"
        waiting = stager.prompt(_retry(f"Could not write {kept_file}"))
        copy = pass_prompt_copy(waiting.prompt)

        area = _status_area(_status(served))

        assert copy.alert is not None
        assert f'<p class="status-fallback">&#9888; {escape(copy.alert)}</p>' in area
        assert (
            '<p class="prompt-note">The scanner reported: '
            "Could not write failed/pass-3.pnm</p>"
        ) in area
        assert str(kept_file) not in area
        assert str(settings.output.data_dir) not in area
        assert copy.instruction is not None
        assert (
            f"<p><strong>{escape(copy.headline)}</strong> "
            f"{escape(copy.instruction)}</p>"
        ) in area
        assert [_button_id(attrs) for attrs, _ in _buttons(area)] == [
            "mp-retry",
            "mp-finish",
            "mp-abort",
        ]
        assert "mp-rescan" not in area
        assert "<details" not in area

    def test_the_retry_button_id_follows_the_question_not_the_row(
        self, served: _Served, stager: _Stager
    ) -> None:
        """
        A row that lags the open question still gives Scan again its own id.

        A waiting-state write the worker could not make leaves the row on the
        previous question.  The buttons are the open question's, so their ids
        must be too, or keyboard focus is lost across the next swap.
        """
        waiting = stager.prompt(_retry(_DEVICE_ERROR))
        served.job_store.update_state(waiting.job_id, JobState.AWAITING_NEXT_PASS)

        area = _status_area(_status(served))

        assert [_button_id(attrs) for attrs, _ in _buttons(area)] == [
            "mp-retry",
            "mp-finish",
            "mp-abort",
        ]

    @pytest.mark.parametrize("wait", list(PassWait))
    def test_owner_pass_prompt_names_the_job_and_deadline(
        self, served: _Served, stager: _Stager, wait: PassWait
    ) -> None:
        """
        The prompt opens with the document's title and says when it gives up.

        The deadline replaces the duration inside the one timeout note the
        question already had, so the prompt says it once, not twice.
        """
        waiting = stager.prompt(_prompt_for(wait))
        deadline = waiting.coordinator.open_deadline
        assert deadline is not None

        area = _status_area(_status(served))

        heading = pass_heading("Multi-page")
        assert heading == "Adding pages to “Multi-page”"
        assert (
            f'<div class="pages-prompt">\n  <p><strong>{escape(heading)}</strong></p>'
        ) in area
        assert area.count(f"No answer by {local_time(deadline)}") == 1
        assert "No answer within" not in area
        assert area.count("No answer") == 1

    @pytest.mark.parametrize("state", sorted(PASS_WAIT_STATES))
    def test_an_owner_before_the_prompt_opens_sees_the_waiting_line(
        self, served: _Served, stager: _Stager, state: JobState
    ) -> None:
        """
        The row is waiting but no prompt is published yet: one plain line.

        It carries no spinner: the job waits for a person, not the machine.
        """
        stager.job(state)

        area = _status_area(_status(served))

        assert _waiting_line(state) in area
        assert "<button" not in area
        assert "aria-busy" not in area


class TestWhatEveryoneElseSees:
    """Another browser sees that the job waits, and nothing it could answer."""

    def test_an_unowned_job_is_answerable_but_its_error_text_is_nobodys(
        self, served: _Served, stager: _Stager
    ) -> None:
        """
        Anyone may answer a job that recorded no owner, but not read its detail.

        Answering follows the answering rule; the scanner's error text is the
        job's detail, and follows the rule the rest of the job view uses.
        """
        stager.prompt(_retry(_DEVICE_ERROR), owner=None)

        area = _status_area(_status(served))

        assert [_button_id(attrs) for attrs, _ in _buttons(area)] == [
            "mp-retry",
            "mp-finish",
            "mp-abort",
        ]
        assert "The scanner reported" not in area
        assert "/dev/bus/usb" not in area
        assert "device I/O" not in area

    @pytest.mark.parametrize("wait", list(PassWait))
    @pytest.mark.parametrize("presented", [_STRANGER, None], ids=["stranger", "none"])
    def test_no_buttons_no_error_text_and_no_spinner(
        self, served: _Served, stager: _Stager, wait: PassWait, presented: str | None
    ) -> None:
        """The buttons are not emitted at all, never hidden with CSS."""
        waiting = stager.prompt(_prompt_for(wait))
        served.client.cookies.clear()
        if presented is not None:
            served.client.cookies.set(OWNER_COOKIE, presented)

        area = _status_area(_status(served))

        assert _non_owner_line(waiting) in area
        assert "Multi-page" not in area
        assert "<button" not in area
        assert "pages-prompt" not in area
        assert "aria-busy" not in area
        assert "The scanner reported" not in area
        assert "/dev/bus/usb" not in area


@dataclass(frozen=True)
class _Dropped:
    """One answer the route must drop, and how it is sent."""

    kept: int = 4
    number_offset: int = 0
    answer: PassAnswer = PassAnswer.NEXT
    presented: str = _OWNER
    job_id: str | None = None


class TestTheAnswerRoute:
    """``POST /api/multi-page/answer`` claims only the owner's offered answer."""

    @pytest.mark.parametrize(
        ("prompt", "answer"),
        [
            (_next_pass(), PassAnswer.NEXT),
            (_next_pass(), PassAnswer.FINISH),
            (_blank(), PassAnswer.SKIP_BLANKS),
            (_retry(_DEVICE_ERROR), PassAnswer.ABORT),
        ],
        ids=["next", "finish", "skip", "abort-after-failure"],
    )
    def test_the_owners_answer_reaches_the_prompt_and_is_acknowledged(
        self,
        served: _Served,
        stager: _Stager,
        prompt: PassPrompt,
        answer: PassAnswer,
    ) -> None:
        """
        The worker's wait returns the answer; the page says what happens next.

        The row still reads as waiting when the response is rendered, so the
        acknowledgement is what stops the buttons coming back as though the
        click did nothing.
        """
        waiting = stager.prompt(prompt)

        text = _answer(served, waiting.job_id, prompt.number, answer)

        assert waiting.asker.result() is answer
        area = _status_area(text)
        assert f'<p class="busy-line">{escape(pass_answer_label(answer))}</p>' in area
        assert "aria-busy" not in area
        assert "<button" not in area
        assert 'id="status-message"' not in text

    def test_a_second_click_still_acknowledges_the_first(
        self, served: _Served, stager: _Stager
    ) -> None:
        """The repeat is dropped, and the page shows the answer that won."""
        waiting = stager.prompt(_next_pass())

        _answer(served, waiting.job_id, waiting.prompt.number, PassAnswer.NEXT)
        text = _answer(served, waiting.job_id, waiting.prompt.number, PassAnswer.ABORT)

        assert waiting.asker.result() is PassAnswer.NEXT
        area = _status_area(text)
        label = escape(pass_answer_label(PassAnswer.NEXT))
        assert f'<p class="busy-line">{label}</p>' in area
        assert "aria-busy" not in area
        assert "<button" not in area

    @pytest.mark.parametrize(
        "case",
        [
            _Dropped(number_offset=-1),
            _Dropped(kept=0, answer=PassAnswer.FINISH),
            _Dropped(answer=PassAnswer.TIMED_OUT),
            _Dropped(answer=PassAnswer.INTERRUPTED),
            _Dropped(presented=_STRANGER),
            _Dropped(job_id="no-such-job"),
        ],
        ids=[
            "stale-prompt",
            "finish-with-nothing-kept",
            "timed-out",
            "interrupted",
            "not-the-owner",
            "unknown-job",
        ],
    )
    def test_a_dropped_answer_rerenders_the_status_area_only(
        self, served: _Served, stager: _Stager, case: _Dropped
    ) -> None:
        """A dropped answer gets the current status area, no message, prompt open."""
        waiting = stager.prompt(_next_pass(kept=case.kept))
        served.client.cookies.set(OWNER_COOKIE, case.presented)

        text = _answer(
            served,
            case.job_id or waiting.job_id,
            waiting.prompt.number + case.number_offset,
            case.answer,
        )

        assert 'id="status-message"' not in text
        assert waiting.coordinator.open_prompt == waiting.prompt
        area = _status_area(text)
        assert "aria-busy" not in area
        if case.presented == _OWNER:
            assert 'class="pages-prompt"' in area
        else:
            assert "<button" not in area
            assert _non_owner_line(waiting) in area

    @pytest.mark.parametrize(
        ("number", "answer"),
        [("3", "BOGUS"), ("0", "NEXT"), ("-1", "NEXT"), ("three", "NEXT")],
        ids=["unknown-answer", "prompt-zero", "prompt-negative", "prompt-not-a-number"],
    )
    def test_an_invalid_value_is_refused(
        self, served: _Served, stager: _Stager, number: str, answer: str
    ) -> None:
        """A value no button sends never reaches the worker."""
        waiting = stager.prompt(_next_pass(number=3))

        response = served.client.post(
            _ANSWER_ROUTE,
            data={"job_id": waiting.job_id, "prompt": number, "answer": answer},
        )

        assert response.status_code == 422
        assert waiting.coordinator.open_prompt == waiting.prompt

    def test_the_answer_log_never_carries_the_owner_token(
        self,
        served: _Served,
        stager: _Stager,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Ids, the prompt number and the answer are logged; the token is not."""
        waiting = stager.prompt(_next_pass())
        caplog.set_level("DEBUG")

        _answer(served, waiting.job_id, waiting.prompt.number, PassAnswer.NEXT)

        assert waiting.asker.result() is PassAnswer.NEXT
        assert waiting.job_id in caplog.text
        assert _OWNER not in caplog.text


class TestTheScanButtonAndBusyLine:
    """The Scan button and the busy line around a multi-page job."""

    @pytest.mark.parametrize("state", sorted(PASS_WAIT_STATES))
    def test_the_scan_button_waits_for_you(
        self, served: _Served, stager: _Stager, state: JobState
    ) -> None:
        """The Scan button is disabled, captioned for the person, with no spinner."""
        stager.job(state)

        button = _SCAN_BUTTON.search(_status(served))

        assert button is not None
        assert button.group("text").strip() == "Waiting for you…"
        assert "disabled" in _attribute_names(button.group("attrs"))
        assert "aria-busy" not in button.group("attrs")

    def test_the_flip_caption_is_unchanged(
        self, served: _Served, stager: _Stager
    ) -> None:
        """A manual-duplex flip says it is waiting for the flip."""
        stager.job(JobState.AWAITING_FLIP)

        button = _SCAN_BUTTON.search(_status(served))

        assert button is not None
        assert button.group("text").strip() == "Waiting for flip…"

    def test_a_later_pass_leads_with_the_pages_kept(
        self, served: _Served, stager: _Stager
    ) -> None:
        """While the next pass scans, the busy line says how many pages are kept."""
        waiting = stager.prompt(_next_pass(kept=4))
        assert waiting.coordinator.answer(waiting.prompt.number, PassAnswer.NEXT)
        assert waiting.asker.result() is PassAnswer.NEXT
        served.job_store.update_state(waiting.job_id, JobState.SCANNING)
        assert services_of(served.app).worker.pages_kept == 4

        area = _status_area(_status(served))

        line = f"4 pages so far · {progress_label(JobState.SCANNING)}"
        assert busy_line(JobState.SCANNING, pages_kept=4) == line
        assert f'<p class="busy-line">{line}</p>' in area


class TestTheFlipBranchKeepsItsPrompt:
    """A manual-duplex flip renders the flip prompt, not the multi-page one."""

    def test_the_flip_owner_still_sees_the_flip_prompt(
        self, served: _Served, stager: _Stager
    ) -> None:
        """The flip branch renders the flip prompt, and not the multi-page one."""
        stager.job(JobState.AWAITING_FLIP)

        area = _status_area(_status(served))

        assert "flip-prompt" in area
        assert "pages-prompt" not in area


# The button each question's first appearance puts focus on: the first answer
# it offers of Scan next, Retry and Skip.
_PRIMARY_BUTTON = {
    PassWait.NEXT_PASS: "mp-next",
    PassWait.RETRY: "mp-retry",
    PassWait.BLANK_DECISION: "mp-skip",
}
_AUTOFOCUS = re.compile(r"\sautofocus\b")
_STATUS_AREA_OPEN = re.compile(r'<div id="status-area"[^>]*>', re.DOTALL)


def _focused_ids(markup: str) -> list[str]:
    """Return the id of every button in ``markup`` that carries ``autofocus``."""
    return [
        _button_id(attrs)
        for attrs, _ in _buttons(markup)
        if _AUTOFOCUS.search(attrs) is not None
    ]


def _poll_url_of(markup: str) -> str:
    """Return the URL the status area polls, as the browser would request it."""
    opening = _STATUS_AREA_OPEN.search(markup)
    assert opening is not None, markup
    url = re.search(r'hx-get="([^"]*)"', opening.group(0))
    assert url is not None, opening.group(0)
    return html.unescape(url.group(1))


class TestFocus:
    """
    A new question puts focus on its primary button, for its owner only.

    It is carried by the owner's poll rendering, and reaches the browser only
    when that rendering changes -- when the question first appears -- because
    an unchanged poll is answered 204.  The full page never carries it.
    """

    @pytest.mark.parametrize("wait", list(PassWait))
    def test_owner_pass_prompt_autofocuses_its_primary_button(
        self, served: _Served, stager: _Stager, wait: PassWait
    ) -> None:
        """Exactly one button takes focus: Scan next, Retry or Skip."""
        stager.prompt(_prompt_for(wait))

        text = _status(served)

        assert _focused_ids(text) == [_PRIMARY_BUTTON[wait]]
        opening = _STATUS_AREA_OPEN.search(text)
        assert opening is not None
        assert _AUTOFOCUS.search(opening.group(0)) is None

    def test_nothing_kept_still_focuses_scan_next(
        self, served: _Served, stager: _Stager
    ) -> None:
        """With nothing kept Finish is disabled; Scan next is offered and focused."""
        stager.prompt(_next_pass(kept=0))

        assert _focused_ids(_status(served)) == ["mp-next"]

    @pytest.mark.parametrize("wait", list(PassWait))
    @pytest.mark.parametrize("presented", [_STRANGER, None], ids=["stranger", "none"])
    def test_non_owner_pass_prompt_is_never_autofocused(
        self, served: _Served, stager: _Stager, wait: PassWait, presented: str | None
    ) -> None:
        """Another browser is sent no question, and so no focus request."""
        stager.prompt(_prompt_for(wait))
        served.client.cookies.clear()
        if presented is not None:
            served.client.cookies.set(OWNER_COOKIE, presented)

        assert "autofocus" not in _status(served)

    @pytest.mark.parametrize("wait", list(PassWait))
    def test_page_render_never_autofocuses_the_pass_prompt(
        self, served: _Served, stager: _Stager, wait: PassWait
    ) -> None:
        """The page shows the owner's open question and moves no focus to it."""
        stager.prompt(_prompt_for(wait))

        page = served.client.get("/").text

        assert f'id="{_PRIMARY_BUTTON[wait]}"' in page
        assert "autofocus" not in page

    @pytest.mark.parametrize("wait", list(PassWait))
    def test_first_poll_after_a_page_with_a_pass_prompt_is_204(
        self, served: _Served, stager: _Stager, wait: PassWait
    ) -> None:
        """The page's token names the poll's rendering, autofocus and all."""
        stager.prompt(_prompt_for(wait))

        page = served.client.get("/").text
        poll = served.client.get(_poll_url_of(page))

        assert poll.status_code == 204, poll.text

    def test_a_claimed_abort_bakes_focus_scan(
        self, served: _Served, stager: _Stager
    ) -> None:
        """
        Abort sends focus to the Scan button, as the flip prompt's Abort does.

        The response focuses the status area while the scan winds down and
        its poll carries ``focus=scan`` until the Scan button is enabled.
        """
        waiting = stager.prompt(_next_pass())

        text = _answer(served, waiting.job_id, waiting.prompt.number, PassAnswer.ABORT)

        assert waiting.asker.result() is PassAnswer.ABORT
        opening = _STATUS_AREA_OPEN.search(text)
        assert opening is not None
        assert _AUTOFOCUS.search(opening.group(0)) is not None
        assert "focus=scan" in _poll_url_of(text)

    def test_any_other_claimed_answer_bakes_no_focus_scan(
        self, served: _Served, stager: _Stager
    ) -> None:
        """Scan next leaves the next question's own button to take focus."""
        waiting = stager.prompt(_next_pass())

        text = _answer(served, waiting.job_id, waiting.prompt.number, PassAnswer.NEXT)

        assert waiting.asker.result() is PassAnswer.NEXT
        assert "focus=" not in _poll_url_of(text)
