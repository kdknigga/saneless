"""One acquisition pass over python-sane, driven through a page outlet."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from PIL import Image, ImageDraw

import saneless.scanner.scan_session as scan_session_mod
from saneless.exceptions import FeederEmptyError, ScanError
from saneless.scanner.base import PassCapReached, ScanSettings
from saneless.scanner.scan_session import (
    PassOutcome,
    PassStopped,
    restart_library,
    run_pass,
    start_library,
)
from saneless.text_safety import neutralise_controls
from saneless.vocabulary import ScanStage
from tests.fake_sane import FakeSaneDev, FakeSaneError, FakeSaneHandle, FakeSaneModule

if TYPE_CHECKING:
    from saneless.scanner.page_budget import _ScanParameters
    from saneless.scanner.scan_session import SaneDevice

_DEVICE = "test:0"
_END_OF_FEED = "Document feeder out of documents"
_SESSION_LOGGER = "saneless.scanner.scan_session"


@dataclass
class _Page:
    """One page the outlet was handed."""

    image: Image.Image
    number: int
    dpi: int


@dataclass
class RecordingOutlet:
    """
    A page outlet that records everything the pass tells it.

    Attributes:
        answers: What ``page`` answers, in order; True once they run out.
        cancel: What ``cancel_requested`` reports; a test may flip it.
        timeline: Every stage and reading call, in the order made.
        stages: The stage calls alone.
        configured_calls: The keyword arguments of each ``configured`` call.
        pages: The pages handed on.
        handles: The handles ``reading`` was given, None included.

    """

    answers: list[bool] = field(default_factory=list)
    cancel: bool = False
    timeline: list[str] = field(default_factory=list)
    stages: list[tuple[ScanStage, int | None]] = field(default_factory=list)
    configured_calls: list[tuple[int, bool]] = field(default_factory=list)
    pages: list[_Page] = field(default_factory=list)
    handles: list[object] = field(default_factory=list)

    def stage(self, stage: ScanStage, page: int | None) -> None:
        """Record a stage."""
        self.stages.append((stage, page))
        self.timeline.append(f"{stage}" if page is None else f"{stage} {page}")

    def configured(
        self, parameters: _ScanParameters, *, resolution: int, use_adf: bool
    ) -> None:
        """Record the configuration."""
        assert parameters.depth == 8
        self.configured_calls.append((resolution, use_adf))
        self.timeline.append("configured")

    def page(self, image: Image.Image, *, number: int, dpi: int) -> bool:
        """Record a page and answer from the programmed list."""
        self.pages.append(_Page(image, number, dpi))
        self.timeline.append(f"page {number}")
        return self.answers.pop(0) if self.answers else True

    def reading(self, dev: SaneDevice | None) -> None:
        """Record the handle a read is in progress on."""
        self.handles.append(dev)
        self.timeline.append("reading" if dev is not None else "reading done")

    def cancel_requested(self) -> bool:
        """Report the cancel flag."""
        return self.cancel


def _content_image(index: int) -> Image.Image:
    """
    Build a readable page that differs from the others.

    Returns:
        A 200x300 RGB page well above the byte floor.

    """
    image = Image.new("RGB", (200, 300), "white")
    ImageDraw.Draw(image).rectangle((10, 10 + index, 190, 290), fill="black")
    return image


def _unreadable_image() -> Image.Image:
    """
    Build a page below the byte floor.

    Returns:
        A 10x10 RGB page, 300 bytes of image data.

    """
    return Image.new("RGB", (10, 10), "white")


def _feeder() -> ScanSettings:
    """
    Build settings that send the pass through the named feeder.

    Returns:
        Feeder settings.

    """
    return ScanSettings(
        source="Automatic Document Feeder", resolution=300, mode="Color"
    )


def _flatbed() -> ScanSettings:
    """
    Build settings that take the flatbed path.

    Returns:
        Flatbed settings.

    """
    return ScanSettings(source="Flatbed", resolution=300, mode="Color")


def _install(
    monkeypatch: pytest.MonkeyPatch,
    dev: FakeSaneDev,
    *,
    open_error: BaseException | None = None,
    init_error: BaseException | None = None,
) -> FakeSaneModule:
    """
    Patch a fake python-sane module serving ``dev`` into scan_session.

    Returns:
        The module.

    """
    module = FakeSaneModule(device=dev, open_error=open_error, init_error=init_error)
    monkeypatch.setattr(scan_session_mod, "sane", module)
    return module


def test_a_feeder_pass_reports_stages_and_pages_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three good sheets: every stage in order, three pages, a clean outcome."""
    dev = FakeSaneDev()
    loaded = [_content_image(index) for index in range(3)]
    dev.load_feeder(loaded)
    _install(monkeypatch, dev)
    outlet = RecordingOutlet()

    outcome = run_pass(_DEVICE, _feeder(), outlet)

    assert outlet.stages == [
        (ScanStage.OPEN, None),
        (ScanStage.CONFIGURE, None),
        (ScanStage.START, 1),
        (ScanStage.READ, 1),
        (ScanStage.START, 2),
        (ScanStage.READ, 2),
        (ScanStage.START, 3),
        (ScanStage.READ, 3),
        (ScanStage.START, 4),
        (ScanStage.CLOSE, None),
    ]
    assert outlet.configured_calls == [(300, True)]
    assert outlet.timeline.index("configured") < outlet.timeline.index("page 1")
    framing = scan_session_mod._PageFraming("full", 300, geometry_set=False)
    assert [page.number for page in outlet.pages] == [1, 2, 3]
    assert [page.dpi for page in outlet.pages] == [300, 300, 300]
    assert [page.image.tobytes() for page in outlet.pages] == [
        framing.crop(image).tobytes() for image in loaded
    ]
    assert outcome == PassOutcome(
        resolution=300, rejected=0, substituted_source=None, cap_reached=None
    )
    assert dev.close_calls == 1


