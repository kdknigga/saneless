"""Shared test fixtures and helpers for all test modules."""

from __future__ import annotations

import functools
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import httpx
import pytest
from PIL import Image, ImageDraw

from saneless import paperless as paperless_module
from saneless.config import (
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
)
from saneless.paperless import PaperlessClient, UploadResult
from saneless.pipeline import FlipCoordinator
from saneless.scanner import sane_backend as sane_backend_mod
from saneless.scanner.base import DeviceCapabilities, ScanBatch, ScannerBackend
from saneless.vocabulary import FlipOutcome

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from saneless.job import Job, JobStore
    from saneless.scanner.base import DeviceInfo, PageRecord, PageSink, ScanSettings
    from saneless.vocabulary import JobState

_POLL_INTERVAL = 0.02

# Where Playwright keeps its browsers, resolved once from the real environment
# when this module is imported -- before any test redirects HOME. The browser
# driver starts inside a test, where HOME and XDG_CACHE_HOME already point at an
# empty fake home, and it would look for Chromium there.
_BROWSERS = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or str(
    Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ms-playwright"
)

_XDG_BASES = (
    ("XDG_CONFIG_HOME", (".config",)),
    ("XDG_STATE_HOME", (".local", "state")),
    ("XDG_DATA_HOME", (".local", "share")),
    ("XDG_CACHE_HOME", (".cache",)),
)


@pytest.fixture
def tmp_config_dir(tmp_path: Path) -> Path:
    """Create a temporary directory for config files."""
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    return config_dir


@pytest.fixture
def sample_toml(tmp_config_dir: Path) -> Path:
    """Write a minimal valid TOML config to tmp_config_dir/saneless.toml."""
    toml_content = """\
[scanner]
host = "192.168.1.50"

[paperless]
url = "http://paperless:8000"
token = "abc123"

[output]
tmp_dir = "/tmp/saneless"
log_level = "INFO"

[profiles.default]
source = "Flatbed"
resolution = 300
mode = "color"
"""
    config_file = tmp_config_dir / "saneless.toml"
    config_file.write_text(toml_content)
    return config_file


@pytest.fixture
def sample_pil_image() -> Image.Image:
    """Return a 100x100 white RGB PIL Image."""
    return Image.new("RGB", (100, 100), "white")


@pytest.fixture
def sample_pil_images() -> list[Image.Image]:
    """Return a list of 3 sample PIL Images of varying sizes and colors."""
    return [
        Image.new("RGB", (100, 100), "white"),
        Image.new("RGB", (200, 200), "red"),
        Image.new("RGB", (150, 150), "blue"),
    ]


def _config_file_stamp(path: Path) -> tuple[int, int] | None:
    """
    Stamp a config file by modification time and size, or None when absent.

    Only ``stat`` is read, never the contents: in a developer's checkout this
    file is their real, gitignored configuration.

    Returns:
        ``(st_mtime_ns, st_size)``, or ``None`` when the file does not exist.

    """
    try:
        status = path.stat()
    except FileNotFoundError:
        return None
    return status.st_mtime_ns, status.st_size


@pytest.fixture(autouse=True, scope="session")
def _suite_leaves_cwd_config_alone() -> Iterator[None]:
    """
    Fail the run if the suite creates or changes ``./saneless.toml`` (T-26-32).

    The retired lazy auto-profile generation wrote there from inside a job, and
    the file is gitignored, so ``git status`` after a run cannot show a stray
    copy.  The path is fixed when the session starts, before any test changes
    the working directory.

    Yields:
        Nothing; the check runs after the last test.

    """
    path = Path.cwd() / "saneless.toml"
    before = _config_file_stamp(path)
    yield
    if _config_file_stamp(path) != before:
        pytest.fail(f"the test suite created or modified {path}")


