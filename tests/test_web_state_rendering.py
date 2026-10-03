"""
Every job state renders the markup the web templates promise for it.

The templates are the one surface neither ``ty`` nor ``pyrefly`` can see, and
with the state checks behind ``Job.is_active`` / ``Job.is_busy`` and the
``job_label`` / ``progress_label`` filters, nothing else pins *which* state
produces *which* markup.  These tests pin the mapping for every ``JobState``
member.

Every case is parametrised over ``list(JobState)`` rather than a hand-written
list of names, so an eighth member cannot be added without forcing a decision
here -- the same discipline ``tests/test_vocabulary.py`` applies to the lookup
functions themselves.
"""

from __future__ import annotations

import ast
import html
import json
import re
import sqlite3
import threading
import time
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, override
from urllib.parse import parse_qs, urlsplit

import jinja2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jinja2 import nodes
from markupsafe import escape

from saneless.config import (
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
    WebConfig,
)
from saneless.job import JobResult
from saneless.scanner.base import DeviceInfo
from saneless.vocabulary import (
    ACTIVE_STATES,
    BUSY_STATES,
    CORRESPONDENTS_LOADING,
    HIDDEN_JOB_TITLE,
    HIDDEN_WARNING_LINE,
    NO_SCRIPT_LINE,
    PASS_WAIT_STATES,
    SCAN_BLOCKED_REASON,
    SCAN_BLOCKED_URL_REASON,
    TAG_FILTER_LABEL,
    TERMINAL_STATES,
    TITLE_MAX_LENGTH,
    UNCONFIRMED_FILING_LABEL,
    UNCONFIRMED_SEND_LABEL,
    ErrorCategory,
    FlipOutcome,
    JobState,
    PassAnswer,
    PassWait,
    RequestRejection,
    SubmitResult,
    error_message,
    error_next_step,
    flip_answer_label,
    flip_deadline_note,
    flip_heading,
    job_label,
    job_status_class,
    local_time,
    non_owner_wait_line,
    page_counts,
    page_title,
    pass_wait_state,
    progress_label,
    rejection_message,
    state_label,
)
from saneless.web import app as app_module
from saneless.web.app import create_app
from saneless.web.routes import CHECK_AGAIN_WAIT_SECONDS, TAG_FILTER_MAX_LENGTH
from saneless.worker import WorkerFlipCoordinator
from tests.conftest import (
    StubScannerBackend,
    load_the_lists,
    poll_until,
    quiet_window,
)
from tests.template_support import template_markup, template_start_tags

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    import httpx2

    from saneless.job import JobStore


class _StubScanner(StubScannerBackend):
    """
    The shared stub backend, but reporting one device instead of none.

    Only ``get_devices`` differs: the profile dropdown and the worker's
    startup profile generation both read it, and a device list of one
    is what these tests render against.  Capabilities and ``scan_pages`` come
    straight from ``StubScannerBackend``; these tests never run a scan.
    """

    def get_devices(self) -> list[DeviceInfo]:
        """
        Report a single fake device.

        Returns:
            A one-element list naming the device the settings point at.

        """
        return [
            DeviceInfo(
                name="test:device:001",
                vendor="Test",
                model="Stub",
                device_type="virtual",
            ),
        ]


# The log file every app in this module writes to. A host filesystem path must
# never reach a LAN-visible page, so this name is deliberately unlike anything a
# template could produce by accident.
_LOG_FILE_NAME = "render-test-do-not-render-me.log"

# The category `_job_in_state` records on an ERROR row unless a test asks for
# another. Production never writes an ERROR row without one -- the worker always
# classifies (`worker.py`) -- so this, not NULL, is the shape the status area's
# main path renders. REJECTED is avoided because it has its own routing.
_DEFAULT_ERROR_CATEGORY = ErrorCategory.SCANNER

# The two failures that may already be in paperless-ngx.  They render amber
# with no alert, so the red-alert tests below cover every other category, and
# the amber ones are pinned by their own class.  Written out rather than read
# from ``is_amber_category`` so the set is pinned, not echoed.
_AMBER_CATEGORIES = frozenset(
    {ErrorCategory.UNCONFIRMED_SEND, ErrorCategory.UNCONFIRMED_FILING}
)

# The browser the rendering tests look through, and the owner every row they
# stage records.  A job's title, preview and detail text reach only the
# browser that started it, so a rendering test that means to see them has to
# be that browser; the tests about what anyone else sees name their own
# tokens below.
_RENDERING_BROWSER = "the-browser-these-rows-were-rendered-for"

# The templates and the stylesheet, located the way the app locates them, so a
# moved package cannot make a source assertion pass on an empty file.
_PACKAGE_DIR = Path(app_module.__file__).parent
_TEMPLATES_DIR = _PACKAGE_DIR / "templates"
_APP_CSS = _PACKAGE_DIR / "static" / "app.css"


class _ScanButton(NamedTuple):
    """
    The Scan button as one response renders it.

    Captured whole, so attribute and text assertions cannot be satisfied by
    markup somewhere else on the page.
    """

    attributes: dict[str, str | None]
    """The opening tag's attributes, in whatever order they are written."""
    text: str
    """The markup between the opening and the closing tag."""
    markup: str
    """The whole element, from its opening tag to its closing tag."""
    start: int
    """Where the opening tag starts in the response."""


class _ScanButtonFinder(HTMLParser):
    """Count the elements carrying the Scan button's id and locate each button."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.ids = 0
        self.buttons: list[
            tuple[tuple[int, int], str, dict[str, str | None], tuple[int, int]]
        ] = []
        self._open: tuple[tuple[int, int], str, dict[str, str | None]] | None = None

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if attributes.get("id") != "scan-btn":
            return
        self.ids += 1
        opening = self.get_starttag_text()
        if tag == "button" and opening is not None:
            self._open = (self.getpos(), opening, attributes)

    @override
    def handle_endtag(self, tag: str) -> None:
        if tag == "button" and self._open is not None:
            position, opening, attributes = self._open
            self.buttons.append((position, opening, attributes, self.getpos()))
            self._open = None


def _offset(markup: str, position: tuple[int, int]) -> int:
    """
    Convert a parser's line and column into an offset into ``markup``.

    Args:
        markup: The text the parser read.
        position: The parser's one-based line and zero-based column.

    Returns:
        The same place as an index into ``markup``.

    """
    line, column = position
    return sum(len(text) + 1 for text in markup.split("\n")[: line - 1]) + column


def _scan_buttons(markup: str) -> tuple[int, list[_ScanButton]]:
    """
    Parse every Scan button out of a response.

    Args:
        markup: The rendered response body.

    Returns:
        How many elements carry ``id="scan-btn"``, and each Scan button.

    """
    finder = _ScanButtonFinder()
    finder.feed(markup)
    finder.close()
    buttons: list[_ScanButton] = []
    for opened, opening, attributes, closed in finder.buttons:
        start = _offset(markup, opened)
        body = start + len(opening)
        close = _offset(markup, closed)
        end = markup.index(">", close) + 1
        buttons.append(
            _ScanButton(attributes, markup[body:close], markup[start:end], start)
        )
    return finder.ids, buttons


def _scan_button(markup: str) -> _ScanButton | None:
    """Return the first Scan button in ``markup``, or None when there is none."""
    _ids, buttons = _scan_buttons(markup)
    return buttons[0] if buttons else None


class _StartTagReader(HTMLParser):
    """Collect every start tag of a rendered response with its attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def _rendered_start_tags(markup: str) -> list[tuple[str, dict[str, str | None]]]:
    """Return the start tags of a rendered response, in document order."""
    reader = _StartTagReader()
    reader.feed(markup)
    reader.close()
    return reader.tags


def _template_tags(*parts: str) -> list[tuple[str, dict[str, str | None]]]:
    """Return the start tags of the template at ``parts`` under the templates."""
    return template_start_tags(_TEMPLATES_DIR.joinpath(*parts))


def _all_template_tags() -> list[tuple[Path, str, dict[str, str | None]]]:
    """Return every start tag of every template, with the template it is in."""
    return [
        (path, tag, attributes)
        for path in sorted(_TEMPLATES_DIR.rglob("*.html"))
        for tag, attributes in template_start_tags(path)
    ]


def _template_tree(path: Path) -> nodes.Template:
    """
    Parse a template into Jinja's syntax tree.

    Comments never reach the tree, so a check that walks it sees only the
    expressions and statements the template evaluates.
    """
    return jinja2.Environment(autoescape=True).parse(path.read_text(encoding="utf-8"))


def _template_names(tree: nodes.Template) -> list[str]:
    """Return every name, attribute, filter and string constant a template uses."""
    found: list[str] = []
    for node in tree.find_all((nodes.Name, nodes.Getattr, nodes.Filter, nodes.Const)):
        if isinstance(node, (nodes.Name, nodes.Filter)):
            found.append(node.name)
        elif isinstance(node, nodes.Getattr):
            found.append(node.attr)
        elif isinstance(node, nodes.Const) and isinstance(node.value, str):
            found.append(node.value)
    return found


def _job_attribute(node: nodes.Node) -> str | None:
    """Return ``attr`` when ``node`` is ``job.attr``, and None otherwise."""
    if (
        isinstance(node, nodes.Getattr)
        and isinstance(node.node, nodes.Name)
        and node.node.name == "job"
    ):
        return node.attr
    return None


def _conditions(tree: nodes.Template) -> list[nodes.Node]:
    """Return the test of every ``if``, ``elif`` and inline ``if`` in a template."""
    tests: list[nodes.Node] = [node.test for node in tree.find_all(nodes.If)]
    tests.extend(node.test for node in tree.find_all(nodes.CondExpr))
    return tests


def _truth_tested_job_attributes(test: nodes.Node) -> list[str]:
    """
    Return the ``job`` attributes a condition tests for truthiness.

    An attribute counts when the condition is the attribute itself, its
    negation, or one side of an ``and`` or ``or``; a comparison, a test or a
    filter applied to it does not.
    """
    attribute = _job_attribute(test)
    if attribute is not None:
        return [attribute]
    if isinstance(test, nodes.Not):
        return _truth_tested_job_attributes(test.node)
    if isinstance(test, (nodes.And, nodes.Or)):
        return [
            *_truth_tested_job_attributes(test.left),
            *_truth_tested_job_attributes(test.right),
        ]
    return []


_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_CSS_BLOCK = re.compile(r"(?P<selector>[^{}]*)\{(?P<body>[^{}]*)\}")


def _css_rules(css: str) -> list[tuple[str, str]]:
    """
    Return every innermost rule of a stylesheet, with its comments removed.

    Args:
        css: The stylesheet's text.

    Returns:
        Each rule's selector, whitespace normalised, and its body.

    """
    return [
        (" ".join(block.group("selector").split()), block.group("body"))
        for block in _CSS_BLOCK.finditer(_CSS_COMMENT.sub("", css))
    ]


def _declarations(body: str) -> dict[str, str]:
    """Return a rule body's declarations as property names and their values."""
    pairs = [part.split(":", 1) for part in body.split(";") if ":" in part]
    return {name.strip(): value.strip() for name, value in pairs}


# The paperless-ngx credential a configured appliance has. Anything outside
# `config.PLACEHOLDER_TOKENS` counts as real: the predicate is a fixed literal
# set and never a shape heuristic.
_REAL_CREDENTIAL = "test-token"

# The shipped stand-in the predicate refuses -- docker-compose.yml and the
# docker reference both carry it, so it is the placeholder a real installation
# is most likely to be left with.
_SHIPPED_PLACEHOLDER = "changeme"


def _make_app(
    tmp_path: Path,
    *,
    credential: str = _REAL_CREDENTIAL,
    url: str = "http://paperless.invalid",
    show_lists: bool = True,
) -> FastAPI:
    """
    Build a real app with a stub scanner and no network calls, not yet started.

    Args:
        tmp_path: The directory the app writes its database and files under.
        credential: The paperless-ngx token this appliance is configured with.
            The default is a real one; pass `_SHIPPED_PLACEHOLDER` for the
            blocked-Scan-button case.
        url: The paperless-ngx address; empty means unset, which blocks the
            Scan button too.
        show_lists: Whether the form shows the tag list and the correspondent
            select.  With neither, the page waits for no list, so its Scan
            button answers to the job and the appliance alone.

    Returns:
        The app, whose lifespan (and so its worker) starts with its TestClient.

    """
    settings = Settings(
        scanner=ScannerConfig(device="test:device:001"),
        paperless=PaperlessConfig(url=url, token=credential),
        output=OutputConfig(
            tmp_dir=str(tmp_path),
            data_dir=str(tmp_path),
            # Named distinctively so `test_no_log_path_reaches_the_rendered_page` can
            # search the rendered markup for it and mean something: the default
            # would be a path fragment that could collide with tmp_path itself.
            log_file=str(tmp_path / _LOG_FILE_NAME),
        ),
        # Two profiles, so the set is not the bare default.  With only
        # ``default``, the worker's startup generation would build
        # profiles from _StubScanner's device and swap them in while these
        # requests read the dropdown -- generation these tests do not intend.
        profiles={
            "default": ProfileConfig(),
            "duplex": ProfileConfig(source="ADF Duplex"),
        },
        web=WebConfig(show_tags=show_lists, show_correspondent=show_lists),
    )
    app = create_app(settings, _StubScanner())
    app.state.paperless.get_tags = _no_rows
    app.state.paperless.get_correspondents = _no_rows
    return app


def _no_rows(
    *, timeout: float | httpx2.Timeout | None = None
) -> list[dict[str, object]]:
    """
    Stand in for a paperless-ngx list that answers with no rows.

    Keyword-only ``timeout``, as the real client takes it: the lazy list load
    fetches with the short request budget.

    Args:
        timeout: The per-request budget, ignored.

    Returns:
        An empty list.

    """
    del timeout
    return []


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """
    TestClient over a real app with a stub scanner and no network calls.

    It presents the owner token every row this module stages records, so the
    status area and history render each row as its owner sees it.
    """
    with TestClient(_make_app(tmp_path)) as tc:
        tc.cookies.set(_OWNER_COOKIE, _RENDERING_BROWSER)
        yield tc


@pytest.fixture
def listless_client(tmp_path: Path) -> Iterator[TestClient]:
    """
    Yield the same browser over a form that shows neither list.

    A page with a list on it holds Scan until the lists arrive, whatever the
    job, so a test about what the job alone does to the page's Scan button
    renders a page that waits for nothing.
    """
    with TestClient(_make_app(tmp_path, show_lists=False)) as tc:
        tc.cookies.set(_OWNER_COOKIE, _RENDERING_BROWSER)
        yield tc


@pytest.fixture
def blocked_client(tmp_path: Path) -> Iterator[TestClient]:
    """
    TestClient over an appliance whose paperless-ngx token is a placeholder.

    The verdict is derived from `Settings`, which is read once at process
    start, so a second app is the only honest way to drive the blocked case:
    there is no runtime setter to reach for.
    """
    with TestClient(_make_app(tmp_path, credential=_SHIPPED_PLACEHOLDER)) as tc:
        yield tc


@pytest.fixture
def url_unset_client(tmp_path: Path) -> Iterator[TestClient]:
    """TestClient over an appliance with a real token and no paperless-ngx address."""
    with TestClient(_make_app(tmp_path, url="")) as tc:
        yield tc


def _app(client: TestClient) -> FastAPI:
    """Extract the FastAPI app from a TestClient, helping the type checker."""
    app = client.app
    if not isinstance(app, FastAPI):
        msg = "Expected FastAPI app"
        raise TypeError(msg)
    return app


def _adopt_as_current_job(client: TestClient, job_id: str) -> None:
    """
    Point the live worker at `job_id` without submitting real work.

    This writes ScanWorker._current_job_id directly, which is private. It is the
    single place in this module that does so, deliberately: the routes read the
    current job through the worker, and driving it through the real submit path
    would run a pipeline these rendering tests do not want.

    Safe because no job is ever submitted here, so _process_job's `finally`
    cannot race the assignment. If the worker's job tracking changes, this one
    helper is the only thing that needs updating.
    """
    _app(client).state.worker._current_job_id = job_id


def _set_warning(job_store: JobStore, job_id: str, warning: str) -> None:
    """
    Write the `warning` column directly.

    The status markup interpolates `job.warning`, so the escaping tests need
    a value to escape whatever the row's state.  A single parameterised UPDATE
    stages exactly that, the same kind of deliberate reach-through as
    `_adopt_as_current_job` above.
    """
    with job_store._conn:
        job_store._conn.execute(
            "UPDATE jobs SET warning = ? WHERE id = ?",
            (warning, job_id),
        )


def _job_in_state(
    client: TestClient,
    state: JobState,
    warning: str | None = None,
    error_category: ErrorCategory | None = _DEFAULT_ERROR_CATEGORY,
) -> str:
    """
    Create a job, drive it to `state`, and make it the worker's current job.

    The category is written for every state, exactly as `error` already was:
    only the ERROR branch reads either, so the other states are unaffected, and
    keeping one write means the helper has one shape. Pass
    ``error_category=None`` to get a row recorded with no category, whose
    status area falls back to the specific message alone.

    Returns:
        The job's id, which the technical-details assertions need.

    """
    job_store: JobStore = _app(client).state.job_store
    job = job_store.create_job(
        profile="default", title="Render Test", owner_token=_RENDERING_BROWSER
    )
    job_store.update_state(
        job.id, state, error="disk on fire", error_category=error_category
    )
    if warning is not None:
        _set_warning(job_store, job.id, warning)
    _adopt_as_current_job(client, job.id)
    return job.id


@pytest.mark.parametrize("state", list(JobState))
def test_history_cell_shows_the_shared_label(
    client: TestClient, state: JobState
) -> None:
    """
    Each state reaches `state_label` in the history table.

    This pins the wiring -- which state is routed through which filter -- not
    the label text, because the expectation is built from the same function the
    template calls. The literal strings are pinned separately in
    tests/test_vocabulary.py; the one below stands alone as a spot check.
    """
    _job_in_state(client, state)
    response = client.get("/api/jobs/history")
    assert response.status_code == 200
    assert f">\n    {state_label(state)}\n  </td>" in response.text


@pytest.mark.parametrize("state", list(JobState))
def test_history_cell_css_class(client: TestClient, state: JobState) -> None:
    """Only the four terminal history cells carry a status CSS class."""
    _job_in_state(client, state)
    text = client.get("/api/jobs/history").text
    assert ('<td class="status-done">' in text) is (state is JobState.DONE)
    assert ('<td class="status-error">' in text) is (state is JobState.ERROR)
    assert ('<td class="status-fallback">' in text) is (state is JobState.FALLBACK)
    assert ('<td class="status-cancelled">' in text) is (state is JobState.CANCELLED)


@pytest.mark.parametrize("state", list(JobState))
def test_status_area_polls_only_while_active(
    client: TestClient, state: JobState
) -> None:
    """The 1s status poll is attached for exactly the active states."""
    _job_in_state(client, state)
    text = client.get("/api/jobs/current/status").text
    assert ('hx-trigger="every 1s"' in text) is (state in ACTIVE_STATES)


@pytest.mark.parametrize("state", list(JobState))
def test_status_area_is_always_focusable(client: TestClient, state: JobState) -> None:
    """
    Every rendering of the status area can take focus, in every state.

    It is where focus goes after an action, and htmx restores focus by id
    only to an element that can hold it, so the attribute cannot depend on
    the state or on which route rendered the area.
    """
    job_id = _job_in_state(client, state)
    renderings = [
        client.get("/").text,
        client.get("/api/jobs/current/status").text,
        client.get(f"/api/jobs/{job_id}/status").text,
        client.post("/api/flip/continue", data={"job_id": job_id}).text,
    ]
    for text in renderings:
        opening = _STATUS_OPEN.findall(text)
        assert len(opening) == 1, text
        assert 'tabindex="-1"' in opening[0]