def test_an_unreadable_sheet_is_counted_not_handed_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sheet failing the integrity check is skipped and counted."""
    dev = FakeSaneDev()
    dev.load_feeder([_content_image(0), _unreadable_image(), _content_image(2)])
    _install(monkeypatch, dev)
    outlet = RecordingOutlet()

    outcome = run_pass(_DEVICE, _feeder(), outlet)

    assert [page.number for page in outlet.pages] == [1, 3]
    assert outcome.rejected == 1


def test_a_pass_of_only_unreadable_sheets_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every sheet unreadable is an error, and no page is handed on."""
    dev = FakeSaneDev()
    dev.load_feeder([_unreadable_image(), _unreadable_image()])
    _install(monkeypatch, dev)
    outlet = RecordingOutlet()

    with pytest.raises(ScanError) as caught:
        run_pass(_DEVICE, _feeder(), outlet)

    assert str(caught.value) == (
        "All 2 page(s) fed were unreadable and were skipped (zero dimensions, "
        "or below 10000 bytes of image data); no usable page was produced"
    )
    assert outlet.pages == []
    assert dev.close_calls == 1


def test_an_empty_feeder_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """No sheet at all is the feeder-empty error, and the device is closed."""
    dev = FakeSaneDev(pages=0)
    _install(monkeypatch, dev)

    with pytest.raises(FeederEmptyError) as caught:
        run_pass(_DEVICE, _feeder(), RecordingOutlet())

    assert str(caught.value) == "No paper detected in feeder"
    assert dev.close_calls == 1


def test_the_named_feeder_cap_ends_the_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """A named feeder stops at its cap and names the sheet it did not keep."""
    monkeypatch.setattr(scan_session_mod, "_MAX_ADF_PAGES", 2)
    monkeypatch.setattr(scan_session_mod, "_MAX_AUTO_FEEDER_PAGES", 3)
    dev = FakeSaneDev(pages=4)
    _install(monkeypatch, dev)
    outlet = RecordingOutlet()

    outcome = run_pass(_DEVICE, _feeder(), outlet)

    assert [page.number for page in outlet.pages] == [1, 2]
    assert outcome.cap_reached == PassCapReached(
        cap=2, sheet_not_kept=3, auto_source=False
    )
    assert dev.calls.count("start") == 3