def reset_sane_process_state() -> None:
    """
    Return SANE to "never initialised, not wedged" for the next test.

    ``_INIT`` and ``_WEDGE`` are process-global by design (D-17, D-13): the
    first ``SaneBackend`` built in a process initialises SANE and every later
    one deliberately does not, and a read that never came back refuses the
    next scan on a *different* backend object.  Both are exactly the kind of
    state a test cannot be trusted to leave behind, so the suite resets them
    around every test rather than asking each module to remember.

    The reset goes through the public ``shutdown()`` and not into the guard's
    own fields, because "after a shutdown a later init is allowed" is the
    behaviour D-17 promises; reaching past it would let that promise rot while
    the tests kept passing.

    ``shutdown()`` has one documented refusal: it leaves the guard armed when a
    read is still recorded as outstanding, because ``sane_exit()`` closes every
    open handle and SANE forbids that while an operation is in flight.  A test
    that wedged the backend and did not release it would therefore strand
    ``_INIT.done`` at ``True`` -- the very leak this helper exists to stop --
    so that one case is finished off by hand, and pointedly *without* calling
    ``sane_exit()``, which would be unsafe for the same reason ``shutdown()``
    declined to.
    """
    sane_backend_mod.shutdown()
    if not sane_backend_mod._INIT.done:
        return
    record = sane_backend_mod._WEDGE
    record.stuck = False
    record.done = None
    record.device = None
    record.iterator = None
    record.device_id = ""
    record.page_label = ""
    sane_backend_mod._INIT.done = False
    sane_backend_mod._INIT.host = ""
    sane_backend_mod._INIT.version = None


@pytest.fixture(autouse=True)
def sane_process_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    Give every test in the suite an uninitialised, unwedged SANE (D-17, D-13).

    This is suite-wide and not module-wide on purpose.  The guard is process
    state, so a single module that builds a real ``SaneBackend`` over a fake
    ``sane`` and does not reset it suppresses ``sane.init()`` for every later
    test in the same process -- including the ones that then make real SANE
    calls against a library that was never initialised.  A module-local fixture
    fixes only the module that remembers to add one; this cannot be forgotten
    by construction.

    ``monkeypatch`` is requested, and not used, purely for its ordering.  It is
    the fixture every module patches ``sane_backend.sane`` through, and a
    fixture that requests it is torn down before its ``undo`` runs -- so the
    final reset still finds the fake in place rather than the real library that
    the undo restores.

    Args:
        monkeypatch: Requested for teardown ordering only.

    Yields:
        Nothing; the reset runs on both sides of the test.

    """
    _ = monkeypatch  # ordering only: tear down before the sane-module undo
    reset_sane_process_state()
    yield
    reset_sane_process_state()


@pytest.fixture(autouse=True)
def hermetic_env(
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Run every test in a fake home and its own working directory.

    Without this a test reaches the developer's real files: ``Path.home()``
    and the XDG variables decide config discovery and the state defaults, so a
    ``Settings()`` would read ``~/.config/saneless/config.toml`` -- live URL and
    token included -- and a ``JobStore`` would write under ``~/.local/state``.
    The working directory matters the same way, because ``./saneless.toml`` is
    the first config search path and a developer's checkout usually has one.

    HOME and the XDG variables are set, not ``Path.home()`` patched, so
    ``os.path.expanduser``, every library and any subprocess see the fake home
    too. The fake home comes from ``tmp_path_factory`` rather than from inside
    ``tmp_path``, because some tests assert exactly what ``tmp_path`` holds.

    A test about the unset-XDG fallback deletes the variable itself.

    Args:
        tmp_path: The test's own directory, which becomes the working directory.
        tmp_path_factory: Source of a fresh fake home directory.
        monkeypatch: Undoes every change after the test.

    """
    for key in list(os.environ):
        if key.startswith("SANELESS_"):
            monkeypatch.delenv(key, raising=False)
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    for variable, parts in _XDG_BASES:
        monkeypatch.setenv(variable, str(home.joinpath(*parts)))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", _BROWSERS)
    monkeypatch.chdir(tmp_path)


def build_settings(tmp_path: Path, **overrides: object) -> Settings:
    """
    Build Settings with test-safe defaults, every directory under ``tmp_path``.

    Scratch space and durable state sit on separate subtrees, as they do in a
    real deployment. Temp-cleanup assertions walk ``tmp_dir`` demanding that
    nothing survives a run, and ``failed/`` under ``data_dir`` is meant to
    survive, so the two must not share a root.

    A keyword replaces its whole section, so a test overriding ``output`` must
    supply its own paths; most tests instead mutate one field of the result.

    Import it as ``from tests.conftest import build_settings`` where a
    module-level helper needs it, or request the ``make_settings`` fixture.

    Args:
        tmp_path: The test's own temporary directory.
        **overrides: Whole sections (``scanner``, ``paperless``, ``output``,
            ``profiles``, ...) to use instead of the defaults.

    Returns:
        A fresh Settings instance.

    """
    auth = "test-token"
    sections: dict[str, Any] = {
        "scanner": ScannerConfig(device="test:device:001"),
        "paperless": PaperlessConfig(url="http://localhost:8000", token=auth),
        "output": OutputConfig(
            tmp_dir=tmp_path / "tmp",
            data_dir=tmp_path / "data",
            log_file=tmp_path / "data" / "saneless.log",
        ),
        "profiles": {"default": ProfileConfig()},
    }
    sections.update(overrides)
    return Settings(**sections)


