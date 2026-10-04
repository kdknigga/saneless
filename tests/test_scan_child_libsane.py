"""
The real scan child against real libsane's ``test`` backend.

These tests start ``_scan_child.py`` through saneless's own launcher, exactly
as a scan job does, and drive it through ``ScanChildSession``: a whole feeder
pass, and a cancel that reaches the child while a page is being read.  Each
child reads its SANE configuration from a ``SANE_CONFIG_DIR`` of the test's
own, because options set through python-sane in one process do not reach
another; nothing here loads libsane in the pytest process.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Final

import pytest

from saneless.exceptions import ScanInterrupted
from saneless.pipeline import _SPOOL_LABEL_A
from saneless.scanner import listing, scan_child
from saneless.scanner.base import ScanSettings
from saneless.scanner.scan_child import ScanChildSession
from saneless.spool import SpooledPageSink
from saneless.vocabulary import ScanStage
from tests.conftest import poll_until

if TYPE_CHECKING:
    from pathlib import Path

    from saneless.scanner.scan_protocol import StageFrame

pytestmark = pytest.mark.sane_hardware

# The options the slowed read needs, under the names the device lists them
# by.  A test backend without them cannot make a read slow.
_READ_DELAY_OPTION_NAMES: Final = frozenset(
    {"read-delay", "read-delay-duration", "read-limit", "read-limit-size"}
)

# Makes every read genuinely slow: the backend's reader waits 0.2 s before
# each buffer it writes, and every buffer is one byte.  At 1200 dpi a page is
# millions of bytes, so the read is still under way long after any cancel.
_SLOW_READ_CONF: Final = (
    "read-delay true\nread-delay-duration 200000\nread-limit true\nread-limit-size 1\n"
)
_SLOW_READ_RESOLUTION: Final = 1200

# The test backend's feeder holds ten sheets.
_FEEDER_SHEETS: Final = 10

# How long the read stage may take to begin.  A bound, not a pause: the child
# reaches it in well under a second.
_READ_SEEN_BUDGET_SECONDS: Final = 30.0

# How much longer than the cancel grace the cancelled pass may take to
# return, for the child's exit and its reaping.
_REAP_ALLOWANCE_SECONDS: Final = 2.0


def _sane_config(directory: Path, test_conf: str | None = None) -> Path:
    """
    Write a SANE configuration naming only the ``test`` backend.

    Args:
        directory: Where to write it; created here.
        test_conf: The ``test.conf`` contents, or ``None`` for the
            backend's compiled-in defaults.

    Returns:
        The directory, for ``SANE_CONFIG_DIR``.

    """
    directory.mkdir()
    (directory / "dll.conf").write_text("test\n", encoding="ascii")
    if test_conf is not None:
        (directory / "test.conf").write_text(test_conf, encoding="ascii")
    return directory


def _spool(tmp_path: Path) -> SpooledPageSink:
    """
    Build a real sink spooling under ``tmp_path``.

    Returns:
        The sink.

    """
    directory = tmp_path / "spool"
    directory.mkdir()
    return SpooledPageSink(directory, _SPOOL_LABEL_A, 0)


class _ReadWatchingSession(ScanChildSession):
    """
    A scan session that notes when the child reports it is reading a page.

    Attributes:
        read_seen: Set once the child has entered the read stage.

    """

    def __init__(self, abort: threading.Event) -> None:
        """
        Start children through the real launcher.

        Args:
            abort: The session's abort Event.

        """
        super().__init__(lambda: scan_child.start_scan_child(""), abort=abort)
        self.read_seen = threading.Event()

    def _note_stage(self, frame: StageFrame) -> None:
        """Record the stage, and note the read stage."""
        super()._note_stage(frame)
        if frame.stage == ScanStage.READ.value:
            self.read_seen.set()


def test_a_real_feeder_pass_returns_ten_pages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    A feeder pass through the real child brings back every sheet.

    Args:
        monkeypatch: Points SANE at the test's configuration.
        tmp_path: Holds the configuration and the spool.

    """
    monkeypatch.setenv("SANE_CONFIG_DIR", str(_sane_config(tmp_path / "sane.d")))
    settings = ScanSettings(
        source="Automatic Document Feeder", resolution=75, mode="Gray"
    )
    session = ScanChildSession(lambda: scan_child.start_scan_child(""))
    try:
        batch = session.scan_pass("test:0", settings, _spool(tmp_path))
    finally:
        session.close()

    assert len(batch.pages) == _FEEDER_SHEETS
    assert batch.actual_resolution == 75
    assert batch.cap_reached is None
    assert session.children_killed == 0


def test_a_cancel_mid_read_ends_the_real_child_within_the_grace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    An abort set while a page is read makes the real child cancel and exit.

    The read is slowed so it is certainly under way when the abort is set.
    The child must cancel the read, close the device and exit on its own,
    so nothing is killed, and the pass must return within the cancel grace
    plus the time to reap the child.

    Args:
        monkeypatch: Points SANE at the test's configuration.
        tmp_path: Holds the configuration and the spool.

    """
    monkeypatch.setenv(
        "SANE_CONFIG_DIR", str(_sane_config(tmp_path / "sane.d", _SLOW_READ_CONF))
    )
    reply = listing.run_listing_child(
        listing.ListingRequest(capabilities="test:0"), configured_host=""
    )
    # A capability read that failed is a failure, never a reason to skip.
    assert reply.options is not None, (
        reply.init_error,
        reply.open_error,
        reply.options_error,
    )
    offered = {option[0] for option in reply.options}
    if not offered >= _READ_DELAY_OPTION_NAMES:
        pytest.skip("this libsane test backend has no read-delay options")

    abort = threading.Event()
    session = _ReadWatchingSession(abort)
    set_at: list[float] = []

    def abort_once_reading() -> None:
        if poll_until(session.read_seen.is_set, _READ_SEEN_BUDGET_SECONDS):
            set_at.append(time.monotonic())
            abort.set()

    helper = threading.Thread(target=abort_once_reading, daemon=True)
    helper.start()
    settings = ScanSettings(
        source="Flatbed", resolution=_SLOW_READ_RESOLUTION, mode="Gray"
    )
    try:
        with pytest.raises(ScanInterrupted):
            session.scan_pass("test:0", settings, _spool(tmp_path))
        raised_at = time.monotonic()
    finally:
        session.close()
        helper.join(_READ_SEEN_BUDGET_SECONDS)

    assert set_at, "the child never reported reading the page"
    elapsed = raised_at - set_at[0]
    assert session.children_killed == 0, f"killed after {elapsed:.1f} s"
    assert elapsed < scan_child.CANCEL_GRACE_SECONDS + _REAP_ALLOWANCE_SECONDS