def test_an_auto_source_uses_the_auto_feeder_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto sent through the feeder is held to the lower cap."""
    monkeypatch.setattr(scan_session_mod, "_MAX_ADF_PAGES", 3)
    monkeypatch.setattr(scan_session_mod, "_MAX_AUTO_FEEDER_PAGES", 2)
    dev = FakeSaneDev(pages=4)
    dev.report_sources(["Auto", "ADF"])
    _install(monkeypatch, dev)
    outlet = RecordingOutlet()
    settings = ScanSettings(
        source="Auto", resolution=300, mode="Color", auto_source_mode="adf"
    )

    outcome = run_pass(_DEVICE, settings, outlet)

    assert [page.number for page in outlet.pages] == [1, 2]
    assert outcome.cap_reached == PassCapReached(
        cap=2, sheet_not_kept=3, auto_source=True
    )


def test_a_stop_answer_ends_the_pass_before_the_next_sheet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop answer for page 1 means sheet 2 is never started."""
    dev = FakeSaneDev(pages=3)
    _install(monkeypatch, dev)
    outlet = RecordingOutlet(answers=[False])

    with pytest.raises(PassStopped):
        run_pass(_DEVICE, _feeder(), outlet)

    assert dev.calls.count("start") == 1
    assert [page.number for page in outlet.pages] == [1]
    assert dev.cancel_calls >= 1
    assert dev.close_calls == 1


def test_a_page_read_during_a_cancel_is_discarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The image a cancelled read returned is never handed on."""
    dev = FakeSaneDev(pages=3)
    _install(monkeypatch, dev)
    outlet = RecordingOutlet()
    real_snap = FakeSaneDev.snap

    def snap_then_cancel(self: FakeSaneDev, *, no_cancel: bool = False) -> Image.Image:
        image = real_snap(self, no_cancel=no_cancel)
        outlet.cancel = True
        return image

    monkeypatch.setattr(FakeSaneDev, "snap", snap_then_cancel)

    with pytest.raises(PassStopped):
        run_pass(_DEVICE, _feeder(), outlet)

    assert outlet.pages == []
    assert dev.calls.count("snap") == 1
    assert dev.close_calls == 1


class _CancelOnRegister(RecordingOutlet):
    """An outlet whose cancel arrives just as the handle is registered."""

    def reading(self, dev: SaneDevice | None) -> None:
        """Record the handle, and request the cancel as it is registered."""
        super().reading(dev)
        if dev is not None:
            self.cancel = True


def test_a_cancel_before_the_handle_is_registered_starts_no_sheet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A cancel that came before the handle was registered starts nothing.

    Cancelling the handle could not reach it then, so the sheet would
    otherwise be started and read in full before the cancel was seen.
    """
    dev = FakeSaneDev(pages=3)
    _install(monkeypatch, dev)

    with pytest.raises(PassStopped):
        run_pass(_DEVICE, _feeder(), _CancelOnRegister())

    assert dev.calls.count("start") == 0
    assert dev.calls.count("snap") == 0
    assert dev.close_calls == 1