@pytest.fixture
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    """
    Hand a test ``build_settings`` already bound to its own ``tmp_path``.

    Returns:
        A callable taking the same section overrides as ``build_settings``.

    """
    return functools.partial(build_settings, tmp_path)


@pytest.fixture
def default_settings(tmp_path: Path) -> Settings:
    """Return a Settings instance with test-safe defaults under ``tmp_path``."""
    return build_settings(tmp_path)


def _refuse_every_request(request: httpx.Request) -> httpx.Response:
    """
    Fail a Paperless request the way an unreachable server does.

    Args:
        request: The request the client tried to send.

    Raises:
        httpx.ConnectError: Always, as a refused connection would.

    """
    msg = "paperless unreachable in tests"
    raise httpx.ConnectError(msg, request=request)


@pytest.fixture
def offline_paperless(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """
    Give the web app a Paperless client that never leaves the process.

    The web settings name ``http://localhost:8000``, so a test that submits a
    scan used to have its worker really connect there: refused on most
    machines, then backed off for a real one and two seconds -- and on a
    developer machine running Paperless, delivered with the test token.

    ``create_app`` still builds a real ``PaperlessClient``; only its transport
    is replaced, by one that fails every request with the same ``ConnectError``
    a refused connection raises. The upload backoff is recorded instead of
    slept. Tests that replace ``app.state.paperless`` methods are unaffected.

    Args:
        monkeypatch: Undoes both patches after the test.

    Returns:
        The backoff delays the client asked for, in order.

    """
    delays: list[float] = []

    def build_client(
        *, url: str, token: str, consume_dir: Path | None = None
    ) -> PaperlessClient:
        """
        Build the app's Paperless client over the refusing transport.

        Returns:
            A real client whose every request fails without a socket.

        """
        return PaperlessClient(
            url=url,
            token=token,
            consume_dir=consume_dir,
            transport=httpx.MockTransport(_refuse_every_request),
        )

    monkeypatch.setattr("saneless.web.app.PaperlessClient", build_client)
    # ``saneless.paperless`` reaches its backoff through the ``time`` module, so
    # this swaps that module's ``sleep`` for the length of the test.
    monkeypatch.setattr(paperless_module.time, "sleep", delays.append)
    return delays


def _inked_page() -> Image.Image:
    """
    Return a 100x100 page with a black square on it.

    Inked rather than blank, because a blank page is what
    ``pipeline._drop_empty_pages`` exists to remove: a stub handing back a
    white rectangle makes every empty-page-detection profile delete the whole
    scan, and the test then fails somewhere that has nothing to do with it.

    Returns:
        A fresh image; callers may spool or mutate it.

    """
    image = Image.new("RGB", (100, 100), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([10, 10, 90, 90], fill="black")
    return image


def scan_batch(
    pages: Sequence[PageRecord],
    *,
    resolution: int = 300,
    rejected: int = 0,
) -> ScanBatch:
    """
    Build a ScanBatch for a stubbed scanner.

    ``scan_pages`` returns a record rather than yielding, so a stub handing back
    ``iter([...])`` no longer models the backend at all -- and because a
    ``MagicMock`` will return whatever it is given, that mismatch surfaces as a
    confusing failure deep in the pipeline rather than at the stub.

    ``pages`` is the ordered ``PageRecord`` tuple a sink produced, not a list of
    images (D-01). A test that only needs "a scan happened" should not hand-build
    records for this: ``spooling`` runs real pages through the caller's own sink
    and the records come back from there, so the spooled files behind them
    actually exist.

    The two extra facts default to "nothing surprising happened": the device
    honoured the resolution it was asked for and rejected no sheets. A test that
    cares about either passes it explicitly.

    Args:
        pages: The records the stubbed scan produced, in document order.
        resolution: The resolution the device reports having actually used.
        rejected: How many fed sheets failed their integrity checks.

    Returns:
        A ScanBatch carrying those records and both facts.

    """
    return ScanBatch(
        pages=tuple(pages), actual_resolution=resolution, pages_rejected=rejected
    )


def spooling(
    pages: Sequence[Image.Image],
    *,
    resolution: int = 300,
    rejected: int = 0,
) -> Callable[[str, ScanSettings, PageSink], ScanBatch]:
    """
    Build the ``side_effect`` a stubbed ``scan_pages`` needs (D-01).

    A factory returning a callable, rather than a ready-made batch for
    ``return_value``, because the sink does not exist until the call happens:
    it is the pipeline's, created inside the per-job workspace, and the records
    in the batch are whatever *that* sink gives back. A batch built in advance
    could only carry records the pipeline never made, pointing at files that
    were never written.

    The same argument ``scan_batch`` records still applies to the shape of the
    stub itself: a ``MagicMock`` returns whatever it is given, so a stub that
    does not model the backend surfaces as a confusing failure deep in the
    pipeline rather than at the stub. Modelling the backend now means taking
    the sink and filling it.

    Args:
        pages: The pages the stubbed scan hands to the sink, in order.
        resolution: The resolution the device reports having actually used.
        rejected: How many fed sheets failed their integrity checks.

    Returns:
        One callable with ``scan_pages``' own ``(device_id, settings, sink)``
        shape, ready to assign to ``MagicMock.side_effect``.

    """

    def _spool_pages(
        device_id: str, settings: ScanSettings, sink: PageSink
    ) -> ScanBatch:
        """Spool every page into the sink the caller supplied."""
        records = [sink.add(page) for page in pages]
        return scan_batch(records, resolution=resolution, rejected=rejected)

    return _spool_pages


def spooling_in_turn(
    *page_lists: Sequence[Image.Image],
    resolution: int = 300,
) -> Callable[[str, ScanSettings, PageSink], ScanBatch]:
    """
    Build one ``side_effect`` that spools a different list per call.

    For manual duplex, where pass A and pass B are two ``scan_pages`` calls on
    the same stub and have to produce different pages.

    ONE callable that counts its own calls, never a list of callables:
    ``unittest.mock`` calls a ``side_effect`` only when the ``side_effect``
    itself is callable, and a *list* is consumed as an iterable of results, so
    each element is handed back as-is. A list of functions would therefore make
    ``scan_pages`` return a function object, and the failure lands wherever the
    pipeline first treats it as a batch (29-RESEARCH.md Pitfall 5). The
    in-repo precedent is ``tests/test_worker.py``'s ``_PassBGatedScanner``,
    which tracks ``scan_calls`` on itself for the same reason.

    Args:
        page_lists: One list of pages per expected call, in call order.
        resolution: The resolution the device reports having actually used.

    Returns:
        One callable with ``scan_pages``' own ``(device_id, settings, sink)``
        shape, ready to assign to ``MagicMock.side_effect``.

    """
    calls = 0

    def _spool_next(
        device_id: str, settings: ScanSettings, sink: PageSink
    ) -> ScanBatch:
        """Spool the list belonging to this call number."""
        nonlocal calls
        index = calls
        calls += 1
        if index >= len(page_lists):
            msg = (
                f"scan_pages was called {calls} time(s), but spooling_in_turn "
                f"was given only {len(page_lists)} page list(s)"
            )
            raise AssertionError(msg)
        records = [sink.add(page) for page in page_lists[index]]
        return scan_batch(records, resolution=resolution)

    return _spool_next


def images_of(batch: ScanBatch) -> list[Image.Image]:
    """
    Read a batch's spooled pages back, in record order.

    The pages a scan produced are files now, so an assertion about what was
    scanned has to open them. This is the one place that happens, so an
    existing image assertion survives the record switch by being handed
    ``images_of(batch)`` instead of ``batch.pages`` -- a one-line change per
    site rather than a rewrite.

    Order comes from the record list and nothing else: the directory is never
    sorted or globbed, which is the invariant HARD-01's own test attacks (D-02).

    Args:
        batch: The batch whose spooled pages to read.

    Returns:
        The pages, fully loaded so no file handle is left open, in the order
        the records are in.

    """
    images: list[Image.Image] = []
    for record in batch.pages:
        image = Image.open(record.path)
        # Forces the read now and lets Pillow close the file it opened; a
        # lazy ImageFile would trip the suite's ResourceWarning-as-error.
        image.load()
        images.append(image)
    return images


class StubScannerBackend(ScannerBackend):
    """
    A concrete one-page scanner backend for tests that are not about scanning.

    The seven near-identical stub classes across the web, cross-origin and
    lifespan tests differ only in ``scan_pages``; ``get_devices`` and
    ``get_capabilities`` are the same everywhere. They live here so a subclass
    overrides the one method it cares about.

    Subclasses the ABC rather than duck-typing it, for the reason Phase 24's
    WR-08 measured and ``tests/test_cli.py``'s ``MockSaneBackend`` records:
    every stub that subclassed was caught by the type checkers when its
    contract changed, and the ones that did not were missed. This phase is
    exactly such a contract change, which is what makes the point again.

    Import it as ``from tests.conftest import StubScannerBackend``; the bare
    ``conftest`` form raises ``ModuleNotFoundError`` under pytest 9's importlib
    mode.
    """

    def get_devices(self) -> list[DeviceInfo]:
        """
        Report no devices.

        Returns:
            An empty list.

        """
        return []

    def get_capabilities(self, device_id: str) -> DeviceCapabilities:
        """
        Report a plain flatbed at one resolution.

        Args:
            device_id: Ignored; every device answers the same here.

        Returns:
            Capabilities naming one source, one resolution and one mode.

        """
        return DeviceCapabilities(
            sources=["Flatbed"], resolutions=[300], modes=["color"]
        )

    def scan_pages(
        self, device_id: str, settings: ScanSettings, sink: PageSink
    ) -> ScanBatch:
        """
        Spool exactly one inked page.

        Args:
            device_id: Ignored; this stub scans nothing real.
            settings: Only ``resolution`` is used, and only to report it back.
            sink: The caller's sink, which receives the one page.

        Returns:
            A batch of the single record the sink returned.

        """
        record = sink.add(_inked_page())
        return scan_batch([record], resolution=settings.resolution)


class AlwaysContinueFlipCoordinator(FlipCoordinator):
    """
    A flip coordinator whose operator flips the stack the instant it is asked.

    For pipeline tests that exercise a manual-duplex run but are not about the
    flip wait itself.  A manual-duplex request has to carry a coordinator, and
    this one answers ``CONTINUED`` at once, so pass B starts straight away.

    Subclasses the ABC rather than duck-typing it, for the reason Phase 24's
    WR-08 measured and ``tests/test_cli.py``'s ``MockSaneBackend`` records:
    every stub that subclassed was caught by the type checkers when its
    contract changed, and the ones that did not were missed.

    Import it as ``from tests.conftest import AlwaysContinueFlipCoordinator``;
    the bare ``conftest`` form raises ``ModuleNotFoundError`` under pytest 9's
    importlib mode.
    """

    def wait_for_flip(self, timeout: float) -> FlipOutcome:
        """
        Report the stack flipped, without waiting.

        Args:
            timeout: Ignored; the answer is immediate.

        Returns:
            Always ``FlipOutcome.CONTINUED``.

        """
        return FlipOutcome.CONTINUED


@pytest.fixture
def mock_scanner() -> MagicMock:
    """Return a mock ScannerBackend spooling a single image with content."""
    scanner = MagicMock(spec=ScannerBackend)
    scanner.scan_pages.side_effect = spooling([_inked_page()])
    return scanner


@pytest.fixture
def multi_page_images() -> list[Image.Image]:
    """Return 5 distinct page images simulating an ADF scan."""
    colors = ["white", "red", "blue", "green", "yellow"]
    return [Image.new("RGB", (200, 300), c) for c in colors]


@pytest.fixture
def empty_page_image() -> Image.Image:
    """Return a nearly-white image that should be detected as empty."""
    return Image.new("RGB", (200, 300), (253, 253, 253))


@pytest.fixture
def content_page_image() -> Image.Image:
    """Return an image with content (not empty)."""
    img = Image.new("RGB", (200, 300), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([20, 20, 180, 280], fill="black")
    return img


@pytest.fixture
def mock_paperless() -> MagicMock:
    """Return a mock PaperlessClient that succeeds."""
    paperless = MagicMock(spec=PaperlessClient)
    paperless.upload_document.return_value = UploadResult(
        delivered_to_api=True, task_uuid="mock-task-uuid"
    )
    # poll_task's return value is not part of its contract: after OUTC-01 a
    # successful poll is "it returned" and a failed one is "it raised", so a
    # stub that hands back a status dict would encode a contract that no longer
    # exists.  None keeps this fixture honest about what success means.
    paperless.poll_task.return_value = None
    return paperless


def wait_for_state(
    store: JobStore,
    job_id: str,
    state: JobState | frozenset[JobState],
    timeout: float = 2.0,
) -> Job:
    """
    Block until a job reaches an awaited state, then return it.

    The worker runs on its own thread, so tests that drive it have to wait for
    a state rather than read one.  Passing a frozenset lets a caller wait on a
    whole class of states -- ``TERMINAL_STATES`` in particular -- without
    knowing which member the run will land on.

    The default budget is deliberately far below pytest-timeout's 60 s SIGALRM:
    a wait that hits this ceiling raises a message naming the job, the awaited
    state and the state actually observed, which a SIGALRM traceback would not.

    Args:
        store: The job store to poll.
        job_id: The id of the job to wait on.
        state: A single awaited state, or a set of acceptable states.
        timeout: Seconds to wait before giving up.

    Returns:
        The job, in one of the awaited states.

    Raises:
        RuntimeError: If the job does not reach the awaited state in time.

    """
    wanted = state if isinstance(state, frozenset) else frozenset([state])
    observed = "<no such job>"
    found: Job | None = None
    tick = threading.Event()  # never set: each wait() is a bounded pause
    for _ in range(max(1, int(timeout / _POLL_INTERVAL))):
        job = store.get_job(job_id)
        if job is not None:
            observed = job.state.value
            if job.state in wanted:
                found = job
                break
        tick.wait(_POLL_INTERVAL)
    else:
        names = ", ".join(sorted(s.value for s in wanted))
        msg = (
            f"Job {job_id} did not reach {names} within {timeout}s "
            f"(last observed: {observed})"
        )
        raise RuntimeError(msg)
    assert found is not None
    return found


@pytest.fixture(name="wait_for_state")
def _wait_for_state_fixture() -> Callable[..., Job]:
    """
    Hand the wait_for_state helper to a test module.

    pytest 9 imports test modules in ``importlib`` mode, so ``tests/`` never
    lands on ``sys.path`` and ``from conftest import wait_for_state`` raises
    ``ModuleNotFoundError``.  A fixture is the supported route for a conftest
    helper, and it is the only one that also keeps ty and pyrefly happy.

    Returns:
        The wait_for_state function itself, uncalled.

    """
    return wait_for_state


def poll_until(
    predicate: Callable[[], bool], budget: float, interval: float = _POLL_INTERVAL
) -> bool:
    """
    Poll ``predicate`` until it holds or ``budget`` seconds pass.

    Between polls the calling thread pauses for ``interval`` on an Event
    nobody sets: a bounded pause between polls, which the ``time.sleep`` ban
    does not see, so the budget -- not the pause -- is what keeps it short.
    A passing test stops polling as soon as the predicate holds. The
    predicate is checked once more after the deadline, so a condition that
    became true during the last pause is still seen.

    Args:
        predicate: The condition to wait for; called repeatedly, so it must be
            cheap and free of side effects.
        budget: Seconds to keep polling.
        interval: Seconds between polls.

    Returns:
        Whether the predicate held before the budget ran out.

    """
    deadline = time.monotonic() + budget
    tick = threading.Event()  # never set: each wait() is a bounded pause
    while time.monotonic() < deadline:
        if predicate():
            return True
        tick.wait(interval)
    return predicate()


@pytest.fixture(name="poll_until")
def _poll_until_fixture() -> Callable[..., bool]:
    """
    Hand the poll_until helper to a test module.

    A fixture for the same reason as ``wait_for_state``: test modules cannot
    import from conftest by name under pytest 9's importlib mode.

    Returns:
        The poll_until function itself, uncalled.

    """
    return poll_until


def quiet_window(seconds: float) -> None:
    """
    Block the calling thread for ``seconds``: the one sanctioned fixed pause.

    Some tests prove that something does *not* happen, and nothing can be
    polled for an event that never comes, so they give the code a short
    window to misbehave in and then look. That window is a pause in all but
    name, and it costs its full length on every passing run, so it goes
    through this one helper rather than an inline wait: every fixed pause in
    the suite can then be found by searching for its name, and a prek hook
    flags an inline ``Event().wait`` written anywhere else. Import it as
    ``from tests.conftest import quiet_window``.

    Args:
        seconds: How long to leave the code under test alone. Keep it short.

    """
    never_set = threading.Event()
    never_set.wait(seconds)