def test_flip_buttons_have_stable_ids(client: TestClient) -> None:
    """
    Continue and Abort carry fixed ids, so focus on either survives a swap.

    htmx moves focus back to the element with the focused element's id after
    an outerHTML swap; a button without an id is lost to ``<body>``.
    """
    _job_in_state(client, JobState.AWAITING_FLIP)

    text = client.get("/api/jobs/current/status").text

    continue_button = re.search(r'<button[^>]*id="flip-continue"[^>]*>', text)
    abort_button = re.search(r'<button[^>]*id="flip-abort"[^>]*>', text)
    assert continue_button is not None
    assert abort_button is not None
    assert 'hx-post="/api/flip/continue"' in continue_button.group(0)
    assert 'hx-post="/api/flip/abort"' in abort_button.group(0)


@pytest.mark.parametrize("state", list(JobState))
def test_status_area_prose(client: TestClient, state: JobState) -> None:
    """
    Each state renders its own status markup.

    As above, the busy line is built from `progress_label`, so it pins routing
    rather than text; the literal spot check below guards the text itself.
    """
    _job_in_state(client, state)
    text = client.get("/api/jobs/current/status").text

    busy_line = f'<p class="busy-line">{progress_label(state)}</p>'
    assert (busy_line in text) is (state in BUSY_STATES)

    # AWAITING_FLIP is active but NOT busy: it shows the flip prompt instead of
    # a progress line, and never renders aria-busy.
    assert ("flip-prompt" in text) is (state is JobState.AWAITING_FLIP)
    if state not in BUSY_STATES:
        assert 'aria-busy="true"' not in text
    # A multi-page prompt needs the worker to hold an open question for the
    # job; a row merely recorded as waiting, as every row here is, has none,
    # so no state renders one.
    assert "pages-prompt" not in text

    if state is JobState.DONE:
        assert '<p class="status-done">&#10003; Done: Render Test</p>' in text
    if state is JobState.ERROR:
        # The paragraph carries the category sentence with no literal `Error: `
        # prefix, because the sentence names the problem itself. `role="alert"`
        # is on a wrapping <div> so the next step is announced too; it is
        # asserted in TestStatusAreaError.
        sentence = escape(error_message(_DEFAULT_ERROR_CATEGORY))
        assert f'<p class="status-error">&#10007; {sentence}</p>' in text
        assert "&#10007; Error: disk on fire" not in text
    if state is JobState.FALLBACK:
        assert (
            '<p class="status-fallback">&#8594; Saved to folder: Render Test</p>'
            in text
        )
        # A fallback is a degradation, not a failure: no role="alert" here.
        assert 'role="alert"' not in text
    if state is JobState.CANCELLED:
        assert '<p class="status-cancelled">&#8856; Cancelled: Render Test</p>' in text
        # A cancel is a deliberate stop, not a failure: no role="alert" here.
        assert 'role="alert"' not in text
    # The history-refresh hook belongs to the terminal states only -- all four
    # of them, FALLBACK and CANCELLED included, or the table goes stale.
    assert ('hx-get="/api/jobs/history"' in text) is (state in TERMINAL_STATES)


@pytest.mark.parametrize("state", sorted(PASS_WAIT_STATES))
def test_multi_page_wait_names_what_it_waits_on(
    client: TestClient, state: JobState
) -> None:
    """
    A multi-page job waiting with no open question says what it waits on.

    The line carries no ``aria-busy``: the job waits for a person, so a
    spinner would make it look hung.
    """
    _job_in_state(client, state)
    text = client.get("/api/jobs/current/status").text

    assert f"<p>{escape(progress_label(state))}</p>" in text
    assert 'aria-busy="true"' not in text


# Every status-area rendering the browser swaps in, by the route that sends it.
# Both polls, the scan submit, the three answer routes, and the fallback a poll
# answers when it cannot read its job.  None of them may carry the persistent
# region: it is on the page once and is never replaced, which is what lets a
# screen reader hear each change inside it.
_STATUS_ROUTES = (
    "current poll",
    "followed poll",
    "scan submit",
    "flip continue",
    "flip abort",
    "multi-page answer",
    "lost-contact fallback",
)
_HX_REQUEST = {"HX-Request": "true"}
_DIV_TAG = re.compile(r"<div\b|</div>")
_STATUS_LIVE_OPEN = '<div id="status-live" role="status">'


def _element(markup: str, element_id: str) -> str:
    """
    Return one ``<div>`` whole, from its open tag to its matching close.

    The div tags after the open tag are counted, so nested divs -- an ERROR's
    alert wrapper, a prompt's button group -- stay inside the result.

    Args:
        markup: The rendered page or response.
        element_id: The id the div carries as its first attribute.

    Returns:
        The element's markup.

    """
    start = markup.index(f'<div id="{element_id}"')
    depth = 0
    for tag in _DIV_TAG.finditer(markup, start):
        depth += 1 if tag.group() == "<div" else -1
        if depth == 0:
            return markup[start : tag.end()]
    pytest.fail(f"#{element_id} is never closed")