def test_a_cancel_during_start_cancels_the_read_it_began(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A cancel that lands while ``start`` runs cancels the read, which is not taken.

    A ``sane_cancel`` made before or during ``sane_start`` does not stop the
    read that start begins, so the read is cancelled once start returns.
    """
    dev = FakeSaneDev(pages=3)
    _install(monkeypatch, dev)
    outlet = RecordingOutlet()
    events: list[str] = []
    real_start, real_cancel = FakeSaneDev.start, FakeSaneDev.cancel

    def start_then_cancel(self: FakeSaneDev) -> None:
        real_start(self)
        events.append("start")
        outlet.cancel = True

    def record_cancel(self: FakeSaneDev) -> None:
        events.append("cancel")
        real_cancel(self)

    monkeypatch.setattr(FakeSaneDev, "start", start_then_cancel)
    monkeypatch.setattr(FakeSaneDev, "cancel", record_cancel)

    with pytest.raises(PassStopped):
        run_pass(_DEVICE, _feeder(), outlet)

    assert dev.calls.count("snap") == 0
    assert events[:2] == ["start", "cancel"]
    assert outlet.pages == []
    assert dev.close_calls == 1


@pytest.mark.parametrize("failing", ["start", "snap"])
def test_a_failed_read_waits_for_the_reader_before_cancelling(
    monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    """A jam at start or snap waits for the backend's reader before the cancel."""
    jam = FakeSaneError("Document feeder jammed")
    if failing == "start":
        dev = FakeSaneDev(start_error=jam, start_error_page=0)
    else:
        dev = FakeSaneDev()
        dev.fail_call("snap", jam)
    _install(monkeypatch, dev)
    events: list[str] = []

    def record_wait(before: frozenset[int] | None, limit: float = 1.0) -> bool:
        del before, limit
        events.append("wait")
        return True

    real_cancel = FakeSaneDev.cancel

    def record_cancel(self: FakeSaneDev) -> None:
        events.append("cancel")
        real_cancel(self)

    monkeypatch.setattr(scan_session_mod, "_await_backend_threads", record_wait)
    monkeypatch.setattr(FakeSaneDev, "cancel", record_cancel)

    with pytest.raises(ScanError) as caught:
        run_pass(_DEVICE, _feeder(), RecordingOutlet())

    assert events == ["wait", "cancel"]
    assert str(caught.value) == "Scanner error on page 1: Document feeder jammed"
    assert caught.value.__cause__ is jam
    assert dev.close_calls == 1


def test_an_empty_flatbed_is_reported_as_feeder_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The end-of-feed message on the flatbed path is the feeder-empty error."""
    empty = FakeSaneError(_END_OF_FEED)
    dev = FakeSaneDev(start_error=empty, start_error_page=0)
    _install(monkeypatch, dev)

    with pytest.raises(FeederEmptyError) as caught:
        run_pass(_DEVICE, _flatbed(), RecordingOutlet())

    assert str(caught.value) == "No paper detected in feeder"
    assert caught.value.__cause__ is empty


def test_a_flatbed_read_error_names_the_device(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Any other flatbed failure names the device, with control bytes defused."""
    device_id = "net:host\x1b[31m:test:0"
    failure = FakeSaneError("Error during device I/O")
    dev = FakeSaneDev()
    dev.fail_call("snap", failure)
    _install(monkeypatch, dev)

    with pytest.raises(ScanError) as caught:
        run_pass(device_id, _flatbed(), RecordingOutlet())

    assert str(caught.value) == (
        f"Scanner error on {neutralise_controls(device_id)}: Error during device I/O"
    )
    assert "\x1b" not in str(caught.value)
    assert caught.value.__cause__ is failure
    assert not isinstance(caught.value, FeederEmptyError)


def test_an_unreadable_flatbed_page_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A flatbed has no next page, so an unreadable one ends the pass."""
    dev = FakeSaneDev()
    dev.load_feeder([_unreadable_image()])
    _install(monkeypatch, dev)
    outlet = RecordingOutlet()

    with pytest.raises(ScanError) as caught:
        run_pass(_DEVICE, _flatbed(), outlet)

    assert str(caught.value) == (
        "The scanner returned an unreadable page (zero dimensions, or below "
        "10000 bytes of image data)"
    )
    assert outlet.pages == []
    assert dev.close_calls == 1


def test_an_open_failure_is_chained_and_closes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device that will not open is a chained error, and nothing is closed."""
    refusal = FakeSaneError("Invalid argument")
    dev = FakeSaneDev()
    _install(monkeypatch, dev, open_error=refusal)
    outlet = RecordingOutlet()

    with pytest.raises(ScanError) as caught:
        run_pass(_DEVICE, _feeder(), outlet)

    assert str(caught.value) == "Could not open scanner test:0: Invalid argument"
    assert caught.value.__cause__ is refusal
    assert dev.close_calls == 0
    assert dev.cancel_calls == 0
    assert outlet.stages == [(ScanStage.OPEN, None)]


def test_a_close_failure_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A close failure neither replaces the outcome nor the pass's own error."""
    dev = FakeSaneDev(pages=1)
    dev.fail_call("close", FakeSaneError("close went wrong"))
    _install(monkeypatch, dev)

    with caplog.at_level(logging.WARNING, logger=_SESSION_LOGGER):
        outcome = run_pass(_DEVICE, _feeder(), RecordingOutlet())

    assert outcome.resolution == 300
    warnings = [
        record
        for record in caplog.records
        if record.name == _SESSION_LOGGER and record.levelno == logging.WARNING
    ]
    assert [record.getMessage() for record in warnings] == [
        "Could not close scanner test:0"
    ]

    dev.load_feeder([_unreadable_image()])
    with pytest.raises(ScanError, match="unreadable page"):
        run_pass(_DEVICE, _flatbed(), RecordingOutlet())
    assert dev.close_calls == 2


class _ReportGoneError(Exception):
    """The outlet could not report a stage: its channel is gone."""


class _CloseUnreported(RecordingOutlet):
    """An outlet that cannot report the close stage."""

    def stage(self, stage: ScanStage, page: int | None) -> None:
        """Record the stage, then fail to report the close."""
        super().stage(stage, page)
        if stage is ScanStage.CLOSE:
            raise _ReportGoneError


@pytest.mark.parametrize("flatbed_page", ["good", "unreadable"])
def test_the_device_closes_when_the_close_cannot_be_reported(
    monkeypatch: pytest.MonkeyPatch, flatbed_page: str
) -> None:
    """
    An outlet that fails to report the close still has the device closed.

    The pass's own error goes on unchanged; with none, the report's failure
    is raised once the device is closed.
    """
    dev = FakeSaneDev()
    image = _content_image(0) if flatbed_page == "good" else _unreadable_image()
    dev.load_feeder([image])
    _install(monkeypatch, dev)
    expected = _ReportGoneError if flatbed_page == "good" else ScanError

    with pytest.raises(expected):
        run_pass(_DEVICE, _flatbed(), _CloseUnreported())

    assert dev.cancel_calls >= 1
    assert dev.close_calls == 1


class _ReadUnreported(RecordingOutlet):
    """An outlet that cannot report the read stage."""

    def stage(self, stage: ScanStage, page: int | None) -> None:
        """Record the stage, then fail to report the read."""
        super().stage(stage, page)
        if stage is ScanStage.READ:
            raise _ReportGoneError


@pytest.mark.parametrize("settings", [_feeder(), _flatbed()], ids=["feeder", "flatbed"])
def test_an_outlet_failure_mid_sheet_is_raised_as_it_was(
    monkeypatch: pytest.MonkeyPatch, settings: ScanSettings
) -> None:
    """
    An outlet that fails part way through a sheet is not a scanner error.

    Its own error comes out unchanged, never wrapped as "Scanner error on
    page 1", and the device is still closed.
    """
    dev = FakeSaneDev(pages=1)
    _install(monkeypatch, dev)

    with pytest.raises(_ReportGoneError):
        run_pass(_DEVICE, settings, _ReadUnreported())

    assert dev.calls.count("snap") == 0
    assert dev.close_calls == 1


def test_the_reading_handle_is_registered_around_each_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handle is registered before each start and cleared after each read."""
    dev = FakeSaneDev(pages=1)
    _install(monkeypatch, dev)
    outlet = RecordingOutlet()

    run_pass(_DEVICE, _feeder(), outlet)

    assert outlet.timeline == [
        "open",
        "configure",
        "configured",
        "start 1",
        "reading",
        "read 1",
        "reading done",
        "page 1",
        "start 2",
        "reading",
        "reading done",
        "close",
    ]
    registered = [handle for handle in outlet.handles if handle is not None]
    assert len(registered) == 2
    assert all(isinstance(handle, FakeSaneHandle) for handle in registered)
    assert registered[0] is registered[1]


@pytest.mark.parametrize("exit_fails", [False, True])
def test_restart_library_exits_then_inits(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    *,
    exit_fails: bool,
) -> None:
    """A restart is sane_exit then sane_init; a failing exit is only logged."""
    module = _install(monkeypatch, FakeSaneDev())
    order: list[str] = []
    real_exit, real_init = module.exit, module.init
    exit_failure = FakeSaneError("exit went wrong")

    def recorded_exit() -> None:
        order.append("exit")
        real_exit()
        if exit_fails:
            raise exit_failure

    def recorded_init() -> tuple[int, int, int, int]:
        order.append("init")
        return real_init()

    monkeypatch.setattr(module, "exit", recorded_exit)
    monkeypatch.setattr(module, "init", recorded_init)

    with caplog.at_level(logging.WARNING, logger=_SESSION_LOGGER):
        version = restart_library()

    assert order == ["exit", "init"]
    assert (module.exit_call_count, module.init_call_count) == (1, 1)
    assert version == real_init()
    warned = [
        record
        for record in caplog.records
        if record.name == _SESSION_LOGGER and record.levelno == logging.WARNING
    ]
    assert len(warned) == (1 if exit_fails else 0)


def test_start_library_reports_an_init_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SANE that will not start is a chained ScanError with today's text."""
    failure = FakeSaneError("Error during device I/O")
    _install(monkeypatch, FakeSaneDev(), init_error=failure)

    with pytest.raises(ScanError) as caught:
        start_library()

    assert str(caught.value).startswith("Could not initialise SANE: ")
    assert str(caught.value) == "Could not initialise SANE: Error during device I/O"
    assert caught.value.__cause__ is failure