def _status_response(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> str:
    """
    Send one status-rendering request and return its body.

    Args:
        client: The client to request through.
        monkeypatch: Breaks the store's reads for the fallback case.
        route: One of ``_STATUS_ROUTES``.

    Returns:
        The response body, after asserting it is a 200.

    """
    if route == "scan submit":
        response = client.post(
            "/api/scan",
            data={"profile": "default", "title": "Region Test"},
            headers=_HX_REQUEST,
        )
        assert response.status_code == 200, response.text
        return response.text
    job_id = _job_in_state(client, JobState.AWAITING_FLIP)
    if route == "lost-contact fallback":
        store: JobStore = _app(client).state.job_store

        def unreadable(*_args: object, **_kwargs: object) -> object:
            msg = "disk I/O error"
            raise sqlite3.OperationalError(msg)

        for name in ("get_job", "latest_run_job"):
            monkeypatch.setattr(store, name, unreadable)
    match route:
        case "current poll":
            response = client.get("/api/jobs/current/status", headers=_HX_REQUEST)
        case "followed poll" | "lost-contact fallback":
            response = client.get(f"/api/jobs/{job_id}/status", headers=_HX_REQUEST)
        case "flip continue" | "flip abort":
            response = client.post(
                f"/api/flip/{route.removeprefix('flip ')}",
                data={"job_id": job_id},
                headers=_HX_REQUEST,
            )
        case _:
            response = client.post(
                "/api/multi-page/answer",
                data={"job_id": job_id, "prompt": "1", "answer": PassAnswer.NEXT},
                headers=_HX_REQUEST,
            )
    assert response.status_code == 200, response.text
    return response.text


def test_the_status_area_sits_in_one_persistent_status_region(
    client: TestClient,
) -> None:
    """
    A fresh page has one polite region, and the status area is all it holds.

    The region is the element a screen reader watches, so it must exist before
    anything inside it changes and must never itself be replaced.  The error
    slot stays the one assertive region, directly above it.
    """
    markup = client.get("/").text

    assert markup.count('role="status"') == 1
    assert markup.count('role="alert"') == 1
    assert re.search(
        r'<div id="status-message" role="alert"></div>\s*'
        + re.escape(_STATUS_LIVE_OPEN),
        markup,
    )
    area = _element(markup, "status-area")
    assert re.fullmatch(
        re.escape(_STATUS_LIVE_OPEN) + r"\s*" + re.escape(area) + r"\s*</div>",
        _element(markup, "status-live"),
    )


@pytest.mark.parametrize("route", _STATUS_ROUTES)
def test_no_status_response_contains_the_live_region(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    """
    Every response swaps the status area inside the region, never the region.

    A response that carried the region would nest a second one inside the
    first, and a region that is itself swapped in is not heard at all.
    """
    text = _status_response(client, monkeypatch, route)

    assert text.count('id="status-area"') == 1
    assert "status-live" not in text
    assert 'role="status"' not in text


@pytest.mark.parametrize("owner", [True, False], ids=["owner", "other-viewer"])
@pytest.mark.parametrize("state", list(JobState))
def test_status_area_carries_no_aria_busy(
    client: TestClient, state: JobState, *, owner: bool
) -> None:
    """
    No status line is marked busy; a busy line is drawn from a class instead.

    A busy subtree inside a live region may be held back by a screen reader
    until it stops being busy, which for "Scanning..." is never, so the
    spinner comes from ``.busy-line`` in app.css.  The Scan button, outside
    the region, keeps its attribute while its job is busy.
    """
    _job_in_state(client, state)
    text = _as_browser(client, _RENDERING_BROWSER if owner else None)
    area = _element(text, "status-area")

    assert "aria-busy" not in area
    assert ('<p class="busy-line">' in area) is (state in BUSY_STATES)
    button = _only_scan_button(text)
    assert (button.attributes.get("aria-busy") == "true") is (state in BUSY_STATES)


@pytest.mark.parametrize("owner", [True, False], ids=["owner", "other-viewer"])
def test_a_flip_acknowledgement_is_a_busy_line(
    client: TestClient, *, owner: bool
) -> None:
    """
    An answered flip reads as work in progress, with no busy attribute.

    The machine is about to scan the backs, so the line has a spinner; the
    unanswered prompt a non-owner sees waits for a person and has none.
    """
    worker = _app(client).state.worker
    job_id = _job_in_state(client, JobState.AWAITING_FLIP)
    unanswered = _element(_as_browser(client, None), "status-area")
    assert "aria-busy" not in unanswered
    assert "busy-line" not in unanswered

    coordinator = WorkerFlipCoordinator(job_id)
    coordinator.arm()
    worker._flip_coordinator = coordinator
    try:
        assert worker.continue_flip(job_id)
        text = _as_browser(client, _RENDERING_BROWSER if owner else None)
    finally:
        worker._flip_coordinator = None

    area = _element(text, "status-area")
    label = escape(flip_answer_label(FlipOutcome.CONTINUED))
    assert f'<p class="busy-line">{label}</p>' in area
    assert "aria-busy" not in area


# The alert region and the disclosure, captured whole. Neither nests a <div> or
# a <details>, so a non-greedy body is exact rather than merely convenient.
_ALERT_DIV = re.compile(r'<div role="alert">(?P<body>.*?)</div>', re.DOTALL)
_TECH_DETAILS = re.compile(
    r'<details class="tech-details"(?P<attrs>[^>]*)>(?P<body>.*?)</details>',
    re.DOTALL,
)

# The ERROR line of a row recorded with no category, byte for byte.
# Substituting UNKNOWN would print "Something went wrong." over a row that
# still holds a truthful specific message.
_LEGACY_ERROR_LINE = (
    '<p role="alert" class="status-error">&#10007; Error: disk on fire</p>'
)


class TestStatusAreaError:
    """
    The ERROR branch renders a sentence, a next step, and a disclosure.

    The three things worth breaking a test over are that the alert covers the
    next step and not just the sentence, that the specific message is still
    rendered, inside the disclosure, and that no host filesystem path ever
    reaches the markup.
    """

    @staticmethod
    def _status(client: TestClient) -> str:
        """
        Render the status area for the current job.

        Returns:
            The status poll's body.

        """
        return client.get("/api/jobs/current/status").text

    @pytest.mark.parametrize(
        "category", [c for c in ErrorCategory if c not in _AMBER_CATEGORIES]
    )
    def test_one_alert_covers_the_sentence_and_the_next_step(
        self, client: TestClient, category: ErrorCategory
    ) -> None:
        """
        The alert announces the message *and* what to do about it.

        The next step is the most actionable content on the page; leaving it
        outside the alert would mean a screen-reader user never hears it.
        Parametrised over every red ``ErrorCategory`` so a new member cannot be
        added without forcing a decision here; the two amber ones, which are
        deliberately not alerts, are pinned in ``TestAmberErrorRendering``.
        """
        _job_in_state(client, JobState.ERROR, error_category=category)
        text = self._status(client)

        assert text.count('role="alert"') == 1
        match = _ALERT_DIV.search(text)
        assert match is not None, "the ERROR branch renders no alert div"
        # Escaped, because ASSEMBLY's next step contains an apostrophe and
        # Jinja renders it as `&#39;`. Comparing against the raw constant would
        # quietly exempt exactly the copy most likely to carry punctuation.
        sentence = escape(error_message(category))
        next_step = escape(error_next_step(category))
        body = match.group("body")
        assert sentence in body
        assert next_step in body
        assert f'<p class="status-error">&#10007; {sentence}</p>' in body

    def test_the_disclosure_is_collapsed_and_sits_outside_the_alert(
        self, client: TestClient
    ) -> None:
        """
        "Technical details" is not announced with the failure, and starts shut.

        No ``open`` attribute, so the detail is one tap away rather than in the
        way; and outside the alert, so a screen reader reads the sentence and
        the next step without the debugging aid.
        """
        _job_in_state(client, JobState.ERROR)
        text = self._status(client)

        details = _TECH_DETAILS.search(text)
        assert details is not None, "the ERROR branch renders no disclosure"
        assert "open" not in details.group("attrs")
        assert "<summary>Technical details</summary>" in details.group("body")

        alert = _ALERT_DIV.search(text)
        assert alert is not None
        assert "<details" not in alert.group("body")

    def test_the_disclosure_holds_the_specific_message_category_and_job_id(
        self, client: TestClient
    ) -> None:
        """
        The disclosure holds the specific message, the category and the job id.

        ``vocabulary.error_message``'s own docstring warns against swapping the
        specific message for a category sentence, so ``job.error`` is rendered
        inside the disclosure, beside the category and the job id and nothing
        else.
        """
        job_id = _job_in_state(client, JobState.ERROR)
        details = _TECH_DETAILS.search(self._status(client))
        assert details is not None
        body = details.group("body")

        assert "disk on fire" in body
        assert f"Category: {_DEFAULT_ERROR_CATEGORY.value}" in body
        assert f"Job: {job_id}" in body

    def test_a_row_without_a_category_renders_the_legacy_line_verbatim(
        self, client: TestClient
    ) -> None:
        """
        A row with no category renders its message alone, with no next step.

        Substituting UNKNOWN would print "Something went wrong." over a row
        that still holds a truthful specific message.
        """
        _job_in_state(client, JobState.ERROR, error_category=None)
        text = self._status(client)

        assert _LEGACY_ERROR_LINE in text
        assert text.count('role="alert"') == 1
        assert "tech-details" not in text
        assert escape(error_next_step(ErrorCategory.UNKNOWN)) not in text

    @pytest.mark.parametrize("category", [_DEFAULT_ERROR_CATEGORY, None])
    def test_no_log_path_reaches_the_rendered_page(
        self, client: TestClient, category: ErrorCategory | None
    ) -> None:
        """
        The configured log file never appears in the markup.

        It is a host filesystem path on a LAN-visible page. Both the
        categorised and the legacy branch are checked, because the disclosure
        is the surface that would be tempted to offer one.
        """
        _job_in_state(client, JobState.ERROR, error_category=category)
        for text in (self._status(client), client.get("/").text):
            assert _LOG_FILE_NAME not in text

    def test_no_template_names_a_log_path(self) -> None:
        """
        No template under ``web/templates/`` references a log path at all.

        The rendered-page assertion above proves this app does not leak one;
        this proves no template *could*.  The templates' expressions and markup
        are read, not their comments.
        """
        log_path = re.compile(r"log_file|log_path|logfile", re.IGNORECASE)
        offenders = sorted(
            (path.name, name)
            for path in _TEMPLATES_DIR.rglob("*.html")
            for name in [
                *_template_names(_template_tree(path)),
                template_markup(path.read_text(encoding="utf-8")),
            ]
            if log_path.search(name)
        )
        assert offenders == []

    def test_the_disclosure_escapes_markup_rather_than_interpolating_it(
        self, client: TestClient
    ) -> None:
        """
        ``job.error`` is exception-derived text and is escaped.

        The disclosure puts an untrusted string in the document; autoescaping is
        a setting, and a setting can be changed, so the property is pinned here
        as it is for ``job.warning``.
        """
        job_store: JobStore = _app(client).state.job_store
        job = job_store.create_job(
            profile="default", title="Render Test", owner_token=_RENDERING_BROWSER
        )
        job_store.update_state(
            job.id,
            JobState.ERROR,
            error="<script>alert(1)</script>",
            error_category=_DEFAULT_ERROR_CATEGORY,
        )
        _adopt_as_current_job(client, job.id)

        text = self._status(client)
        assert "<script>alert(1)</script>" not in text
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text

    def test_the_summary_clears_the_touch_target_floor(self) -> None:
        """
        The summary is a finger-sized row (WCAG 2.5.5).

        Rare control or not, it is still one someone taps standing at the
        scanner.
        """
        summary = _declarations(_css_rule("details.tech-details > summary"))
        assert summary.get("min-height") == "2.75rem"


class TestAmberErrorRendering:
    """
    A failure that may already be in paperless-ngx wears amber, not red.

    Red reads as "do it again", and scanning again is exactly what these two
    categories must not prompt before the reader has checked the document
    list.  The status area, the history cell and its label all follow the
    category, so the three cannot disagree about one row.
    """

    @staticmethod
    def _status(client: TestClient) -> str:
        """
        Render the status area for the current job.

        Returns:
            The status poll's body.

        """
        return client.get("/api/jobs/current/status").text

    @pytest.mark.parametrize("category", sorted(_AMBER_CATEGORIES))
    def test_amber_status_is_a_warning_not_an_alert(
        self, client: TestClient, category: ErrorCategory
    ) -> None:
        """The message and next step render in amber, with no alert role."""
        _job_in_state(client, JobState.ERROR, error_category=category)
        text = self._status(client)

        sentence = escape(error_message(category))
        next_step = escape(error_next_step(category))
        assert f'<p class="status-fallback">&#9888; {sentence}</p>' in text
        assert f"<p>{next_step}</p>" in text
        assert 'role="alert"' not in text
        assert "status-error" not in text
        assert "&#10007;" not in text

    @pytest.mark.parametrize("category", sorted(_AMBER_CATEGORIES))
    def test_amber_status_keeps_the_technical_details(
        self, client: TestClient, category: ErrorCategory
    ) -> None:
        """The disclosure is still there, shut, holding the specific message."""
        job_id = _job_in_state(client, JobState.ERROR, error_category=category)
        details = _TECH_DETAILS.search(self._status(client))
        assert details is not None, "the amber branch renders no disclosure"
        assert "open" not in details.group("attrs")
        body = details.group("body")
        assert "<summary>Technical details</summary>" in body
        assert "disk on fire" in body
        assert f"Category: {category.value}" in body
        assert f"Job: {job_id}" in body

    def test_an_upload_failure_beside_the_amber_ones_stays_a_red_alert(
        self, client: TestClient
    ) -> None:
        """A plain upload failure keeps the red alert markup."""
        _job_in_state(client, JobState.ERROR, error_category=ErrorCategory.UPLOAD)
        text = self._status(client)

        sentence = escape(error_message(ErrorCategory.UPLOAD))
        assert text.count('role="alert"') == 1
        assert f'<p class="status-error">&#10007; {sentence}</p>' in text
        assert "status-fallback" not in text

    @pytest.mark.parametrize(
        ("category", "label"),
        [
            (ErrorCategory.UNCONFIRMED_SEND, UNCONFIRMED_SEND_LABEL),
            (ErrorCategory.UNCONFIRMED_FILING, UNCONFIRMED_FILING_LABEL),
        ],
    )
    def test_amber_history_cell_names_the_category(
        self, client: TestClient, category: ErrorCategory, label: str
    ) -> None:
        """The history row is amber and says what happened, never "Failed"."""
        _job_in_state(client, JobState.ERROR, error_category=category)
        text = client.get("/api/jobs/history").text

        assert f'<td class="status-fallback">\n    {label}\n  </td>' in text
        assert "status-error" not in text
        assert state_label(JobState.ERROR) not in text

    def test_an_upload_failure_history_cell_stays_red_beside_amber(
        self, client: TestClient
    ) -> None:
        """An upload failure's history row keeps the red "Failed" cell."""
        _job_in_state(client, JobState.ERROR, error_category=ErrorCategory.UPLOAD)
        text = client.get("/api/jobs/history").text

        failed = state_label(JobState.ERROR)
        assert f'<td class="status-error">\n    {failed}\n  </td>' in text
        assert "status-fallback" not in text

    def test_no_template_chooses_a_row_colour_from_the_state(self) -> None:
        """
        The history cell's class comes from one vocabulary function.

        An inline chain of state comparisons in the template is how a warned
        upload would reach green and an amber failure red.
        """
        tree = _template_tree(_TEMPLATES_DIR / "partials" / "history.html")
        classes = [
            (_job_attribute(node.node), [_job_attribute(arg) for arg in node.args])
            for node in tree.find_all(nodes.Filter)
            if node.name == "job_status_class" and node.node is not None
        ]
        assert classes == [("state", ["warning", "error_category"])]
        state_comparisons = [
            node
            for node in tree.find_all(nodes.Compare)
            if _job_attribute(node.expr) == "state"
            or any(_job_attribute(operand.expr) == "state" for operand in node.ops)
        ]
        assert state_comparisons == []


@pytest.mark.parametrize("state", list(JobState))
def test_scan_button_disabled_and_busy_split(
    listless_client: TestClient, state: JobState
) -> None:
    """`disabled` follows is_active; `aria-busy` follows the narrower is_busy."""
    # On a page that waits for no list, so the job is the only source.
    client = listless_client
    _job_in_state(client, state)
    match = _scan_button(client.get("/").text)
    assert match is not None, "scan button markup not found"
    attrs = match.attributes

    assert ("disabled" in attrs) is (state in ACTIVE_STATES)
    assert (attrs.get("aria-busy") == "true") is (state in BUSY_STATES)


def _scan_caption(state: JobState) -> str:
    """
    Return the Scan button's exact caption while a job is in ``state``.

    A job waiting for a person says which person-shaped wait it is: the flip
    has its own caption, and every multi-page wait shares one.  A job still
    in the queue is Queued, never Scanning: the button must not say the
    scanner is at work while the status area says the job waits its turn.
    The captions end in the U+2026 character, which Jinja does not escape.
    """
    if state is JobState.PENDING:
        return "Queued…"
    if state is JobState.AWAITING_FLIP:
        return "Waiting for flip…"
    if state in PASS_WAIT_STATES:
        return "Waiting for you…"
    if state in BUSY_STATES:
        return "Scanning…"
    return "Scan"


@pytest.mark.parametrize("state", list(JobState))
def test_scan_button_text(client: TestClient, state: JobState) -> None:
    """The button shows the exact caption for each state."""
    _job_in_state(client, state)
    match = _scan_button(client.get("/").text)
    assert match is not None, "scan button markup not found"
    text = match.text.strip()

    expected = _scan_caption(state)
    assert text == expected


_OOB_ATTR = ' hx-swap-oob="true"'
_OOB_MESSAGE_CLEAR = '<div id="status-message" hx-swap-oob="innerHTML"></div>'


def _only_scan_button(text: str) -> _ScanButton:
    """Return the single scan button in `text`, failing on none or several."""
    ids, buttons = _scan_buttons(text)
    assert ids == 1, "expected exactly one scan button"
    assert len(buttons) == 1, "scan button markup not found"
    return buttons[0]


def test_page_renders_one_inline_scan_button(client: TestClient) -> None:
    """
    The full page carries exactly one Scan button, and it is not OOB.

    The button markup lives in one partial.  Were the OOB include in
    ``partials/status.html``, which the page also includes, the page would
    carry two ``id="scan-btn"`` and this count would catch it.
    """
    match = _only_scan_button(client.get("/").text)
    assert "hx-swap-oob" not in match.attributes


@pytest.mark.parametrize("state", list(JobState))
def test_poll_scan_button_matches_the_page_button(
    listless_client: TestClient, state: JobState
) -> None:
    """
    The poll's OOB button is the page's button plus the OOB flag.

    One template renders both, so for the same job the two copies are
    byte-identical once ``hx-swap-oob`` is removed, and the button's state
    has one source of truth.  The page waits for no list; the next test pins
    what a page with lists adds.
    """
    client = listless_client
    _job_in_state(client, state)
    page = _only_scan_button(client.get("/").text)
    poll = _only_scan_button(client.get("/api/jobs/current/status").text)

    assert poll.attributes.get("hx-swap-oob") == "true"
    assert poll.markup.replace(_OOB_ATTR, "", 1) == page.markup


# What the page's button adds while its lists load: the third disabled source,
# and the hold line it points at.
_HOLD_ATTRS = frozenset({("disabled", None), ("aria-describedby", "scan-hold-reason")})


@pytest.mark.parametrize("state", list(JobState))
def test_page_button_differs_from_the_poll_only_by_the_hold(
    client: TestClient, state: JobState
) -> None:
    """
    With lists on the page, the page's button is the poll's plus the hold.

    The page holds Scan until the lists arrive, which no status response
    knows about, so the two copies may differ by exactly the hold's
    attributes and the poll's OOB flag, and by nothing else: the label and
    ``aria-busy`` still come from the job alone.
    """
    _job_in_state(client, state)
    page = _only_scan_button(client.get("/").text)
    poll = _only_scan_button(client.get("/api/jobs/current/status").text)
    page_attrs = set(page.attributes.items())
    poll_attrs = set(poll.attributes.items())

    assert page_attrs - poll_attrs <= _HOLD_ATTRS
    assert page_attrs >= _HOLD_ATTRS
    assert poll_attrs - page_attrs == {("hx-swap-oob", "true")}
    assert page.text == poll.text


@pytest.mark.parametrize("state", list(JobState))
def test_lazy_load_scan_button_matches_the_poll_button(
    client: TestClient, state: JobState
) -> None:
    """
    The lazy list load releases the hold and nothing else.

    Its out-of-band button is rendered from the same status context the poll
    renders from, so once the lists have landed the button is exactly what a
    poll would show for the same job.
    """
    _job_in_state(client, state)
    lists = load_the_lists(client, client.get("/").text)
    loaded = _only_scan_button(lists)
    poll = _only_scan_button(client.get("/api/jobs/current/status").text)

    assert loaded.markup == poll.markup


@pytest.mark.parametrize("state", list(JobState))
def test_poll_scan_button_follows_the_state_table(
    client: TestClient, state: JobState
) -> None:
    """
    The OOB button on every poll follows the job's state.

    ``aria-busy`` is omitted, never ``"false"``, when the job is not busy.
    """
    _job_in_state(client, state)
    text = client.get("/api/jobs/current/status").text
    match = _only_scan_button(text)
    attrs = match.attributes

    assert ("disabled" in attrs) is (state in ACTIVE_STATES)
    assert (attrs.get("aria-busy") == "true") is (state in BUSY_STATES)
    assert 'aria-busy="false"' not in text

    expected = _scan_caption(state)
    assert match.text.strip() == expected


def test_scan_success_carries_button_status_and_message_clear(
    client: TestClient,
) -> None:
    """
    A successful scan re-renders the button and clears the slot OOB.

    The clear is for this response only: it empties an error left in
    ``#status-message`` by an earlier rejected submit.
    """
    response = client.post(
        "/api/scan", data={"profile": "default", "title": "Button Test"}
    )
    assert response.status_code == 200
    text = response.text

    match = _only_scan_button(text)
    assert match.attributes.get("hx-swap-oob") == "true"
    assert text.count('id="status-area"') == 1
    assert text.count(_OOB_MESSAGE_CLEAR) == 1
    assert text.count("status-message") == 1


def test_poll_never_clears_the_status_message(client: TestClient) -> None:
    """
    A poll re-renders the button OOB but never touches the slot.

    Carrying the clear here would erase a 429 shown mid-scan within a second.
    """
    _job_in_state(client, JobState.SCANNING)
    text = client.get("/api/jobs/current/status").text
    assert _only_scan_button(text).attributes.get("hx-swap-oob") == "true"
    assert "status-message" not in text


@pytest.mark.parametrize("answer", ["continue", "abort"])
def test_flip_responses_never_clear_the_status_message(
    client: TestClient, answer: str
) -> None:
    """Flip Continue and Abort re-render the button OOB, not the slot."""
    _job_in_state(client, JobState.AWAITING_FLIP)
    job_id = _app(client).state.worker._current_job_id
    response = client.post(f"/api/flip/{answer}", data={"job_id": job_id})
    assert response.status_code == 200
    assert _only_scan_button(response.text).attributes.get("hx-swap-oob") == "true"
    assert "status-message" not in response.text


def test_scan_error_response_carries_the_button_only_to_return_focus(
    client: TestClient,
) -> None:
    """
    A rejected scan renders the error and hands focus back to Scan.

    The button rides along once, out-of-band, enabled and with ``autofocus``,
    so the keyboard user whose press was refused is back on it.  It is the only
    thing besides the error: no status area, no second button.
    """
    response = client.post(
        "/api/scan",
        data={"profile": "no-such-profile", "title": "Rejected"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 422
    button = _only_scan_button(response.text).attributes
    assert button.get("hx-swap-oob") == "true"
    assert "disabled" not in button
    assert "autofocus" in button
    assert "status-area" not in response.text


# The rendered scan form's opening tag.  The element carries further
# attributes, so its open tag has whitespace after the URL.
_SCAN_FORM = re.compile(r'<form hx-post="/api/scan"\s+[^>]*>', re.DOTALL)
_TITLE_INPUT = re.compile(r'<input[^>]*id="title-input"[^>]*>')


def test_scan_form_disables_the_button_without_inheritance(
    client: TestClient,
) -> None:
    """
    The form disables the button for its own round-trip only.

    ``hx-disabled-elt`` does it with no script of the page's own.
    ``hx-disinherit`` is mandatory: the selects and refresh buttons inside the
    form would otherwise inherit it and disable the button for the length of
    their own requests.
    """
    match = _SCAN_FORM.search(client.get("/").text)
    assert match is not None, "scan form markup not found"
    form = match.group(0)
    assert 'hx-disabled-elt="#scan-btn"' in form
    assert 'hx-disinherit="hx-disabled-elt"' in form


def test_title_input_is_capped_at_the_server_limit(client: TestClient) -> None:
    """``#title-input`` carries the server's title cap as maxlength."""
    match = _TITLE_INPUT.search(client.get("/").text)
    assert match is not None, "title input markup not found"
    assert f'maxlength="{TITLE_MAX_LENGTH}"' in match.group(0)
    assert 'maxlength="118"' in match.group(0)


def test_title_input_cap_comes_from_the_route_context(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The maxlength number is the route's constant, not a template literal."""
    monkeypatch.setattr("saneless.web.routes.TITLE_MAX_LENGTH", 99)
    match = _TITLE_INPUT.search(client.get("/").text)
    assert match is not None, "title input markup not found"
    assert 'maxlength="99"' in match.group(0)


def test_page_loads_no_app_script_and_no_remote_url(client: TestClient) -> None:
    """The page loads no application JavaScript and no off-box URL."""
    text = client.get("/").text
    assert "app.js" not in text
    assert "http://" not in text
    assert "https://" not in text
    assert "hx-on" not in text


def test_app_script_is_gone(client: TestClient) -> None:
    """``/static/app.js`` does not exist; its 404 uses the one renderer."""
    response = client.get("/static/app.js")
    assert response.status_code == 404
    assert response.json() == {
        "status": "error",
        "detail": rejection_message(RequestRejection.NOT_FOUND),
    }


def test_idle_page_button_and_status(client: TestClient) -> None:
    """
    With no job at all the button reads Scan and the status area is idle.

    The page holds the button until its lists arrive; the lazy list load
    releases it.
    """
    page = client.get("/").text
    held = _only_scan_button(page)
    assert held.text.strip() == "Scan"
    assert "disabled" in held.attributes
    match = _only_scan_button(load_the_lists(client, page))
    assert match.text.strip() == "Scan"
    assert "disabled" not in match.attributes
    assert "aria-busy" not in match.attributes

    status = client.get("/api/jobs/current/status").text
    assert "<p>Ready to scan.</p>" in status
    assert 'hx-trigger="every 1s"' not in status


def test_uploading_renders_its_literal_strings(client: TestClient) -> None:
    """
    UPLOADING renders its exact label and prose, independent of the filters.

    The parametrised tests above build their expectations from `state_label` /
    `progress_label`, so they would pass for any label text. This one hard-codes
    the strings, so a change to either is caught here as well as in
    tests/test_vocabulary.py.
    """
    _job_in_state(client, JobState.UPLOADING)
    assert "Uploading" in client.get("/api/jobs/history").text
    status = client.get("/api/jobs/current/status").text
    assert '<p class="busy-line">Uploading to paperless-ngx...</p>' in status


_HISTORY_RELOAD = (
    '<div hx-get="/api/jobs/history" hx-target="#history-body" '
    'hx-swap="outerHTML" hx-trigger="load" class="htmx-hidden"></div>'
)


def test_fallback_reloads_the_history_table(client: TestClient) -> None:
    """
    FALLBACK carries the hidden history-reload div; a live state does not.

    FALLBACK is terminal, so the history table must refresh the moment the
    status area swaps to it -- exactly as it does for DONE and ERROR. Without
    the div the table keeps showing the job as Uploading until the user
    reloads the page.
    """
    _job_in_state(client, JobState.FALLBACK)
    assert _HISTORY_RELOAD in client.get("/api/jobs/current/status").text

    _job_in_state(client, JobState.SCANNING)
    assert _HISTORY_RELOAD not in client.get("/api/jobs/current/status").text


def test_fallback_warning_renders_inline(client: TestClient) -> None:
    """
    The warning is a second visible paragraph, not a tooltip or a hidden detail.

    A user whose title, tags and correspondent were dropped has to be told so
    without hovering anything.
    """
    _job_in_state(client, JobState.FALLBACK, warning="Metadata was not applied.")
    text = client.get("/api/jobs/current/status").text
    assert '<p class="status-fallback">Metadata was not applied.</p>' in text


def test_fallback_without_a_warning_renders_no_empty_paragraph(
    client: TestClient,
) -> None:
    """With no warning recorded, the second paragraph is omitted entirely."""
    _job_in_state(client, JobState.FALLBACK)
    text = client.get("/api/jobs/current/status").text
    assert '<p class="status-fallback">None</p>' not in text
    assert '<p class="status-fallback"></p>' not in text
    assert text.count('<p class="status-fallback">') == 1


def test_fallback_warning_is_escaped_not_injected(client: TestClient) -> None:
    """
    A warning carrying markup is escaped, never interpolated as HTML.

    `job.warning` can carry text from upstream of saneless, such as a
    paperless-ngx response body.  Jinja2 autoescaping is on by default under
    `Jinja2Templates`, but "by default" is a setting, and a setting can be
    changed; this pins the property itself.  If this test fails the fix is to
    re-enable autoescaping, never to sanitise at the call site.
    """
    _job_in_state(client, JobState.FALLBACK, warning="<script>alert(1)</script>")
    text = client.get("/api/jobs/current/status").text
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text


# The sentence the pipeline records when the scanner could not read a sheet: the
# commonest way an upload succeeds while a page is lost.
_SKIPPED_SHEET_WARNING = (
    "1 page(s) could not be read by the scanner and were skipped. "
    "They were not removed for being blank; rescan those sheets."
)

# The headline a warned upload wears, amber like a fallback and never "Done".
_WARNED_HEADLINE = (
    '<p class="status-fallback">&#9888; Uploaded with a warning: Render Test</p>'
)


def _assert_warned_done(text: str) -> None:
    """
    Assert that a page shows a warned upload as one, and never as a clean one.

    Args:
        text: The rendered status area or index page.

    """
    assert _WARNED_HEADLINE in text
    assert f'<p class="status-fallback">{_SKIPPED_SHEET_WARNING}</p>' in text
    assert "Done: Render Test" not in text
    assert '<p class="status-done">' not in text
    # A lost sheet is a degraded success, not a failure, as with a fallback.
    # Only the status area and what follows it are searched: the index page
    # carries an empty `#status-message` alert region above it for form errors.
    status_area = text[text.index('<div id="status-area"') :]
    assert 'role="alert"' not in status_area


def test_warned_done_renders_the_warning_on_the_status_poll(
    client: TestClient,
) -> None:
    """
    An upload that lost a sheet is headed in amber with the reason beneath.

    A green tick over a document missing a page tells the operator the stack
    is safe to throw away, which is the one thing it is not.
    """
    _job_in_state(client, JobState.DONE, warning=_SKIPPED_SHEET_WARNING)
    _assert_warned_done(client.get("/api/jobs/current/status").text)


def test_warned_done_renders_the_warning_on_the_index_page(
    client: TestClient,
) -> None:
    """
    A reload names the warned upload as the last scan, never a green tick.

    The page reports a finished job as the past, so the amber headline is the
    poll's; what the page must still never do is call a warned upload Done,
    and it keeps the warning beneath, muted like the line above it.
    """
    _job_in_state(client, JobState.DONE, warning=_SKIPPED_SHEET_WARNING)
    area = _element(client.get("/").text, "status-area")

    assert re.search(
        r'<p class="last-scan">Last scan: \u26a0 Uploaded with a warning: '
        r"Render Test \u2014 started [^<]+</p>",
        area,
    )
    assert f'<p class="last-scan">{_SKIPPED_SHEET_WARNING}</p>' in area
    assert "Done: Render Test" not in area
    assert "status-done" not in area
    assert 'role="alert"' not in area


def test_warned_done_warning_is_escaped_not_injected(client: TestClient) -> None:
    """
    The upload branch escapes its warning exactly as the fallback branch does.

    The warning column is written by more than one path, so the second place
    that renders it needs the same pin as the first.
    """
    _job_in_state(client, JobState.DONE, warning="<script>alert(1)</script>")
    text = client.get("/api/jobs/current/status").text
    assert "<script>alert(1)</script>" not in text
    assert (
        '<p class="status-fallback">&lt;script&gt;alert(1)&lt;/script&gt;</p>' in text
    )


def test_done_without_a_warning_is_unchanged_warned_guard(
    client: TestClient,
) -> None:
    """
    A clean upload keeps its green tick, byte for byte, and gains no amber line.

    Named with the warned tests so the guard runs whenever they do: splitting
    the branch must not change what a clean upload looks like.
    """
    _job_in_state(client, JobState.DONE)
    text = client.get("/api/jobs/current/status").text
    assert '<p class="status-done">&#10003; Done: Render Test</p>' in text
    assert "status-fallback" not in text


def test_warned_done_history_cell_label_and_class(client: TestClient) -> None:
    """The history row names a warned upload and colours it amber, not green."""
    _job_in_state(client, JobState.DONE, warning=_SKIPPED_SHEET_WARNING)
    text = client.get("/api/jobs/history").text
    assert '<td class="status-fallback">\n    Uploaded with a warning\n  </td>' in text
    assert '<td class="status-done">' not in text
    assert state_label(JobState.DONE) not in text


# A scan that produced twelve pages, discarded two as blank and sent ten on.
_COUNTS = JobResult(
    outcome=None,
    warning=None,
    pages_scanned=12,
    pages_removed=2,
    pages_uploaded=10,
)

# The one-page scan that measured no blanks. Its zero is a true measurement and
# must render as 0; only a NULL suppresses the sentence.
_ONE_PAGE = JobResult(
    outcome=None,
    warning=None,
    pages_scanned=1,
    pages_removed=0,
    pages_uploaded=1,
)

# The six terminal cases, and what each records. Only the first two record
# counts: `worker.py` leaves them NULL on error and on cancel, `job.py` leaves
# them NULL on a refused submit, and an older row may carry none at all. Four
# of six is the common path, not an edge.
_TERMINAL_CASES = [
    pytest.param(JobState.DONE, None, _COUNTS, id="done"),
    pytest.param(JobState.FALLBACK, None, _COUNTS, id="fallback"),
    pytest.param(JobState.ERROR, _DEFAULT_ERROR_CATEGORY, None, id="error"),
    pytest.param(JobState.CANCELLED, None, None, id="cancelled"),
    pytest.param(JobState.ERROR, ErrorCategory.REJECTED, None, id="rejected"),
    pytest.param(JobState.DONE, None, None, id="pre-phase-23"),
]

# The two branches that record counts, so the parametrised expectation is one
# membership test rather than a second copy of the table above.
_CASES_WITH_COUNTS = {"done", "fallback"}


def _finished_job(
    client: TestClient,
    state: JobState,
    result: JobResult | None,
    error_category: ErrorCategory | None = None,
) -> str:
    """
    Finish a job in `state`, recording `result`'s counts or leaving them NULL.

    ``finish_job`` is the only writer of the count columns, and omitting the
    result leaves all three NULL rather than zero -- which is exactly how the
    four countless terminal cases arise in production.

    Returns:
        The job's id.

    """
    job_store: JobStore = _app(client).state.job_store
    job = job_store.create_job(
        profile="default", title="Render Test", owner_token=_RENDERING_BROWSER
    )
    job_store.finish_job(
        job.id,
        state,
        result=result,
        error="disk on fire",
        error_category=error_category,
    )
    _adopt_as_current_job(client, job.id)
    return job.id


class TestPageCounts:
    """
    Where the page counts render, and where they deliberately do not.

    Counts render for every terminal job that recorded them.  ERROR and
    CANCELLED rows have none by construction, so their absence there is
    expected rather than a gap.
    """

    @pytest.mark.parametrize(("state", "category", "result"), _TERMINAL_CASES)
    def test_pages_render_for_exactly_the_cases_that_recorded_them(
        self,
        client: TestClient,
        request: pytest.FixtureRequest,
        state: JobState,
        category: ErrorCategory | None,
        result: JobResult | None,
    ) -> None:
        """
        Two of the six terminal cases show counts; four show no element at all.

        Not an empty paragraph and not a blank line -- nothing. A sentence
        naming two of three counts would invite the reader to wonder about the
        third, so one NULL suppresses the whole line.
        """
        case = request.node.callspec.id
        _finished_job(client, state, result, category)
        text = client.get("/api/jobs/current/status").text

        expected = case in _CASES_WITH_COUNTS
        assert ('<p class="page-counts">' in text) is expected
        assert ("page-counts" in text) is expected

    def test_pages_sentence_is_the_shared_filter_verbatim(
        self, client: TestClient
    ) -> None:
        """The status area shows what `page_counts` built, not its own wording."""
        _finished_job(client, JobState.DONE, _COUNTS)
        sentence = page_counts(_COUNTS)
        assert sentence is not None
        text = client.get("/api/jobs/current/status").text
        assert f'<p class="page-counts">{sentence}</p>' in text
        assert "12 pages scanned, 2 blank removed, 10 uploaded" in text

    def test_a_measured_zero_pages_renders_as_zero(self, client: TestClient) -> None:
        """
        A scan where nothing was blank really did remove 0 pages.

        The guard is the filter returning None, never truthiness; a truthiness
        guard anywhere on this path would delete this line.
        """
        _finished_job(client, JobState.DONE, _ONE_PAGE)
        text = client.get("/api/jobs/current/status").text
        assert (
            '<p class="page-counts">1 page scanned, 0 blank removed, 1 uploaded</p>'
            in text
        )

    def test_the_pages_line_follows_the_outcome_and_precedes_the_thumbnail(
        self, client: TestClient
    ) -> None:
        """
        The counts are the last paragraph of the terminal branch.

        They are a footnote to the outcome, so they read after it -- and before
        the preview, which is the end of the status area.
        """
        job_id = _finished_job(client, JobState.DONE, _COUNTS)
        _app(client).state.job_store.update_thumbnail(job_id, "dGVzdA==")
        text = client.get("/api/jobs/current/status").text

        assert text.index('class="status-done"') < text.index('class="page-counts"')
        assert text.index('class="page-counts"') < text.index('class="thumbnail"')

    def test_the_fallback_pages_line_follows_the_warning(
        self, client: TestClient
    ) -> None:
        """Under FALLBACK the counts read after the warning, not between it and the outcome."""
        job_store: JobStore = _app(client).state.job_store
        job = job_store.create_job(
            profile="default", title="Render Test", owner_token=_RENDERING_BROWSER
        )
        job_store.finish_job(
            job.id,
            JobState.FALLBACK,
            result=JobResult(
                outcome=None,
                warning="Metadata was not applied.",
                pages_scanned=12,
                pages_removed=2,
                pages_uploaded=10,
            ),
        )
        _adopt_as_current_job(client, job.id)
        text = client.get("/api/jobs/current/status").text

        assert text.index("Metadata was not applied.") < text.index(
            'class="page-counts"'
        )

    def test_the_history_title_cell_carries_the_pages_as_a_second_line(
        self, client: TestClient
    ) -> None:
        """
        The counts are a second line in the Title cell, not a fifth column.

        A span with `display: block`, so it forms its own line inside the cell
        the mobile rule already wraps.
        """
        _finished_job(client, JobState.DONE, _COUNTS)
        sentence = page_counts(_COUNTS)
        assert sentence is not None
        text = client.get("/api/jobs/history").text
        assert f'<span class="page-counts">{sentence}</span>' in text

    def test_the_history_cell_omits_the_pages_element_entirely(
        self, client: TestClient
    ) -> None:
        """A row with no counts renders no span, not an empty one."""
        _finished_job(client, JobState.CANCELLED, None)
        text = client.get("/api/jobs/history").text
        assert "page-counts" not in text

    def test_the_history_table_still_has_four_columns(self) -> None:
        """
        The history table has four headers and an empty row spanning four.

        The page counts are a second line in the Title cell, so no fifth column
        drifts against `colspan` or competes with the Time column, which the
        zone name already widens.
        """
        headers = [tag for tag, _ in _template_tags("index.html") if tag == "th"]
        spans = [
            attributes["colspan"]
            for _, attributes in _template_tags("partials", "history.html")
            if "colspan" in attributes
        ]
        assert len(headers) == 4
        assert spans == ["4"]

    def test_no_template_guards_a_page_count_with_truthiness(self) -> None:
        """
        The guard is the filter returning None, never a count's truthiness.

        `{% if job.pages_removed %}` would silently delete a true zero, so no
        template is allowed to write one.
        """
        offenders = sorted(
            (path.name, attribute)
            for path in _TEMPLATES_DIR.rglob("*.html")
            for test in _conditions(_template_tree(path))
            for attribute in _truth_tested_job_attributes(test)
            if attribute.startswith("pages_")
        )
        assert offenders == []

    def test_the_stylesheet_gains_one_muted_pages_rule(self) -> None:
        """
        The one ``.page-counts`` rule is a muted block, with no colour literal.

        Muted rather than a status colour, because a count is a measurement and
        not an outcome.
        """
        selectors = [
            selector
            for selector, _ in _css_rules(_APP_CSS.read_text(encoding="utf-8"))
            if ".page-counts" in selector
        ]
        assert selectors == [".page-counts"]
        assert _declarations(_css_rule(".page-counts")) == {
            "display": "block",
            "color": "var(--pico-muted-color)",
        }


# Four pages scanned, the backs of two sheets removed as blank, two uploaded.
_BLANK_BACKS = JobResult(
    outcome=None,
    warning=None,
    pages_scanned=4,
    pages_removed=2,
    pages_uploaded=2,
    removed_positions=(2, 4),
)

_BLANK_BACKS_NOTE = "Removed as blank: pages 2, 4 of 4 scanned."


class TestRemovedPagesNote:
    """
    The pages removed as blank are named beside the counts, as information.

    The note is not a warning, so a DONE that removed blank backs keeps its
    green tick, and pages are named by scanned position.
    """

    def test_the_status_poll_names_the_removed_pages(self, client: TestClient) -> None:
        """The note is a muted `page-counts` line and the DONE headline stays green."""
        _finished_job(client, JobState.DONE, _BLANK_BACKS)
        text = client.get("/api/jobs/current/status").text
        assert f'<p class="page-counts">{_BLANK_BACKS_NOTE}</p>' in text
        assert '<p class="status-done">&#10003; Done: Render Test</p>' in text
        assert "status-fallback" not in text

    def test_the_index_page_leaves_the_note_to_history(
        self, client: TestClient
    ) -> None:
        """
        A reload reports the scan as past, and its history row keeps the note.

        The status area's last-scan rendering carries no counts, so the note
        the operator needs to rescan by is the history row's, on the same page.
        """
        _finished_job(client, JobState.DONE, _BLANK_BACKS)
        text = client.get("/").text
        area = _element(text, "status-area")
        assert "page-counts" not in area
        assert '<p class="last-scan">Last scan: \u2713 Done: Render Test' in area
        assert f'<span class="page-counts">{_BLANK_BACKS_NOTE}</span>' in text

    def test_the_note_follows_the_counts(self, client: TestClient) -> None:
        """The note reads after the counts sentence it explains."""
        _finished_job(client, JobState.DONE, _BLANK_BACKS)
        text = client.get("/api/jobs/current/status").text
        assert text.index("4 pages scanned, 2 blank removed, 2 uploaded") < (
            text.index(_BLANK_BACKS_NOTE)
        )

    def test_the_history_row_names_the_removed_pages(self, client: TestClient) -> None:
        """History carries the note as another `page-counts` line in the Title cell."""
        _finished_job(client, JobState.DONE, _BLANK_BACKS)
        text = client.get("/api/jobs/history").text
        assert f'<span class="page-counts">{_BLANK_BACKS_NOTE}</span>' in text
        assert '<td class="status-done">' in text
        assert "status-fallback" not in text

    def test_a_fallback_names_the_removed_pages_too(self, client: TestClient) -> None:
        """FALLBACK shows the note beside its counts, as DONE does."""
        _finished_job(client, JobState.FALLBACK, _BLANK_BACKS)
        text = client.get("/api/jobs/current/status").text
        assert f'<p class="page-counts">{_BLANK_BACKS_NOTE}</p>' in text

    @pytest.mark.parametrize(
        "path", ["/api/jobs/current/status", "/api/jobs/history", "/"]
    )
    def test_no_positions_render_no_note(self, client: TestClient, path: str) -> None:
        """A job that recorded no positions renders no note element at all."""
        _finished_job(client, JobState.DONE, _COUNTS)
        text = client.get(path).text
        assert "Removed as blank" not in text


class TestHistoryTimeCell:
    """The history Time cell names its zone."""

    def test_the_time_cell_names_the_zone(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A copy-pasted line is self-describing, so the zone is on every row.

        `local_time` renders whatever zone the C library reports, so pinning
        `TZ` and calling `time.tzset()` is the only way to assert a shape on a
        host in an unknown zone; the trailing `tzset` makes the library notice
        monkeypatch's teardown.
        """
        monkeypatch.setenv("TZ", "America/Chicago")
        time.tzset()
        try:
            _finished_job(client, JobState.DONE, _COUNTS)
            text = client.get("/api/jobs/history").text
            cell = re.search(r"<tr>\s*<td>([^<]*)</td>", text)
            assert cell is not None, "no history row rendered"
            assert re.fullmatch(
                r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} \S+", cell.group(1).strip()
            )
        finally:
            monkeypatch.undo()
            time.tzset()

    def test_the_time_cell_renders_through_the_shared_filter(self) -> None:
        """
        The template formats the time with `local_time` alone, never `strftime`.

        `cli.py` imports the same object, so the web table and the CLI table
        cannot disagree about the zone or the format without editing the one
        implementation.
        """
        names = _template_names(
            _template_tree(_TEMPLATES_DIR / "partials" / "history.html")
        )
        assert "strftime" not in names
        assert names.count("local_time") == 1


# How long the owed-write test waits for the worker to act.  Idle ticks run at
# 20 ms there, so this is many ticks' worth of slack.
_OWED_WRITE_BUDGET = 2.0

# The error every simulated job store failure in the owed-write test raises.
_DISK_ERROR = "disk I/O error"


class _BreakableWrite:
    """
    A job store write that raises ``sqlite3.OperationalError`` while broken.

    Every call is counted; while ``broken`` is set the call raises, otherwise
    it delegates to the real method, so the row the worker writes is the row
    the status poll reads back.

    It also stands in for the worker's handling of a job.  SCANNING is a
    progress write recorded under the scanner gate, and its failure is only
    logged, so no store write is left ahead of the pipeline whose failure is
    the loop's own: a store error escaping the job's handling is staged by
    breaking that handling instead.

    Args:
        original: The bound store method, or job handling, being replaced.
        broken: Set while the store should refuse writes.

    """

    def __init__(
        self, original: Callable[..., object], broken: threading.Event
    ) -> None:
        """Wrap ``original``, failing while ``broken`` is set."""
        self._original = original
        self._broken = broken
        self._lock = threading.Lock()
        self._calls = 0

    @property
    def calls(self) -> int:
        """How many times the write has been called so far."""
        with self._lock:
            return self._calls

    def __call__(self, *args: object, **kwargs: object) -> object:
        """Count the call, then raise or delegate."""
        with self._lock:
            self._calls += 1
        if self._broken.is_set():
            raise sqlite3.OperationalError(_DISK_ERROR)
        return self._original(*args, **kwargs)


def test_status_poll_reenables_the_scan_button_once_an_owed_failure_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A job the guard could not end stops disabling Scan once the store heals.

    One loop-level failure whose best-effort ERROR write also failed leaves
    the row active and the button
    disabled while ``/health`` stays 200.  The streak limit is raised here so
    the worker never degrades and no probe runs; the owed write must still
    land on an idle tick, and the next status poll must render ``#scan-btn``
    enabled with no restart and no scan.
    """
    # Before the lifespan starts the worker: patched later, it would sit in a
    # five-second queue wait before the first fast tick.
    monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", 0.02)
    # This test watches /health stay 200 through a failing window; the streak
    # degrade is proven by
    # test_health_reports_the_job_store_failing_while_an_owed_failure_cannot_be_written.
    monkeypatch.setattr("saneless.worker._OWED_RETRY_DEGRADED_AFTER", 1_000_000)
    app = _make_app(tmp_path)
    broken = threading.Event()
    broken.set()

    with TestClient(app) as tc:
        job_store: JobStore = app.state.job_store
        handling = _BreakableWrite(app.state.worker._scan_job, broken)
        updates = _BreakableWrite(job_store.update_state, broken)
        finishes = _BreakableWrite(job_store.finish_job, broken)
        monkeypatch.setattr(app.state.worker, "_scan_job", handling)
        monkeypatch.setattr(job_store, "update_state", updates)
        monkeypatch.setattr(job_store, "finish_job", finishes)

        response = tc.post("/api/scan", data={"profile": "default"})
        assert response.status_code == 200
        job_id = job_store.list_recent(limit=1)[0].id

        # The guard's write, then at least one idle-tick retry.
        assert poll_until(lambda: finishes.calls >= 2, _OWED_WRITE_BUDGET)
        stuck = _only_scan_button(tc.get("/api/jobs/current/status").text)
        assert "disabled" in stuck.attributes
        assert tc.get("/health").status_code == 200

        broken.clear()

        def button_enabled() -> bool:
            text = tc.get("/api/jobs/current/status").text
            return "disabled" not in _only_scan_button(text).attributes

        assert poll_until(button_enabled, _OWED_WRITE_BUDGET)
        finished = job_store.get_job(job_id)
        assert finished is not None
        assert finished.state is JobState.ERROR
        assert tc.get("/health").status_code == 200


def test_health_reports_the_job_store_failing_while_an_owed_failure_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A job store that keeps refusing an owed write shows on ``/health``.

    A store error escapes the job's handling and the guard's ERROR write
    fails, and every idle retry of the owed write fails too.  After a short
    streak of failed retries the worker degrades, so ``/health`` answers 503
    "job store failing" and a new scan is refused with 503.  Once the store heals, the recovery probe and the owed write land:
    ``/health`` is 200 again and the status poll renders ``#scan-btn`` enabled.
    """
    # Before the lifespan starts the worker: patched later, it would sit in a
    # five-second queue wait before the first fast tick.
    monkeypatch.setattr("saneless.worker._IDLE_TICK_SECONDS", 0.02)
    app = _make_app(tmp_path)
    broken = threading.Event()
    broken.set()

    with TestClient(app) as tc:
        job_store: JobStore = app.state.job_store
        handling = _BreakableWrite(app.state.worker._scan_job, broken)
        updates = _BreakableWrite(job_store.update_state, broken)
        finishes = _BreakableWrite(job_store.finish_job, broken)
        monkeypatch.setattr(app.state.worker, "_scan_job", handling)
        monkeypatch.setattr(job_store, "update_state", updates)
        monkeypatch.setattr(job_store, "finish_job", finishes)

        response = tc.post("/api/scan", data={"profile": "default"})
        assert response.status_code == 200
        job_id = job_store.list_recent(limit=1)[0].id

        assert poll_until(
            lambda: tc.get("/health").status_code == 503, _OWED_WRITE_BUDGET
        )
        assert tc.get("/health").json() == {
            "status": "error",
            "detail": "job store failing",
        }
        refused = tc.post("/api/scan", data={"profile": "default"})
        assert refused.status_code == 503

        broken.clear()

        assert poll_until(
            lambda: tc.get("/health").status_code == 200, _OWED_WRITE_BUDGET
        )

        def button_enabled() -> bool:
            text = tc.get("/api/jobs/current/status").text
            return "disabled" not in _only_scan_button(text).attributes

        assert poll_until(button_enabled, _OWED_WRITE_BUDGET)
        finished = job_store.get_job(job_id)
        assert finished is not None
        assert finished.state is JobState.ERROR


# How long the worker gets to reach the held gate, and then to finish the job
# once the gate is let go: a stub scan and an offline upload.
_GATE_WAIT_BUDGET = 5.0

# How long the job is left waiting on the held gate before the status area is
# read.  A SCANNING written ahead of the gate lands within microseconds of the
# worker taking the job.
_GATE_WAIT_WINDOW = 0.3


def test_a_job_waiting_for_the_gate_reads_starting_scan(client: TestClient) -> None:
    """
    A scan queued behind a running check still says "Starting scan...".

    A health check may hold the scanner gate for as long as its listing
    deadline.  The job has not begun to scan until the worker holds the gate,
    so the status area keeps its first line and claims no scan in progress.
    """
    app = _app(client)
    worker = app.state.worker
    job_store: JobStore = app.state.job_store
    gate = worker.scanner_gate
    assert gate.acquire(timeout=_GATE_WAIT_BUDGET)
    try:
        response = client.post("/api/scan", data={"profile": "default"})
        assert response.status_code == 200
        job_id = job_store.list_recent(limit=1)[0].id
        assert poll_until(lambda: worker.current_job_id == job_id, _GATE_WAIT_BUDGET)
        quiet_window(_GATE_WAIT_WINDOW)
        area = _element(client.get("/api/jobs/current/status").text, "status-area")
    finally:
        gate.release()

    def finished() -> bool:
        job = job_store.get_job(job_id)
        return job is not None and job.state in TERMINAL_STATES

    assert poll_until(finished, _GATE_WAIT_BUDGET)
    starting = progress_label(JobState.PENDING)
    assert f'<p class="busy-line">{escape(starting)}</p>' in area
    assert progress_label(JobState.SCANNING) not in area


# --- The owner-gated flip prompt -------------------------------------------

# The cookie's wire name, spelled out rather than imported: a test that imported
# the constant would still pass if the name changed under every browser that
# already holds one.
_OWNER_COOKIE = "saneless_owner"

# The token these tests write straight onto the row.  The mint itself is pinned
# in tests/test_web.py; what is under test here is the rendering, so the row is
# staged directly and the value only has to be distinctive.
_OWNING_BROWSER = "the-browser-that-submitted-this-stack"

# A base64 payload that is not a real JPEG.  Nothing decodes it: it exists so
# the owner's rendering carries an `<img>` that every other browser's lacks.
_THUMBNAIL = "c3RhbmQtaW4="

# The exact confirmation: one question, one consequence, and no
# claim about the pages already scanned, which is a promise this contract
# cannot verify.
_ABORT_CONFIRMATION = "Abort this scan? It will stop and cannot be resumed."

# The copy a viewer who did not submit the job sees in place of the buttons,
# before the worker has recorded when the flip wait began: none of these tests
# arms a flip coordinator, so none has a deadline to name.
_NON_OWNER_LINE = non_owner_wait_line(JobState.AWAITING_FLIP, deadline=None)

_STATUS_OPEN = re.compile(r'<div id="status-area"[^>]*>', re.DOTALL)
_SEEN_TOKEN = re.compile(r"seen=[0-9a-f]+")
_THUMBNAIL_START = '<img src="data:image/jpeg;base64,'

# Any control that would let a second browser seize an answered-for job.  The
# absence of one IS the rendering, so it is asserted like any other contract
# rather than left to memory.
_SEIZE_CONTROL = re.compile(r"override|take[ -]over|force", re.IGNORECASE)


def _flip_job(client: TestClient, owner: str | None) -> str:
    """
    Stage an AWAITING_FLIP job with a thumbnail and the given owner token.

    Args:
        client: The client whose app owns the store and worker.
        owner: The token to record, or None for a pre-upgrade unowned row.

    Returns:
        The job's id.

    """
    job_store: JobStore = _app(client).state.job_store
    job = job_store.create_job(
        profile="default", title="Flip Render", owner_token=owner
    )
    job_store.update_state(job.id, JobState.AWAITING_FLIP)
    job_store.update_thumbnail(job.id, _THUMBNAIL)
    _adopt_as_current_job(client, job.id)
    return job.id


def _as_browser(client: TestClient, token: str | None) -> str:
    """
    Fetch the status area as a browser carrying `token`, and return the markup.

    The cookie is set as a header rather than through the client's jar so one
    client can stand in for two browsers without the jar carrying state from
    one request into the next.  The jar is emptied first, so the module's
    rendering token is not sent alongside and "no token" means none.

    Args:
        client: The client to request through.
        token: The owner token to present, or None to present none.

    Returns:
        The rendered response body.

    """
    client.cookies.clear()
    headers = {} if token is None else {"Cookie": f"{_OWNER_COOKIE}={token}"}
    response = client.get("/api/jobs/current/status", headers=headers)
    assert response.status_code == 200
    return response.text


def _around_the_flip_block(markup: str) -> tuple[str, str]:
    """
    Return the parts of the markup the owner gate must not change.

    The token gates the flip branch's controls, and through the job view the
    title, preview and detail text.  What is left -- the status area's opening
    tag with its poll attributes, and the out-of-band Scan button -- is the
    same for every viewer, so the appliance reads as equally busy to all.

    The poll's ``seen`` token is masked: it hashes what this viewer is shown,
    so it differs exactly where the gate says the rendering does, and the
    path and interval around it are what must match.

    Args:
        markup: The rendered response body.

    Returns:
        The status area's opening tag with its token masked, and the Scan
        button.

    """
    opening = _STATUS_OPEN.search(markup)
    assert opening is not None
    button = _scan_button(markup)
    assert button is not None
    masked = _SEEN_TOKEN.sub("seen=<token>", opening.group(0))
    assert masked != opening.group(0)
    return masked, button.markup


class TestOwnerGatedFlipPrompt:
    """
    Who sees the Continue and Abort buttons, and what everyone else sees.

    The token gates those two buttons.  The gate is
    server-side -- the buttons are not rendered for a non-owner, never hidden
    with CSS, which would be an ASVS V4 failure.  The same token also gates the
    job's preview, through the job view, while the poll and the Scan button
    stay the same for everyone.
    """

    def test_owner_sees_the_flip_prompt_with_both_buttons(
        self, client: TestClient
    ) -> None:
        """The browser that submitted the stack gets the prompt."""
        _flip_job(client, _OWNING_BROWSER)

        markup = _as_browser(client, _OWNING_BROWSER)

        assert "flip the stack over the long edge" in markup.lower()
        assert markup.count('hx-post="/api/flip/') == 2
        assert ">Continue<" in markup
        assert ">Abort scan<" in markup
        assert _NON_OWNER_LINE not in markup

    def test_non_owner_sees_the_waiting_line_and_no_flip_controls(
        self, client: TestClient
    ) -> None:
        """
        A second viewer gets the waiting copy and zero controls.

        Zero controls, not hidden ones: the gate is decided on the server and
        the markup never carries a button the viewer is not allowed to press.
        """
        _flip_job(client, _OWNING_BROWSER)

        markup = _as_browser(client, None)

        assert f"<p>{_NON_OWNER_LINE}</p>" in markup
        assert markup.count('hx-post="/api/flip/') == 0
        assert "Continue" not in markup
        assert "Abort scan" not in markup

    def test_a_different_owner_token_is_also_a_non_owner(
        self, client: TestClient
    ) -> None:
        """A wrong token is no better than no token at all."""
        _flip_job(client, _OWNING_BROWSER)

        markup = _as_browser(client, "some-other-browsers-token")

        assert f"<p>{_NON_OWNER_LINE}</p>" in markup
        assert markup.count('hx-post="/api/flip/') == 0

    def test_owner_and_non_owner_share_the_poll_and_button_but_not_the_preview(
        self, client: TestClient
    ) -> None:
        """The poll and the Scan button match; the controls and preview are the owner's."""
        _flip_job(client, _OWNING_BROWSER)

        owner = _as_browser(client, _OWNING_BROWSER)
        other = _as_browser(client, None)

        assert _around_the_flip_block(owner) == _around_the_flip_block(other)
        assert f"{_THUMBNAIL_START}{_THUMBNAIL}" in owner
        assert _THUMBNAIL_START not in other

    def test_unowned_flip_job_renders_the_prompt_for_everyone(
        self, client: TestClient
    ) -> None:
        """
        A NULL owner token means unowned, so the prompt renders for all.

        This is the manual-duplex job that was already in flight when the
        appliance was upgraded; a strict rule would leave it un-continuable.
        """
        _flip_job(client, None)

        markup = _as_browser(client, None)

        assert markup.count('hx-post="/api/flip/') == 2
        assert _NON_OWNER_LINE not in markup

    def test_owner_flip_prompt_offers_no_way_to_seize_another_job(
        self, client: TestClient
    ) -> None:
        """
        There are exactly two controls and no third one.

        A human who closed the tab is the same case as a human who walked
        away, and the bounded flip timeout resolves both.  The absence of a
        third control is the design, so it is asserted.
        """
        _flip_job(client, _OWNING_BROWSER)

        markup = _as_browser(client, _OWNING_BROWSER)

        assert markup.count("<button") == markup.count('hx-post="/api/flip/') + 1
        assert _SEIZE_CONTROL.search(markup) is None

    def test_non_owner_flip_rendering_offers_nothing_to_seize_with_either(
        self, client: TestClient
    ) -> None:
        """The waiting viewer is offered no control of any kind."""
        _flip_job(client, _OWNING_BROWSER)

        markup = _as_browser(client, None)

        assert _SEIZE_CONTROL.search(markup) is None

    def test_non_owner_flip_line_carries_no_trailing_ellipsis(
        self, client: TestClient
    ) -> None:
        """
        The locked copy ends without one, against the usual in-progress style.

        The string is locked copy, so this test records the style exception
        rather than letting a tidy-up "fix" it.
        """
        _flip_job(client, _OWNING_BROWSER)

        markup = _as_browser(client, None)

        assert f"{_NON_OWNER_LINE}</p>" in markup
        assert f"{_NON_OWNER_LINE}..." not in markup
        assert f"{_NON_OWNER_LINE}&#8230;" not in markup

    def test_flip_abort_button_carries_the_exact_confirmation(
        self, client: TestClient
    ) -> None:
        """Abort asks first, in its exact words, and nothing else asks."""
        _flip_job(client, _OWNING_BROWSER)

        markup = _as_browser(client, _OWNING_BROWSER)

        confirming = [
            (tag, attributes["hx-confirm"])
            for tag, attributes in _rendered_start_tags(markup)
            if "hx-confirm" in attributes
        ]
        assert confirming == [("button", _ABORT_CONFIRMATION)]

    def test_flip_confirmation_is_on_the_button_not_the_scan_form(self) -> None:
        """
        The confirmation is on the flip prompt's Abort button, never the scan form.

        On the form it would make every child request confirm, and it would
        need the form's ``hx-disinherit`` list extended, which would let the
        selects and refresh buttons disable Scan again.
        """
        index = _template_tags("index.html")
        flip = _template_tags("partials", "flip.html")

        assert [
            attributes for _, attributes in index if "hx-confirm" in attributes
        ] == []
        assert _scan_form_attributes().get("hx-disinherit") == "hx-disabled-elt"
        assert [tag for tag, attributes in flip if "hx-confirm" in attributes] == [
            "button"
        ]


# A deadline a stubbed worker reports.  Aware, as the worker's always is, and
# rendered through `local_time`, so the expected line is built the same way.
_DEADLINE = datetime(2026, 9, 30, 19, 23, tzinfo=UTC)

# The flip prompt's fixed sentences, byte for byte, with the template's own
# line breaks and indentation.
_FLIP_INSTRUCTIONS = (
    "<p>\n"
    "    Keep the pages in the same order, then flip the stack over the long edge\n"
    "    (the left or right side, not the top or bottom).\n"
    "  </p>\n"
    "  <p>Place the flipped stack back in the feeder, then press Continue.</p>"
)
_FLIP_CAPTIONS = (
    "<p><small>Long edge (correct)</small></p>",
    "<p><small>Short edge (incorrect)</small></p>",
)

_FLIP_PROMPT_START = '<div class="flip-prompt">'
_HIDDEN_SVG = re.compile(r"<svg\b[^>]*>")


def _flip_prompt(markup: str) -> str:
    """Return the flip prompt of a status response, from its opening tag on."""
    assert _FLIP_PROMPT_START in markup, markup
    return markup[markup.index(_FLIP_PROMPT_START) :]


def _stub_pass_deadline(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, deadline: datetime | None
) -> None:
    """Make the worker report ``deadline`` for any job's multi-page question."""

    def pass_deadline(_job_id: str) -> datetime | None:
        return deadline

    monkeypatch.setattr(_app(client).state.worker, "pass_deadline", pass_deadline)


def _stub_flip_deadline(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, deadline: datetime | None
) -> None:
    """Make the worker report ``deadline`` for any job's flip wait."""

    def flip_deadline(_job_id: str) -> datetime | None:
        return deadline

    monkeypatch.setattr(_app(client).state.worker, "flip_deadline", flip_deadline)


class TestWaitingCopy:
    """
    What a waiting scan tells the person who can answer it, and everyone else.

    Everyone else is told what the scan waits for, where it can be answered
    and, once the worker knows it, when it gives up -- with no title, no
    preview and no button.  The owner's prompt names the scan and says when
    the wait ends and what happens then.  The deadline is a time and nothing
    else, so it reaches every viewer; the title stays behind the owner gate.
    """

    def test_non_owner_flip_line_has_the_deadline(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An armed flip wait gives another browser its way forward and deadline."""
        job_id = _flip_job(client, _OWNING_BROWSER)
        _arm_flip(client, monkeypatch, job_id)
        deadline = _app(client).state.worker.flip_deadline(job_id)
        assert deadline is not None

        markup = _as_browser(client, None)

        line = non_owner_wait_line(JobState.AWAITING_FLIP, deadline=deadline)
        assert local_time(deadline) in line
        assert f"<p>{escape(line)}</p>" in markup
        assert "flip-continue" not in markup
        assert "Flip Render" not in markup
        assert "<img" not in markup

    def test_non_owner_flip_line_without_a_deadline(self, client: TestClient) -> None:
        """Before the wait's start is recorded, the line names no time."""
        _flip_job(client, _OWNING_BROWSER)

        markup = _as_browser(client, None)

        line = non_owner_wait_line(JobState.AWAITING_FLIP, deadline=None)
        assert f"<p>{escape(line)}</p>" in markup
        assert "stops at" not in markup

    @pytest.mark.parametrize("state", sorted(PASS_WAIT_STATES))
    def test_non_owner_pass_lines_have_the_deadline(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, state: JobState
    ) -> None:
        """Each multi-page wait gets the same shape, with its question's deadline."""
        _job_in_state(client, state)
        _stub_pass_deadline(client, monkeypatch, _DEADLINE)

        markup = _as_browser(client, None)

        line = non_owner_wait_line(state, deadline=_DEADLINE)
        assert f"<p>{escape(line)}</p>" in markup
        assert "<button" not in _status_area_of(markup)
        assert "Render Test" not in markup

    @pytest.mark.parametrize("state", sorted(PASS_WAIT_STATES))
    def test_owner_before_the_question_keeps_the_busy_line(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, state: JobState
    ) -> None:
        """
        The owner, before the question is published, sees the progress line.

        That owner is about to be asked, so the waiting line meant for others
        -- "it can be answered from the device that started this scan" -- would
        be wrong for them.
        """
        _job_in_state(client, state)
        _stub_pass_deadline(client, monkeypatch, _DEADLINE)

        markup = _as_browser(client, _RENDERING_BROWSER)

        assert f"<p>{escape(progress_label(state))}</p>" in markup
        assert "started this scan" not in markup

    def test_owner_flip_prompt_names_the_job_and_deadline(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The prompt opens with the scan's title and ends with the deadline note."""
        _flip_job(client, _OWNING_BROWSER)
        _stub_flip_deadline(client, monkeypatch, _DEADLINE)
        timeout = _app(client).state.settings.output.operator_wait_timeout_seconds

        prompt = _flip_prompt(_as_browser(client, _OWNING_BROWSER))

        heading = flip_heading("Flip Render")
        assert heading == "Flip the stack for “Flip Render”"
        assert prompt.startswith(
            f'{_FLIP_PROMPT_START}\n  <p id="flip-heading"><strong>'
            f"{escape(heading)}</strong></p>"
        )
        note = flip_deadline_note(deadline=_DEADLINE, timeout_seconds=timeout)
        assert local_time(_DEADLINE) in note
        assert f'<small class="prompt-note">{escape(note)}</small>' in prompt
        assert prompt.index("prompt-note") > prompt.index('id="flip-abort"')

    def test_owner_flip_note_names_the_duration_before_the_deadline_is_known(
        self, client: TestClient
    ) -> None:
        """With no recorded start the note names how long the wait lasts."""
        _flip_job(client, _OWNING_BROWSER)
        timeout = _app(client).state.settings.output.operator_wait_timeout_seconds

        prompt = _flip_prompt(_as_browser(client, _OWNING_BROWSER))

        note = flip_deadline_note(deadline=None, timeout_seconds=timeout)
        assert f'<small class="prompt-note">{escape(note)}</small>' in prompt

    def test_flip_group_is_named_and_svgs_are_hidden(self, client: TestClient) -> None:
        """
        The pictures are silent and the buttons are named by the job line.

        The captions under the pictures already say what each one shows, so
        a label on the picture would be read twice.
        """
        _flip_job(client, _OWNING_BROWSER)

        prompt = _flip_prompt(_as_browser(client, _OWNING_BROWSER))

        svgs = _HIDDEN_SVG.findall(prompt)
        assert len(svgs) == 2
        for svg in svgs:
            assert 'aria-hidden="true"' in svg
            assert 'focusable="false"' in svg
        assert "aria-label=" not in prompt
        assert '<div role="group" aria-labelledby="flip-heading">' in prompt
        assert prompt.count('id="flip-heading"') == 1

    def test_null_owner_flip_heading_is_generic(self, client: TestClient) -> None:
        """A job anyone may answer is named by the hidden title, not its own."""
        _flip_job(client, None)

        prompt = _flip_prompt(_as_browser(client, None))

        assert (
            f'<p id="flip-heading"><strong>{escape(flip_heading(HIDDEN_JOB_TITLE))}'
            "</strong></p>"
        ) in prompt
        assert "Flip Render" not in prompt

    def test_flip_prompt_unchanged_copy_is_byte_identical(
        self, client: TestClient
    ) -> None:
        """The flip prompt keeps its exact instructions, captions and confirmation."""
        _flip_job(client, _OWNING_BROWSER)

        prompt = _flip_prompt(_as_browser(client, _OWNING_BROWSER))
        template = template_markup(
            (_TEMPLATES_DIR / "partials" / "flip.html").read_text(encoding="utf-8")
        )

        for source in (prompt, template):
            assert _FLIP_INSTRUCTIONS in source
            for caption in _FLIP_CAPTIONS:
                assert caption in source
        for tags in (
            _rendered_start_tags(prompt),
            _template_tags("partials", "flip.html"),
        ):
            assert ("button", _ABORT_CONFIRMATION) in [
                (tag, attributes.get("hx-confirm")) for tag, attributes in tags
            ]


def _status_area_of(markup: str) -> str:
    """Return a status response's status area, without the out-of-band button."""
    button = _scan_button(markup)
    assert button is not None
    return markup[: button.start]


# --- The blocked Scan button and its reason line ----------------------------

# The reason line's copy, verbatim. Pinned here as a literal so
# a change to the constant is caught by a test and not only by a reviewer; that
# the constant lives in Python and not in a template is asserted below.
_BLOCKED_REASON = (
    "The paperless-ngx API token has not been set \N{EM DASH} see System status above."
)

# The reason element, whole. `<small>` nests nothing here, so a non-greedy body
# is exact rather than merely convenient.
_REASON_LINE = re.compile(
    r'<small id="scan-blocked-reason" class="status-error">(?P<text>.*?)</small>',
    re.DOTALL,
)

# The scan form's attributes, in order. `hx-disinherit` keeps the selects and
# refresh buttons from inheriting `hx-disabled-elt`, which is what it protects,
# so the list is asserted whole rather than by membership.
#
# `method` and `action` are not htmx attributes and nothing inherits them: they
# make a submit with JavaScript off a POST, so the title never lands in a URL,
# and the server refuses that POST with a page.
_SCAN_FORM_ATTRS = [
    ("hx-post", "/api/scan"),
    ("method", "post"),
    ("action", "/api/scan"),
    ("hx-target", "#status-area"),
    ("hx-swap", "outerHTML"),
    ("hx-disabled-elt", "#scan-btn"),
    ("hx-disinherit", "hx-disabled-elt"),
]


def _scan_form_attributes() -> dict[str, str | None]:
    """Return the attributes of the page template's one scan form, as written."""
    forms = [
        attributes
        for tag, attributes in _template_tags("index.html")
        if tag == "form" and attributes.get("hx-post") == "/api/scan"
    ]
    assert len(forms) == 1, forms
    return forms[0]


# The package root, for the assertions that follow the copy rather than markup.
_SRC_DIR = _PACKAGE_DIR.parent


def _source_strings(path: Path) -> list[str]:
    """
    Return the text a source file can put on a page, without its comments.

    Args:
        path: A Python module, a template or a stylesheet.

    Returns:
        A module's string constants, a template's markup and string
        constants, or a stylesheet's text with its comments removed.

    """
    source = path.read_text(encoding="utf-8")
    if path.suffix == ".py":
        return [
            node.value
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        ]
    if path.suffix == ".html":
        return [template_markup(source), *_template_names(_template_tree(path))]
    return [_CSS_COMMENT.sub("", source)]


def _css_rule(selector: str) -> str:
    """
    Return the body of the one CSS rule with `selector`.

    Args:
        selector: The selector, without its opening brace.

    Returns:
        Everything between that rule's braces, without comments.

    """
    bodies = [
        body
        for found, body in _css_rules(_APP_CSS.read_text(encoding="utf-8"))
        if found == selector
    ]
    assert len(bodies) == 1, f"expected exactly one {selector} rule"
    return bodies[0]


def _done_job_that_is_not_current(client: TestClient, title: str) -> str:
    """
    Create a job already DONE, leaving the worker's current job alone.

    `_job_in_state` adopts the row it makes as the worker's current job, which
    is exactly what the two-browser cases below must not do: they need a second
    row that ran and ended while something else is still running. The module's
    own `_finished_job` above records page counts and is about the history
    table, so it is not this.

    Args:
        client: The client whose app owns the store.
        title: The title the status area will report for it.

    Returns:
        The job's id.

    """
    job_store: JobStore = _app(client).state.job_store
    job = job_store.create_job(
        profile="default", title=title, owner_token=_RENDERING_BROWSER
    )
    job_store.update_state(job.id, JobState.DONE)
    return job.id


class TestScanBlocked:
    """
    The placeholder token disables the Scan button and says why.

    The button is the courtesy, never the enforcement: every test here is about
    what a viewer is shown, and the refusal that actually holds is proved
    against the route in tests/test_web_errors.py. Only missing paperless-ngx
    settings block the button -- a scanner that is unreachable or a
    paperless-ngx that is down does not, because the user is allowed to try and
    will get a plain-language error with a next step.
    """

    def test_blocked_idle_button_is_disabled_and_still_says_scan(
        self, blocked_client: TestClient
    ) -> None:
        """A blocked appliance with nothing running still offers one word."""
        match = _only_scan_button(blocked_client.get("/").text)
        attrs = match.attributes

        assert "disabled" in attrs
        assert attrs.get("aria-describedby") == "scan-blocked-reason"
        assert "aria-busy" not in attrs
        assert match.text.strip() == "Scan"

    @pytest.mark.parametrize("state", list(JobState))
    def test_blocked_button_leaves_the_job_label_and_aria_busy_alone(
        self, blocked_client: TestClient, state: JobState
    ) -> None:
        """
        The flag is a second `disabled` source and touches nothing else.

        The label and `aria-busy` still come from the job, for every state.
        """
        _job_in_state(blocked_client, state)
        match = _only_scan_button(blocked_client.get("/").text)
        attrs = match.attributes

        assert "disabled" in attrs
        assert attrs.get("aria-describedby") == "scan-blocked-reason"
        assert (attrs.get("aria-busy") == "true") is (state in BUSY_STATES)

        expected = _scan_caption(state)
        assert match.text.strip() == expected

    def test_an_unblocked_page_carries_no_describedby_and_no_reason(
        self, client: TestClient
    ) -> None:
        """A configured appliance's button names no blocked reason, and none renders."""
        page = client.get("/").text
        match = _only_scan_button(page)

        assert match.attributes.get("aria-describedby") != "scan-blocked-reason"
        assert "scan-blocked-reason" not in page
        assert _REASON_LINE.search(page) is None

    def test_blocked_reason_line_renders_the_exact_copy(
        self, blocked_client: TestClient
    ) -> None:
        """The reason is the blocked sentence, once, as visible text."""
        page = blocked_client.get("/").text
        rendered = _REASON_LINE.findall(page)

        assert len(rendered) == 1
        assert rendered[0].strip() == _BLOCKED_REASON

    def test_an_unset_url_blocks_the_button_and_names_the_address(
        self, url_unset_client: TestClient
    ) -> None:
        """The second condition that blocks the button gets its own reason."""
        page = url_unset_client.get("/").text
        match = _only_scan_button(page)
        rendered = _REASON_LINE.findall(page)

        assert "disabled" in match.attributes
        assert match.attributes.get("aria-describedby") == "scan-blocked-reason"
        assert [text.strip() for text in rendered] == [
            "The paperless-ngx address has not been set \N{EM DASH} "
            "see System status above."
        ]

    def test_blocked_reason_line_follows_the_button_inside_the_form(
        self, blocked_client: TestClient
    ) -> None:
        """It reads directly after the control it explains, inside the form."""
        page = blocked_client.get("/").text
        form = page[page.index("<form hx-post=") : page.index("</form>")]

        assert 'id="scan-btn"' in form
        assert 'id="scan-blocked-reason"' in form
        assert form.index('id="scan-btn"') < form.index('id="scan-blocked-reason"')

    def test_blocked_button_stays_disabled_across_repeated_status_polls(
        self, blocked_client: TestClient
    ) -> None:
        """
        No status response can hand back an enabled button.

        The button is re-rendered from server state every second, so a blocked
        flag expressed anywhere but the one partial would be dropped by exactly
        these polls.
        """
        for _ in range(5):
            match = _only_scan_button(
                blocked_client.get("/api/jobs/current/status").text
            )
            assert "disabled" in match.attributes
            assert match.attributes.get("aria-describedby") == "scan-blocked-reason"

    def test_blocked_followed_job_poll_carries_the_blocked_button(
        self, blocked_client: TestClient
    ) -> None:
        """The per-browser poll route carries it too, not just the shared one."""
        job_id = _job_in_state(blocked_client, JobState.DONE)
        match = _only_scan_button(blocked_client.get(f"/api/jobs/{job_id}/status").text)

        assert "disabled" in match.attributes
        assert match.attributes.get("aria-describedby") == "scan-blocked-reason"

    @pytest.mark.parametrize("route", ["continue", "abort"], ids=["continue", "abort"])
    def test_blocked_flip_responses_carry_the_blocked_button(
        self, blocked_client: TestClient, route: str
    ) -> None:
        """Both flip answers re-render the button, and both keep it blocked."""
        job_id = _job_in_state(blocked_client, JobState.AWAITING_FLIP)
        response = blocked_client.post(f"/api/flip/{route}", data={"job_id": job_id})
        match = _only_scan_button(response.text)

        assert "disabled" in match.attributes
        assert match.attributes.get("aria-describedby") == "scan-blocked-reason"

    def test_checks_refresh_carries_no_button_and_no_blocked_reason(
        self, blocked_client: TestClient
    ) -> None:
        """
        Re-running the checks cannot change a verdict read from Settings.

        So the refresh response carries neither the button nor the reason:
        re-rendering them there would be theatre, and it is why
        `#scan-blocked-reason` is never an out-of-band swap target and never
        has to exist as an empty placeholder.
        """
        text = blocked_client.post("/api/checks/refresh").text

        assert 'id="scan-btn"' not in text
        assert 'id="scan-blocked-reason"' not in text

    def test_blocked_reason_is_never_an_out_of_band_target(
        self, blocked_client: TestClient
    ) -> None:
        """
        The reason element exists only on the full page and never swaps itself.

        The *id* still appears in every status response, as the button's
        `aria-describedby` value: that is the point of the attribute. What must
        never appear is a second copy of the element, out-of-band or otherwise.
        """
        page = blocked_client.get("/").text
        reason = _REASON_LINE.search(page)
        assert reason is not None
        assert "hx-swap-oob" not in reason.group(0)
        assert page.count('id="scan-blocked-reason"') == 1

        for path in ("/api/jobs/current/status", "/api/jobs/history", "/api/checks"):
            text = blocked_client.get(path).text
            assert 'id="scan-blocked-reason"' not in text
            assert _REASON_LINE.search(text) is None

    def test_the_blocked_page_renders_the_one_submit_scan_button(
        self, blocked_client: TestClient
    ) -> None:
        """The partial's one button submits the scan form, and the page has one."""
        buttons = [
            attributes
            for tag, attributes in _template_tags("partials", "scan_button.html")
            if tag == "button"
        ]
        assert len(buttons) == 1
        assert buttons[0].get("type") == "submit"
        assert buttons[0].get("id") == "scan-btn"

        button = _only_scan_button(blocked_client.get("/").text)
        assert button.attributes.get("type") == "submit"
        assert button.text.strip() == "Scan"

    def test_the_scan_form_element_gains_no_attribute(self) -> None:
        """
        The scan form carries exactly its own attributes, in order.

        The blocked state lives in the button partial and nowhere else, so the
        form's ``hx-disinherit`` list never has to grow.
        """
        assert list(_scan_form_attributes().items()) == _SCAN_FORM_ATTRS

    def test_no_template_reaches_for_aria_disabled_or_a_tooltip(self) -> None:
        """
        The reason is visible text, not a tooltip and not `aria-disabled`.

        A `disabled` button is not focusable and cannot be reliably hovered, so
        a `title` is unreachable by keyboard and unreliable on touch. Switching
        to `aria-disabled="true"` to make it focusable is forbidden: it would
        break the button's `disabled` contract and `hx-disabled-elt`.
        """
        offenders = [
            (path.name, tag)
            for path, tag, attributes in _all_template_tags()
            if "aria-disabled" in attributes
        ]
        assert offenders == []
        assert [
            tag
            for tag, attributes in _template_tags("partials", "scan_button.html")
            if "title" in attributes
        ] == []

    def test_the_blocked_reason_copy_lives_in_python_and_in_no_template(self) -> None:
        """
        The sentence is a developer constant, never composed in a template.

        It names the problem and nothing else: not the token value, not the
        paperless-ngx URL, which may carry credentials, and no exception text
        (ASVS V7).  Modules are read for their string constants and templates
        and the stylesheet for their text without comments.
        """
        carrying = sorted(
            path
            for path in _SRC_DIR.rglob("*")
            if path.is_file()
            and path.suffix in {".py", ".html", ".css"}
            and any(_BLOCKED_REASON in text for text in _source_strings(path))
        )

        assert len(carrying) == 1, carrying
        assert carrying[0].suffix == ".py"

    def test_the_blocked_reason_rule_adds_no_colour(self) -> None:
        """
        The line is a block with a top margin and borrows `.status-error`.

        `--pico-del-color` on the card measures 7.83:1 light and 5.60:1 dark,
        already asserted in tests/test_browser.py. No new token, no new value.
        """
        rule = _css_rule("#scan-blocked-reason")

        assert "display: block;" in rule
        assert "margin-top: 0.5rem;" in rule
        assert "color" not in rule
        assert "#" not in rule


class TestScanButtonFollowsTheRenderedJob:
    """
    The button's job-derived state describes the job the page is reporting.

    The status area follows the job *this* browser submitted, and the
    out-of-band Scan button is rendered from the same context, so a browser
    whose own job has finished sees an enabled button while somebody else's
    job is still running.  Keying `disabled` on another job would let the
    button read "Scanning..." directly above "Done: ...", the page
    contradicting itself.  The appliance queues, so that browser may start
    another scan and is told where it lands; the worker's QUEUE_FULL refusal
    and its REJECTED row guard against over-submission.
    """

    def test_a_finished_followed_job_leaves_the_button_enabled(
        self, client: TestClient
    ) -> None:
        """A browser whose own scan is done may start another one."""
        _job_in_state(client, JobState.SCANNING)
        finished = _done_job_that_is_not_current(client, "Mine, Already Done")

        text = client.get(f"/api/jobs/{finished}/status").text
        match = _only_scan_button(text)

        assert "Mine, Already Done" in text
        assert "disabled" not in match.attributes
        assert match.text.strip() == "Scan"

    def test_a_browser_that_submitted_nothing_still_sees_the_appliance_busy(
        self, client: TestClient
    ) -> None:
        """The shared poll route is unchanged: it reports the running job."""
        _job_in_state(client, JobState.SCANNING)
        _done_job_that_is_not_current(client, "Somebody Else's, Already Done")

        match = _only_scan_button(client.get("/api/jobs/current/status").text)

        assert "disabled" in match.attributes
        assert match.text.strip() == "Scanning…"

    def test_the_button_never_describes_a_job_the_status_area_is_not_showing(
        self, client: TestClient
    ) -> None:
        """One response, one job: the label and the prose cannot disagree."""
        _job_in_state(client, JobState.SCANNING)
        finished = _done_job_that_is_not_current(client, "Mine, Already Done")

        text = client.get(f"/api/jobs/{finished}/status").text

        assert "Render Test" not in text
        assert "Scanning…" not in text

    def test_a_blocked_appliance_disables_even_a_finished_followed_job(
        self, blocked_client: TestClient
    ) -> None:
        """
        The placeholder flag is independent of which job is rendered.

        It is the OR that makes the courtesy hold for every viewer, whatever
        their own job did.
        """
        _job_in_state(blocked_client, JobState.SCANNING)
        finished = _done_job_that_is_not_current(blocked_client, "Mine, Already Done")

        match = _only_scan_button(
            blocked_client.get(f"/api/jobs/{finished}/status").text
        )

        assert "disabled" in match.attributes
        assert match.attributes.get("aria-describedby") == "scan-blocked-reason"


# Every help line on the scan form, keyed by the id the control points at.
# Profile is deliberately not here: its help line is the live profile
# description, and a second one would be a second line under one control.
_HELP_TEXT = {
    "title-help": "What this document should be called in paperless-ngx.",
    "tag-filter-help": "Type to narrow the list. Ticked tags stay ticked.",
    "tags-help": "Labels to file this under in paperless-ngx. Optional.",
    "correspondent-help": "Who sent this document? Optional.",
}

# The same lines as the page renders them before its lists arrive: the
# correspondent's help line says the list is loading until the lazy list load
# sends the sentence above in its place.
_PAGE_HELP_TEXT = {**_HELP_TEXT, "correspondent-help": CORRESPONDENTS_LOADING}

# The identified help slots the idle page renders, in document order. Profile's
# leads because its control does; `#scan-blocked-reason` is absent because this
# appliance's token is real, and the tag list's empty-state line carries no id
# because it is a state of the list and not a help line for a control.  The
# Scan button's hold line closes the list: it is the button's reason, like the
# blocked one, and stands on the page until the lists arrive.
_HELP_SLOT_IDS = [
    "profile-description",
    "multi-page-help",
    "title-help",
    "tag-filter-help",
    "tags-help",
    "correspondent-help",
    "scan-hold-reason",
]

# Pico styles a help line through `:where(input,select,textarea,fieldset)+small`,
# so each slot has to be its control's *adjacent* sibling. The markup that must
# sit immediately before each one, whitespace aside.
_HELP_ADJACENCY = [
    (r"</fieldset>", "multi-page-help"),
    (r'id="title-input"[^>]*>', "title-help"),
    (r'id="tag-filter"[^>]*>', "tag-filter-help"),
    (r"</fieldset>", "tags-help"),
    (r"</select>", "correspondent-help"),
]

# The filter box, whole, as the page renders it.
_TAG_FILTER_INPUT = re.compile(r'<input[^>]*id="tag-filter"[^>]*>')

# The filter's own form: empty, and with no submit button of its own.
_TAG_FILTER_FORM = re.compile(
    r'<form id="tag-filter-form"(?P<attrs>[^>]*)>(?P<body>.*?)</form>', re.DOTALL
)

# The tag list's wrapper, which the partial renders and every swap replaces.
_TAGS_LIST = re.compile(r'<div id="tags-list"[^>]*>')
_CORRESPONDENT_SELECT = re.compile(
    r'<select name="correspondent" id="correspondent-select"(?P<attrs>[^>]*)>'
)


class TestFormHelpTextAndTagPicker:
    """
    One plain-words line per control, and a filter that cannot scan.

    Two of these assertions exist because of hazards the design removes rather
    than guards against. The filter input's HTML form owner is a separate empty
    form, so Enter in it filters instead of starting a scan and a scan can never
    carry the filter text; and nothing at all is added to the scan form
    element, so its ``hx-disinherit`` list stays as it is.
    """

    def test_every_control_has_one_help_line_wired_with_aria_describedby(
        self, client: TestClient
    ) -> None:
        """
        Four sentences, four slots, four references -- one each.

        The correspondent's sentence reaches its slot with the lazy list load,
        out of band; until then the slot says the list is loading.
        """
        page = client.get("/").text
        lists = load_the_lists(client, page)

        for slot, sentence in _PAGE_HELP_TEXT.items():
            assert page.count(f'<small id="{slot}">{sentence}</small>') == 1, slot
            assert page.count(f'aria-describedby="{slot}"') == 1, slot
        help_line = _HELP_TEXT["correspondent-help"]
        assert (
            lists.count(
                f'<small id="correspondent-help" hx-swap-oob="true">{help_line}</small>'
            )
            == 1
        )

    def test_the_help_lines_are_the_only_identified_small_elements(
        self, client: TestClient
    ) -> None:
        """
        Exactly six slots, in order, and Profile's is the live description.

        Asserted as the whole list rather than by membership, so a second help
        line under any one control fails here -- Profile's included, where the
        live profile description already is the line.
        """
        page = client.get("/").text

        assert re.findall(r'<small id="([^"]+)"', page) == _HELP_SLOT_IDS

    def test_each_help_line_is_its_control_s_adjacent_sibling(
        self, client: TestClient
    ) -> None:
        """Pico's `+small` rule is what makes a help line look like one."""
        page = client.get("/").text

        for before, slot in _HELP_ADJACENCY:
            assert re.search(before + r'\s*<small id="' + slot + '"', page), slot

    def test_the_title_placeholder_survives_its_new_help_line(
        self, client: TestClient
    ) -> None:
        """The two say different things, so neither replaces the other."""
        match = _TITLE_INPUT.search(client.get("/").text)

        assert match is not None
        assert 'placeholder="Document title (auto-generated if empty)"' in match.group(
            0
        )
        assert 'aria-describedby="title-help"' in match.group(0)

    def test_the_tag_filter_input_carries_its_form_owner_and_htmx_wiring(
        self, client: TestClient
    ) -> None:
        """The filter input carries its seven attributes, the form owner first."""
        match = _TAG_FILTER_INPUT.search(client.get("/").text)

        assert match is not None, "tag filter input not rendered"
        for attribute in (
            'type="search"',
            'name="q"',
            'form="tag-filter-form"',
            'hx-get="/api/tags"',
            'hx-target="#tags-list"',
            'hx-swap="outerHTML"',
            'hx-trigger="input changed delay:300ms, search"',
            'hx-include="#tags-list"',
        ):
            assert attribute in match.group(0), attribute

    def test_tag_filter_trigger_length_and_label(self, client: TestClient) -> None:
        """
        The filter follows every edit, is bounded, and has a real name.

        ``input`` catches a paste, a dictation and an autocorrection, which
        ``keyup`` misses; ``search`` is what the box's own clear button fires.
        The length is the route's own bound, so the box never sends what the
        route refuses.  The visually hidden label says what the placeholder
        says, so the spoken name and the visible hint agree.
        """
        page = client.get("/").text
        match = _TAG_FILTER_INPUT.search(page)

        assert match is not None, "tag filter input not rendered"
        tag = match.group(0)
        assert 'hx-trigger="input changed delay:300ms, search"' in tag
        assert "keyup" not in tag
        assert f'maxlength="{TAG_FILTER_MAX_LENGTH}"' in tag
        label = (
            f'<label for="tag-filter" class="sr-only">'
            f"{html.escape(TAG_FILTER_LABEL)}</label>"
        )
        assert page.count(label) == 1
        assert re.search(rf"{re.escape(label)}\s*<input[^>]*id=\"tag-filter\"", page)

    def test_refresh_buttons_sit_outside_legend_and_label(
        self, client: TestClient
    ) -> None:
        """
        The legend names the tags, the label the correspondent, and nothing else.

        A button inside a legend or a label becomes part of the name its
        control is announced by.  The buttons keep everything they had, and
        gain ids so focus can be returned to them after their swap.
        """
        page = client.get("/").text

        legend = re.search(r"<legend>(?P<body>.*?)</legend>", page, re.DOTALL)
        assert legend is not None, page
        assert legend.group("body") == "Tags"
        label = re.search(
            r'<label for="correspondent-select">(?P<body>.*?)</label>', page, re.DOTALL
        )
        assert label is not None, page
        assert label.group("body") == "Correspondent"
        for button_id, resource, name in (
            ("tags-refresh", "tags", "Refresh tags"),
            ("correspondents-refresh", "correspondents", "Refresh correspondents"),
        ):
            assert page.count(f'id="{button_id}"') == 1, button_id
            button = re.search(
                rf'<button type="button" id="{button_id}"(?P<attrs>[^>]*)>'
                r"(?P<body>.*?)</button>",
                page,
                re.DOTALL,
            )
            assert button is not None, button_id
            attrs = button.group("attrs")
            assert f'hx-post="/api/cache/invalidate?resource={resource}"' in attrs
            assert 'class="refresh-btn"' in attrs
            assert f'title="{name}"' in attrs
            assert f'aria-label="{name}"' in attrs
            assert (
                button.group("body") == f'&#x21bb;<span class="sr-only">{name}</span>'
            )
        heading = re.search(
            r'<div class="field-heading">\s*<label for="correspondent-select">'
            r'Correspondent</label>\s*<button type="button" id="correspondents-refresh"',
            page,
        )
        assert heading is not None, page

    def test_the_tag_filter_form_is_an_empty_sibling_after_the_scan_form(
        self, client: TestClient
    ) -> None:
        """
        Enter in the filter filters, because the input belongs to this form.

        It sits after the scan form's closing tag and before the Scan article
        ends, so it is a sibling and not a nested form -- which HTML forbids.
        """
        page = client.get("/").text
        scan_form_close = page.index("</form>")
        filter_form_open = page.index('<form id="tag-filter-form"')

        assert filter_form_open > scan_form_close
        assert "</article>" not in page[scan_form_close:filter_form_open]

        match = _TAG_FILTER_FORM.search(page)
        assert match is not None
        assert match.group("body").strip() == ""
        assert "<button" not in match.group("body")
        for attribute in (
            'hx-get="/api/tags"',
            'hx-target="#tags-list"',
            'hx-swap="outerHTML"',
            'hx-include="#tags-list"',
        ):
            assert attribute in match.group("attrs"), attribute

    def test_the_tag_list_wrapper_asks_for_nothing_until_the_profile_changes(
        self, client: TestClient
    ) -> None:
        """
        The page's loader brings the list; the wrapper asks only on a change.

        The page's one loader fetches both lists together, so a load trigger
        on the wrapper would only fetch the same list again.  Its one request
        is the profile-change refresh, which ticks the new profile's
        defaults.  The filter box, the refresh button and the filter form each
        keep their own.
        """
        page = client.get("/").text
        match = _TAGS_LIST.search(page)

        assert match is not None, "tag list wrapper not rendered"
        wrapper = match.group(0)
        assert re.findall(r'hx-trigger="([^"]*)"', wrapper) == [
            "change from:#profile-select"
        ]
        assert re.findall(r'hx-get="([^"]*)"', wrapper) == ["/api/profiles/tags"]
        assert 'hx-post="/api/cache/invalidate?resource=tags"' in page
        assert 'hx-get="/api/tags" hx-target="#tags-list"' in page

    def test_the_correspondent_select_asks_for_nothing_until_the_profile_changes(
        self, client: TestClient
    ) -> None:
        """
        The options arrive with the lists; the select asks only on a change.

        The page's one loader fetches both lists together, so the select
        asking for its own options once parsed would repeat that work.  The
        select's one request is the profile-change refresh, which selects the
        new profile's default, and the lazy list load's select carries the
        same wiring.
        """
        _app(client).state.cache.set("correspondents", [{"id": 7, "name": "Acme"}])
        page = client.get("/").text
        lists = load_the_lists(client, page)

        for markup in (page, lists):
            match = _CORRESPONDENT_SELECT.search(markup)
            assert match is not None, "correspondent select not rendered"
            attrs = match.group("attrs")
            assert re.findall(r'hx-trigger="([^"]*)"', attrs) == [
                "change from:#profile-select"
            ]
            assert re.findall(r'hx-get="([^"]*)"', attrs) == [
                "/api/profiles/correspondent"
            ]
        assert '<option value="7">Acme</option>' not in page
        assert '<option value="7">Acme</option>' in lists
        assert 'hx-post="/api/cache/invalidate?resource=correspondents"' in page

    def test_the_tag_block_adds_no_attribute_to_the_scan_form(self) -> None:
        """
        The tag block leaves the scan form with exactly its own attributes.

        This is the decisive advantage of the form-owner attribute over
        `hx-params="not q"` on the form, which would need the `hx-disinherit`
        list extended.
        """
        assert list(_scan_form_attributes().items()) == _SCAN_FORM_ATTRS
        assert [
            tag
            for tag, attributes in _template_tags("index.html")
            if "hx-confirm" in attributes
        ] == []

    def test_noscript_line_follows_the_scan_heading(self, client: TestClient) -> None:
        """
        With JavaScript off the page says why Scan will not work, first.

        The line sits directly under the Scan heading, ahead of every control,
        so it is read before anything that would not work.  It is the amber
        fallback line with its warning glyph, and its words come from the
        vocabulary.
        """
        page = client.get("/").text

        noscript = (
            '<noscript><p class="status-fallback">&#9888; '
            f"{html.escape(NO_SCRIPT_LINE)}</p></noscript>"
        )
        assert page.count("<noscript>") == 1
        assert re.search(rf"<h2>Scan</h2>\s*{re.escape(noscript)}", page), page

    def test_no_template_keeps_the_tag_multi_select(self) -> None:
        """No template renders a tag multi-select: the tag list is the one control."""
        offenders = [
            (path.name, tag)
            for path, tag, attributes in _all_template_tags()
            if attributes.get("name") == "tags"
            and (tag == "select" or "multiple" in attributes)
        ]
        assert offenders == []

    def test_the_tag_and_correspondent_refresh_buttons_survive_the_rewrite(
        self, client: TestClient
    ) -> None:
        """One refresh button per list; the tag one swaps the list, filter included."""
        page = client.get("/").text

        assert page.count('aria-label="Refresh tags"') == 1
        assert page.count('aria-label="Refresh correspondents"') == 1

        refresh = re.search(
            r'<button type="button"[^>]*resource=tags[^>]*>', page, re.DOTALL
        )
        assert refresh is not None
        for attribute in (
            'hx-target="#tags-list"',
            'hx-swap="outerHTML"',
            'hx-include="#tag-filter, #tags-list"',
        ):
            assert attribute in refresh.group(0), attribute


# The htmx configuration, captured whole: the attribute is single-quoted so the
# JSON inside it can use double quotes, which is how base.html writes it.
_HTMX_CONFIG = re.compile(r"<meta name=\"htmx-config\"\s+content='(?P<json>[^']*)'>")

# The Check again button, captured whole so an attribute found elsewhere on the
# page cannot satisfy an assertion about this one.
_CHECK_AGAIN = re.compile(r'<button type="button"[^>]*class="check-refresh[^"]*"[^>]*>')


def test_htmx_config_sets_a_request_timeout(client: TestClient) -> None:
    """
    Every htmx request is abandoned after 20 s, and nothing else changed.

    A stalled or half-open request would otherwise hold its element forever:
    a poll that never answers never asks again.  htmx merges the meta
    shallowly, so the timeout is added beside every existing key rather than
    replacing any of them; the response handling and the three eval and style
    switches are pinned here so that adding a key cannot quietly drop one.
    """
    match = _HTMX_CONFIG.search(client.get("/").text)
    assert match is not None, "htmx-config meta not rendered"
    config = json.loads(match.group("json"))

    assert config["timeout"] == 20000
    assert config["allowEval"] is False
    assert config["allowScriptTags"] is False
    assert config["includeIndicatorStyles"] is False
    assert config["responseHandling"] == [
        {"code": "204", "swap": False},
        {"code": "[23]..", "swap": True},
        {"code": "[45]..", "swap": True, "error": True},
    ]


def test_check_again_has_a_stable_id_and_a_timeout_above_its_wait(
    client: TestClient,
) -> None:
    """
    Check again keeps focus across its own swap and outlasts the route's wait.

    htmx restores focus by id after an outerHTML swap, so the id is what keeps
    a keyboard user on the button once ``#checks-body`` is replaced.  The
    route waits a bounded time for the refresher's probe and then answers
    either way, so the request the browser makes must not give up first.
    The effective timeout is the button's own when it carries one and the
    page-wide one otherwise, and the button carries no three-minute override.
    """
    page = client.get("/").text
    assert page.count('id="checks-refresh"') == 1

    match = _CHECK_AGAIN.search(page)
    assert match is not None, "Check again button not rendered"
    button = match.group(0)
    assert 'id="checks-refresh"' in button
    assert "180000" not in button

    config = _HTMX_CONFIG.search(page)
    assert config is not None, "htmx-config meta not rendered"
    timeout_ms = json.loads(config.group("json"))["timeout"]
    request = re.search(r"hx-request='(?P<json>[^']*)'", button)
    if request is not None:
        timeout_ms = json.loads(request.group("json"))["timeout"]
    assert timeout_ms > CHECK_AGAIN_WAIT_SECONDS * 1000


@pytest.mark.parametrize("state", list(JobState))
def test_history_never_emits_an_empty_class(
    client: TestClient, state: JobState
) -> None:
    """
    A row with no status colour has no class attribute at all.

    ``job_status_class`` returns an empty string for a job still in flight,
    and an attribute holding nothing is noise in the markup.  The four
    terminal classes still render; ``test_history_cell_css_class`` pins them.
    """
    _job_in_state(client, state)
    history = client.get("/api/jobs/history").text
    page = client.get("/").text

    assert 'class=""' not in history
    assert 'class=""' not in page
    if not job_status_class(state, None, _DEFAULT_ERROR_CATEGORY):
        label = job_label(state, None, _DEFAULT_ERROR_CATEGORY)
        assert f"<td>\n    {label}\n  </td>" in history


def test_page_title_defaults_to_saneless(client: TestClient) -> None:
    """The tab reads ``saneless`` unless a page sets its own title."""
    assert "<title>saneless</title>" in client.get("/").text


def test_stylesheet_has_no_dead_fallbacks_or_deprecated_clip(
    client: TestClient,
) -> None:
    """
    The served stylesheet carries no dead or deprecated declaration.

    Pico always defines its ins and del colours, so a named-colour fallback
    behind them can never apply and only misleads a reader about what renders.
    ``clip`` is deprecated in favour of ``clip-path``.  The fixed table layout
    and ``word-break: break-word`` are what split history words across lines
    on a phone, so neither may come back.
    """
    response = client.get("/static/app.css")
    assert response.status_code == 200
    rules = _css_rules(response.text)
    declarations = [
        (name, value)
        for _, body in rules
        for name, value in _declarations(body).items()
    ]

    named_fallbacks = [
        value for _, value in declarations if ", green)" in value or ", red)" in value
    ]
    assert named_fallbacks == []
    assert ("table-layout", "fixed") not in declarations
    assert ("word-break", "break-word") not in declarations
    assert [name for name, _ in declarations if name == "clip"] == []

    sr_only = [
        _declarations(body) for selector, body in rules if selector == ".sr-only"
    ]
    assert len(sr_only) == 1, ".sr-only rule not found"
    assert sr_only[0].get("clip-path") == "inset(50%)"


# --- Where focus goes after an action (the focus map) -----------------------

# The one value of the poll's ``focus`` parameter the server acts on.  Written
# out rather than imported, so a renamed constant is a failing test and not a
# silent change to every URL a browser already holds.
_FOCUS_SCAN = "scan"

# The multi-page answers after which focus goes to the status area.  Abort is
# the one answer that sends it to the Scan button, and has its own tests.
_STATUS_AREA_ANSWERS = (
    PassAnswer.NEXT,
    PassAnswer.RESCAN,
    PassAnswer.SKIP_BLANKS,
    PassAnswer.KEEP_BLANKS,
    PassAnswer.FINISH,
)

# The actions whose first following poll is checked for a 204.
_FOCUS_ACTIONS = ("scan", "continue", "abort", "multi-page answer")

_HX_GET = re.compile(r'hx-get="(?P<url>[^"]*)"')
_AUTOFOCUS = re.compile(r"\sautofocus\b")


def _status_open_tag(markup: str) -> str:
    """Return the status area's opening tag, failing when there is none."""
    match = _STATUS_OPEN.search(markup)
    assert match is not None, markup
    return match.group(0)


def _status_poll_url(markup: str) -> str:
    """Return the URL the status area polls, as the browser would request it."""
    match = _HX_GET.search(_status_open_tag(markup))
    assert match is not None, markup
    return html.unescape(match.group("url"))


def _status_poll_query(markup: str) -> dict[str, list[str]]:
    """Return the query the status area's poll URL carries."""
    return parse_qs(urlsplit(_status_poll_url(markup)).query)


def _takes_focus(tag_or_attrs: str) -> bool:
    """Report whether an opening tag, or a tag's attributes, carry ``autofocus``."""
    return _AUTOFOCUS.search(tag_or_attrs) is not None


def _accept_every_submit(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the worker accept every submit without running a scan."""

    def accept(*_args: object, **_kwargs: object) -> SubmitResult:
        return SubmitResult.ACCEPTED

    monkeypatch.setattr(_app(client).state.worker, "submit", accept)


def _arm_flip(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, job_id: str
) -> WorkerFlipCoordinator:
    """
    Give the worker an armed flip wait for ``job_id``, as a scan would.

    Set through ``monkeypatch``, so the worker has no flip wait again once the
    test ends.

    Returns:
        The armed coordinator.

    """
    coordinator = WorkerFlipCoordinator(job_id)
    coordinator.arm()
    monkeypatch.setattr(_app(client).state.worker, "_flip_coordinator", coordinator)
    return coordinator


def _post_answer(client: TestClient, job_id: str, answer: PassAnswer) -> str:
    """Post one multi-page answer as a prompt button would; return the 200 body."""
    response = client.post(
        "/api/multi-page/answer",
        data={"job_id": job_id, "prompt": "1", "answer": answer.value},
        headers=_HX_REQUEST,
    )
    assert response.status_code == 200, response.text
    return response.text


def _flip_answer_response(client: TestClient, job_id: str, answer: str) -> str:
    """Post a flip answer, ``continue`` or ``abort``; return the 200 body."""
    response = client.post(
        f"/api/flip/{answer}", data={"job_id": job_id}, headers=_HX_REQUEST
    )
    assert response.status_code == 200, response.text
    return response.text


class TestFocusEmission:
    """
    The server places focus where the focus map says, and nowhere else.

    Focus moves with no script of the page's own: htmx focuses an element
    carrying ``autofocus`` in the content it has just swapped in, after it has
    restored focus by id.  So an action's response puts ``autofocus`` on the
    status area, an Abort asks for it on the Scan button through its poll URL
    once that button can take it, and neither a page load nor a plain poll
    moves focus at all.
    """

    def test_focus_map_scan_autofocuses_the_status_area(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An accepted Scan moves focus to the status area and asks for no more."""
        _accept_every_submit(client, monkeypatch)

        response = client.post(
            "/api/scan",
            data={"profile": "default", "title": "Focus Test"},
            headers=_HX_REQUEST,
        )

        assert response.status_code == 200, response.text
        assert _takes_focus(_status_open_tag(response.text))
        assert "autofocus" not in _only_scan_button(response.text).attributes
        assert "focus" not in _status_poll_query(response.text)

    def test_focus_map_continue_autofocuses_the_status_area(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A claimed Continue moves focus to the status area."""
        job_id = _job_in_state(client, JobState.AWAITING_FLIP)
        _arm_flip(client, monkeypatch, job_id)

        text = _flip_answer_response(client, job_id, "continue")

        assert str(escape(flip_answer_label(FlipOutcome.CONTINUED))) in text
        assert _takes_focus(_status_open_tag(text))
        assert "focus" not in _status_poll_query(text)

    @pytest.mark.parametrize("answer", _STATUS_AREA_ANSWERS, ids=str)
    def test_focus_map_multi_page_answers_autofocus_the_status_area(
        self, client: TestClient, answer: PassAnswer
    ) -> None:
        """Scan next, Re-scan, Skip, Keep and Finish move focus to the status area."""
        job_id = _job_in_state(client, pass_wait_state(PassWait.NEXT_PASS))

        text = _post_answer(client, job_id, answer)

        assert _takes_focus(_status_open_tag(text))
        assert "focus" not in _status_poll_query(text)
        assert "autofocus" not in _only_scan_button(text).attributes

    def test_focus_map_claimed_abort_bakes_focus_scan(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A claimed Abort focuses the status area and asks its poll for Scan.

        The Scan button is still disabled while the scan winds down, and a
        disabled button cannot take focus, so the response holds focus on the
        status area for the interim and the poll carries the request on.
        """
        job_id = _job_in_state(client, JobState.AWAITING_FLIP)
        _arm_flip(client, monkeypatch, job_id)

        text = _flip_answer_response(client, job_id, "abort")

        assert str(escape(flip_answer_label(FlipOutcome.ABORTED))) in text
        assert _takes_focus(_status_open_tag(text))
        assert _status_poll_query(text)["focus"] == [_FOCUS_SCAN]
        button = _only_scan_button(text).attributes
        assert "disabled" in button
        assert "autofocus" not in button

    def test_focus_map_unclaimed_abort_bakes_nothing(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        An Abort this browser may not give asks nobody for Scan.

        The job is somebody else's, so the answer is dropped and the scan goes
        on; nothing asks for the Scan button, now or on a later poll.
        """
        job_id = _flip_job(client, _OWNING_BROWSER)
        _arm_flip(client, monkeypatch, job_id)

        text = _flip_answer_response(client, job_id, "abort")

        assert _app(client).state.worker.flip_answer(job_id) is None
        assert "focus" not in _status_poll_query(text)
        assert "autofocus" not in _only_scan_button(text).attributes

    def test_an_abort_claimed_after_the_job_ended_focuses_scan_at_once(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        When the rendered Scan button is already enabled, it takes focus now.

        htmx focuses the main swap's ``autofocus`` after the out-of-band
        ones, so a status area asking for focus as well would win; it asks
        for none.
        """
        job_id = _job_in_state(client, JobState.AWAITING_FLIP)
        _arm_flip(client, monkeypatch, job_id)
        store: JobStore = _app(client).state.job_store
        store.update_state(job_id, JobState.CANCELLED)

        text = _flip_answer_response(client, job_id, "abort")

        button = _only_scan_button(text).attributes
        assert "disabled" not in button
        assert "autofocus" in button
        assert not _takes_focus(_status_open_tag(text))

    def test_focus_scan_autofocuses_an_enabled_scan_button_only(
        self, client: TestClient
    ) -> None:
        """
        ``focus=scan`` focuses Scan on the first rendering that can take it.

        While the job is active the button is disabled, so the request is
        carried on in the next poll URL; once the job has ended the button is
        enabled and takes focus.  A poll that did not ask never moves it.
        """
        active = _job_in_state(client, JobState.AWAITING_FLIP)
        waiting = client.get(
            f"/api/jobs/{active}/status", params={"focus": _FOCUS_SCAN}
        ).text
        waiting_button = _only_scan_button(waiting).attributes
        assert "disabled" in waiting_button
        assert "autofocus" not in waiting_button
        assert _status_poll_query(waiting)["focus"] == [_FOCUS_SCAN]

        ended = _job_in_state(client, JobState.CANCELLED)
        asked = client.get(
            f"/api/jobs/{ended}/status", params={"focus": _FOCUS_SCAN}
        ).text
        asked_button = _only_scan_button(asked).attributes
        assert "disabled" not in asked_button
        assert "autofocus" in asked_button
        assert not _takes_focus(_status_open_tag(asked))

        plain = client.get(f"/api/jobs/{ended}/status").text
        assert "autofocus" not in plain

    @pytest.mark.parametrize(
        "value", ["SCAN", "zz-focus-probe", '"><b>zz</b>', "scan,scan"], ids=repr
    )
    def test_unknown_focus_value_is_ignored(
        self, client: TestClient, value: str
    ) -> None:
        """Only the one constant is acted on, and no value is ever echoed."""
        active = _job_in_state(client, JobState.AWAITING_FLIP)
        waiting = client.get(f"/api/jobs/{active}/status", params={"focus": value})
        ended = _job_in_state(client, JobState.CANCELLED)
        asked = client.get(f"/api/jobs/{ended}/status", params={"focus": value})

        assert waiting.status_code == asked.status_code == 200
        assert "focus" not in _status_poll_query(waiting.text)
        assert "autofocus" not in _only_scan_button(asked.text).attributes
        for text in (waiting.text, asked.text):
            assert value not in text
            assert str(escape(value)) not in text

    @pytest.mark.parametrize("state", list(JobState))
    def test_page_and_plain_polls_never_autofocus(
        self, client: TestClient, state: JobState
    ) -> None:
        """
        A page load moves no focus, and a plain poll never focuses the area.

        A poll that focused the status area would pull focus out of the Title
        box once a second while a scan runs.
        """
        job_id = _job_in_state(client, state)

        assert "autofocus" not in client.get("/").text
        for url in ("/api/jobs/current/status", f"/api/jobs/{job_id}/status"):
            text = client.get(url).text
            assert not _takes_focus(_status_open_tag(text))
            assert "autofocus" not in _only_scan_button(text).attributes

    @pytest.mark.parametrize("action", _FOCUS_ACTIONS)
    def test_first_poll_after_an_action_is_still_204(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, action: str
    ) -> None:
        """
        An action's focus attribute does not make the following poll swap.

        The token is taken from the rendering a poll would produce, with no
        action-only attribute, so the poll after an action that changed
        nothing is answered with no content and focus is left where the action
        put it.  After an Abort that poll carries ``focus=scan``, and is still
        answered 204 while the Scan button cannot take focus yet.
        """
        match action:
            case "scan":
                _accept_every_submit(client, monkeypatch)
                response = client.post(
                    "/api/scan",
                    data={"profile": "default", "title": "Focus Test"},
                    headers=_HX_REQUEST,
                )
                assert response.status_code == 200, response.text
                text = response.text
            case "multi-page answer":
                job_id = _job_in_state(client, pass_wait_state(PassWait.NEXT_PASS))
                text = _post_answer(client, job_id, PassAnswer.NEXT)
            case _:
                job_id = _job_in_state(client, JobState.AWAITING_FLIP)
                _arm_flip(client, monkeypatch, job_id)
                text = _flip_answer_response(client, job_id, action)

        poll = client.get(_status_poll_url(text), headers=_HX_REQUEST)

        assert _takes_focus(_status_open_tag(text))
        assert poll.status_code == 204, poll.text


_BUTTON_TAG = re.compile(r"<button\b[^>]*>")


def _focused_buttons(markup: str) -> list[str]:
    """Return the id of every button in ``markup`` that carries ``autofocus``."""
    focused = []
    for tag in _BUTTON_TAG.findall(markup):
        if _takes_focus(tag):
            found = re.search(r'\bid="([^"]+)"', tag)
            assert found is not None, tag
            focused.append(found.group(1))
    return focused


class TestPromptAutofocus:
    """
    A prompt that appears through the poll puts focus on its primary button.

    Only the owner's rendering carries it, since only the owner is sent the
    prompt at all.  It reaches the browser only on the render where the prompt
    first appears, because every later poll is answered 204 while nothing
    changes.  The full page never carries it: a page load moves no focus.
    """

    def test_owner_flip_prompt_autofocuses_continue(self, client: TestClient) -> None:
        """Continue takes focus; Abort, which asks first, does not."""
        _job_in_state(client, JobState.AWAITING_FLIP)

        text = client.get("/api/jobs/current/status").text

        assert 'id="flip-abort"' in text
        assert _focused_buttons(text) == ["flip-continue"]
        assert "autofocus" not in _only_scan_button(text).attributes
        assert not _takes_focus(_status_open_tag(text))

    @pytest.mark.parametrize("presented", [_OWNING_BROWSER + "-not", None])
    def test_non_owner_prompt_is_never_autofocused(
        self, client: TestClient, presented: str | None
    ) -> None:
        """Another browser is sent no prompt, and so no focus request."""
        _flip_job(client, _OWNING_BROWSER)

        text = _as_browser(client, presented)

        assert _NON_OWNER_LINE in text
        assert "autofocus" not in text

    def test_page_render_never_autofocuses_the_prompt(self, client: TestClient) -> None:
        """The page shows the owner's open prompt and moves no focus to it."""
        _job_in_state(client, JobState.AWAITING_FLIP)

        page = client.get("/").text

        assert 'id="flip-continue"' in page
        assert "autofocus" not in page

    def test_first_poll_after_a_page_with_a_prompt_is_204(
        self, client: TestClient
    ) -> None:
        """
        The page's token is the poll's, though only the poll carries autofocus.

        A 200 here would re-render the prompt with ``autofocus`` a second after
        the page loaded, and move focus the settled default says a page load
        never moves.
        """
        _job_in_state(client, JobState.AWAITING_FLIP)

        page = client.get("/").text
        poll = client.get(_status_poll_url(page), headers=_HX_REQUEST)

        assert poll.status_code == 204, poll.text


# --- A fresh page reports a finished job as the past ---------------------------

# One finished job per kind of outcome the "Last scan" line can name: the
# state, the warning and the category it records, the start of the line it
# must render, and the detail line under it (None for none).  The expected
# lines are written out, so a change to the copy fails here as well as in
# tests/test_vocabulary.py.
_WARNED = "A sheet could not be read; rescan it."
_LAST_SCAN_CASES: dict[
    str, tuple[JobState, str | None, ErrorCategory | None, str, str | None]
] = {
    "done": (JobState.DONE, None, None, "Last scan: ✓ Done: Render Test", None),
    "done-warned": (
        JobState.DONE,
        _WARNED,
        None,
        "Last scan: ⚠ Uploaded with a warning: Render Test",
        _WARNED,
    ),
    "fallback": (
        JobState.FALLBACK,
        None,
        None,
        "Last scan: → Saved to folder: Render Test",
        None,
    ),
    "cancelled": (
        JobState.CANCELLED,
        None,
        None,
        "Last scan: ⊘ Cancelled: Render Test",
        None,
    ),
    "error-red": (
        JobState.ERROR,
        None,
        ErrorCategory.SCANNER,
        "Last scan: ✗ Failed: Render Test",
        f"{error_message(ErrorCategory.SCANNER)} "
        f"{error_next_step(ErrorCategory.SCANNER)}",
    ),
    "error-amber": (
        JobState.ERROR,
        None,
        ErrorCategory.UNCONFIRMED_SEND,
        f"Last scan: ⚠ {UNCONFIRMED_SEND_LABEL}: Render Test",
        f"{error_message(ErrorCategory.UNCONFIRMED_SEND)} "
        f"{error_next_step(ErrorCategory.UNCONFIRMED_SEND)}",
    ),
    "error-no-category": (
        JobState.ERROR,
        None,
        None,
        "Last scan: ✗ Failed: Render Test",
        "disk on fire",
    ),
}

# The idle line on an appliance that can scan.
_IDLE_LINE = "<p>Ready to scan.</p>"

# Every class the live outcome branches colour a line with.  None may reach a
# past outcome: it is muted whatever it was.
_LIVE_OUTCOME_CLASSES = (
    "status-done",
    "status-error",
    "status-fallback",
    "status-cancelled",
)

_TITLE_TAG = re.compile(r"<title>(?P<text>.*?)</title>", re.DOTALL)


def _last_job(
    client: TestClient,
    case: str,
    *,
    owner: str | None = _RENDERING_BROWSER,
    title: str = "Render Test",
) -> str:
    """
    Finish a job as ``_LAST_SCAN_CASES[case]`` describes, with a preview and counts.

    The preview and the counts are recorded so that their absence from the
    "Last scan" rendering is a finding, not an accident of the staging.

    Args:
        client: The client whose app owns the store and worker.
        case: A key of ``_LAST_SCAN_CASES``.
        owner: The owner token to record, or None for an unowned row.
        title: The job's title.

    Returns:
        The job's id.

    """
    state, warning, category, _, _ = _LAST_SCAN_CASES[case]
    job_store: JobStore = _app(client).state.job_store
    job = job_store.create_job(profile="default", title=title, owner_token=owner)
    job_store.update_thumbnail(job.id, _THUMBNAIL)
    job_store.finish_job(
        job.id,
        state,
        result=JobResult(
            outcome=None,
            warning=warning,
            pages_scanned=4,
            pages_removed=2,
            pages_uploaded=2,
            removed_positions=(2, 4),
        ),
        error="disk on fire",
        error_category=category,
    )
    _adopt_as_current_job(client, job.id)
    return job.id


def _started(client: TestClient, job_id: str) -> str:
    """Return how the "Last scan" line spells the job's creation time."""
    job_store: JobStore = _app(client).state.job_store
    job = job_store.get_job(job_id)
    assert job is not None
    return local_time(job.created_at)


def _page_as(client: TestClient, token: str | None) -> str:
    """
    Fetch the full page as a browser carrying ``token``, as ``_as_browser`` does.

    Returns:
        The rendered page.

    """
    client.cookies.clear()
    headers = {} if token is None else {"Cookie": f"{_OWNER_COOKIE}={token}"}
    response = client.get("/", headers=headers)
    assert response.status_code == 200
    return response.text


class TestLastScanLine:
    """A page loaded with no job active reports the last one as the past."""

    @pytest.mark.parametrize("case", list(_LAST_SCAN_CASES))
    def test_last_scan_line_on_a_fresh_page(
        self, client: TestClient, case: str
    ) -> None:
        """
        The idle line comes first, then the muted outcome, title and start time.

        A warned or failed job adds its detail line in the same style, and a
        failure with a category keeps its disclosure.
        """
        state, _, category, line, detail = _LAST_SCAN_CASES[case]
        job_id = _last_job(client, case)
        area = _element(client.get("/").text, "status-area")

        expected = f"{line} — started {_started(client, job_id)}"
        assert f'<p class="last-scan">{escape(expected)}</p>' in area
        assert area.index(_IDLE_LINE) < area.index('class="last-scan"')
        if detail is None:
            assert area.count('class="last-scan"') == 1
        else:
            assert f'<p class="last-scan">{escape(detail)}</p>' in area
            assert area.count('class="last-scan"') == 2
        details = _TECH_DETAILS.search(area)
        assert (details is not None) is (
            state is JobState.ERROR and category is not None
        )
        if details is not None:
            assert "open" not in details.group("attrs")
            assert "disk on fire" in details.group("body")
            assert f"Category: {category}" in details.group("body")
            assert f"Job: {job_id}" in details.group("body")

    @pytest.mark.parametrize("case", list(_LAST_SCAN_CASES))
    def test_last_scan_has_no_alert_or_thumbnail(
        self, client: TestClient, case: str
    ) -> None:
        """
        No alert, preview, counts, colour, reload or poll: it is not news.

        The page's one assertive region stays the empty ``#status-message``.
        """
        _last_job(client, case)
        page = client.get("/").text
        area = _element(page, "status-area")

        assert page.count('role="alert"') == 1
        assert 'role="alert"' not in area
        assert "<img" not in area
        assert "page-counts" not in area
        assert "hx-get" not in area
        assert "hx-trigger" not in area
        for name in _LIVE_OUTCOME_CLASSES:
            assert name not in area
        # The page holds Scan only for its lists; once they land, a finished
        # job leaves it offered again.
        button = _only_scan_button(load_the_lists(client, page))
        assert "disabled" not in button.attributes
        assert button.text.strip() == "Scan"

    @pytest.mark.parametrize(
        ("recorded", "presented", "sees_detail"),
        [
            (_RENDERING_BROWSER, _RENDERING_BROWSER, True),
            (_RENDERING_BROWSER, _RENDERING_BROWSER + "-not", False),
            (_RENDERING_BROWSER, None, False),
            (None, _RENDERING_BROWSER, False),
        ],
        ids=["owner", "other-browser", "no-token", "unowned-row"],
    )
    def test_last_scan_title_is_owner_gated(
        self,
        client: TestClient,
        recorded: str | None,
        presented: str | None,
        *,
        sees_detail: bool,
    ) -> None:
        """The real title and warning reach only the browser that started it."""
        job_id = _last_job(
            client, "done-warned", owner=recorded, title="Owner Only Marker"
        )
        area = _element(_page_as(client, presented), "status-area")

        shown = "Owner Only Marker" if sees_detail else HIDDEN_JOB_TITLE
        line = (
            f"Last scan: ⚠ Uploaded with a warning: {shown}"
            f" — started {_started(client, job_id)}"
        )
        warning = _WARNED if sees_detail else HIDDEN_WARNING_LINE
        assert f'<p class="last-scan">{escape(line)}</p>' in area
        assert f'<p class="last-scan">{escape(warning)}</p>' in area
        assert ("Owner Only Marker" in area) is sees_detail
        assert (_WARNED in area) is sees_detail

    def test_no_job_ever_shows_the_idle_line_alone(self, client: TestClient) -> None:
        """A fresh install has nothing to report but that it is ready."""
        area = _element(client.get("/").text, "status-area")

        assert _IDLE_LINE in area
        assert area.count("<p") == 1
        assert "last-scan" not in area

    @pytest.mark.parametrize("route", ["current", "followed"])
    def test_live_terminal_poll_keeps_the_alert(
        self, client: TestClient, route: str
    ) -> None:
        """A poll that watches the job fail still announces it, in red."""
        job_id = _last_job(client, "error-red")
        path = (
            "/api/jobs/current/status"
            if route == "current"
            else f"/api/jobs/{job_id}/status"
        )
        text = client.get(path).text

        assert text.count('role="alert"') == 1
        assert _ALERT_DIV.search(text) is not None
        assert "last-scan" not in text
        assert "Ready to scan." not in text
        assert _HISTORY_RELOAD in text


@pytest.mark.parametrize(
    ("fixture", "reason"),
    [
        ("blocked_client", SCAN_BLOCKED_REASON),
        ("url_unset_client", SCAN_BLOCKED_URL_REASON),
    ],
    ids=["token-unset", "url-unset"],
)
@pytest.mark.parametrize("path", ["/", "/api/jobs/current/status"])
def test_blocked_idle_line_is_the_blocked_reason(
    request: pytest.FixtureRequest, fixture: str, reason: str, path: str
) -> None:
    """
    On an appliance that cannot upload, the idle line says why, not "Ready".

    It is plain text: the red reason is under the button already, and a red
    line in the status area with no alert role would read as a live failure.
    """
    blocked: TestClient = request.getfixturevalue(fixture)
    area = _element(blocked.get(path).text, "status-area")

    assert f"<p>{escape(reason)}</p>" in area
    assert "Ready to scan." not in area
    assert "status-error" not in area


def _queued_behind_a_running_job(client: TestClient) -> str:
    """
    Stage a PENDING job this browser owns behind a SCANNING one.

    Returns:
        The queued job's id.

    """
    _job_in_state(client, JobState.SCANNING)
    job_store: JobStore = _app(client).state.job_store
    return job_store.create_job(
        profile="default", title="Queued Render", owner_token=_RENDERING_BROWSER
    ).id


@pytest.mark.parametrize("queued", [True, False], ids=["queued", "starting"])
def test_queued_button_reads_queued(client: TestClient, *, queued: bool) -> None:
    """
    A job still waiting to start makes the button say Queued, not Scanning.

    Both a job behind another and one about to start alone are PENDING, and
    neither is being scanned yet.
    """
    if queued:
        job_id = _queued_behind_a_running_job(client)
    else:
        job_id = _job_in_state(client, JobState.PENDING)
    for text in (client.get("/").text, client.get(f"/api/jobs/{job_id}/status").text):
        button = _only_scan_button(text)
        assert button.text.strip() == "Queued…"
        assert "disabled" in button.attributes
        assert "Scanning…" not in text


def _titles(markup: str) -> list[str]:
    """Return the text of every ``<title>`` in the markup."""
    return [match.group("text") for match in _TITLE_TAG.finditer(markup)]


# The tab title each state's status response carries, written out.  An ERROR
# row carries the module's default category, which is red.
_STATE_TITLES = {
    JobState.PENDING: "Starting — saneless",
    JobState.SCANNING: "Scanning — saneless",
    JobState.AWAITING_FLIP: "Waiting for flip — saneless",
    JobState.AWAITING_NEXT_PASS: "Waiting for more pages — saneless",
    JobState.AWAITING_BLANK_DECISION: "Waiting: blank pages found — saneless",
    JobState.AWAITING_RETRY: "Waiting: last scan failed — saneless",
    JobState.SCANNING_REVERSE: "Scanning backs — saneless",
    JobState.ASSEMBLING: "Assembling — saneless",
    JobState.UPLOADING: "Uploading — saneless",
    JobState.DONE: "Done — saneless",
    JobState.ERROR: "Failed — saneless",
    JobState.FALLBACK: "Saved to folder — saneless",
    JobState.CANCELLED: "Cancelled — saneless",
}


class TestPageTitle:
    """The tab's title follows the status area's state, never the job's title."""

    def test_every_state_has_a_title(self) -> None:
        """The table covers every state, so a new one forces a decision here."""
        assert set(_STATE_TITLES) == set(JobState)

    @pytest.mark.parametrize("state", list(JobState))
    def test_status_response_title_by_state(
        self, client: TestClient, state: JobState
    ) -> None:
        """
        Each poll carries one title, at the top level, ahead of the area.

        htmx reads a title only at the root of a response, so one inside the
        status area would never reach the tab.
        """
        _job_in_state(client, state)
        text = client.get("/api/jobs/current/status").text

        expected = page_title(state, category=_DEFAULT_ERROR_CATEGORY, queued=False)
        assert expected == _STATE_TITLES[state]
        assert _titles(text) == [escape(expected)]
        assert text.index("<title>") < text.index('<div id="status-area"')

    def test_queued_and_starting_titles_differ(self, client: TestClient) -> None:
        """A job behind another is Queued; one about to start alone, Starting."""
        job_id = _queued_behind_a_running_job(client)
        assert _titles(client.get(f"/api/jobs/{job_id}/status").text) == [
            "Queued — saneless"
        ]
        starting = _job_in_state(client, JobState.PENDING)
        assert _titles(client.get(f"/api/jobs/{starting}/status").text) == [
            "Starting — saneless"
        ]

    def test_a_warned_upload_is_not_titled_done(self, client: TestClient) -> None:
        """The tab names a warned upload as the status area heads it."""
        _job_in_state(client, JobState.DONE, warning=_WARNED)
        assert _titles(client.get("/api/jobs/current/status").text) == [
            "Uploaded with a warning — saneless"
        ]

    @pytest.mark.parametrize("route", _STATUS_ROUTES)
    def test_every_status_response_carries_one_title(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch, route: str
    ) -> None:
        """
        Every status response names the state; the lost-contact one names none.

        The fallback knows nothing of the job, so it leaves the tab's title as
        it was rather than guess.
        """
        text = _status_response(client, monkeypatch, route)

        titles = _titles(text)
        if route == "lost-contact fallback":
            assert titles == []
        else:
            assert len(titles) == 1
            assert titles[0].endswith(" — saneless")
            assert text.index("<title>") < text.index('<div id="status-area"')

    @pytest.mark.parametrize(
        ("stage", "expected"),
        [
            ("none", "saneless"),
            ("finished", "saneless"),
            ("active", "Scanning — saneless"),
        ],
    )
    def test_page_title_on_the_full_page(
        self, client: TestClient, stage: str, expected: str
    ) -> None:
        """
        The page's title is the rule the polls follow.

        A finished job is the past, so the page is titled as idle.
        """
        if stage == "finished":
            _last_job(client, "error-red")
        elif stage == "active":
            _job_in_state(client, JobState.SCANNING)

        assert _titles(client.get("/").text) == [expected]

    @pytest.mark.parametrize("state", list(JobState))
    def test_title_never_contains_the_job_title(
        self, client: TestClient, state: JobState
    ) -> None:
        """
        The owner's own title never reaches the tab, the switcher or history.

        The page names the sentinel in its history row, which is what makes
        its absence from the page's title mean something; a poll renders it
        wherever its state shows the title.
        """
        sentinel = "Tab Title Sentinel"
        job_store: JobStore = _app(client).state.job_store
        job = job_store.create_job(
            profile="default", title=sentinel, owner_token=_RENDERING_BROWSER
        )
        job_store.update_state(job.id, state)
        _adopt_as_current_job(client, job.id)

        page = client.get("/").text
        assert sentinel in page
        for text in (
            page,
            client.get("/api/jobs/current/status").text,
            client.get(f"/api/jobs/{job.id}/status").text,
        ):
            titles = _titles(text)
            assert titles
            assert all(sentinel not in title for title in titles)
