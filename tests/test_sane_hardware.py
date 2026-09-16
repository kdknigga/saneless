"""
Integration tests that drive the real SANE ``test`` backend.

Every other scanner test in this suite talks to a double.  This module talks
to libsane, so that a fake which has drifted from the library cannot keep a
defect green on its own (M-32).  It is gated behind the ``sane_hardware``
marker and deselected by the default command.

No extra apt package is needed to run these: ``libsane-dev`` depends on
``libsane1``, which ships ``libsane-test.so.1``, and both CI jobs already
install it.  The device used throughout is ``test:0``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import saneless.scanner.sane_backend as sane_backend_mod
from saneless.exceptions import ScanError
from saneless.pipeline import _SPOOL_LABEL_A
from saneless.scanner.base import ScanSettings
from saneless.scanner.sane_backend import SaneBackend
from saneless.spool import SpooledPageSink
from tests.conftest import images_of

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from PIL import Image

# The free-space reserve these sinks keep beyond the page being written.  Zero
# for the same reason the in-process scanner tests use zero: these tests are
# about libsane, not about D-07's shortfall arithmetic, and a positive reserve
# would fail them on a CI runner with a nearly full disk.
_NO_FREE_SPACE_RESERVE = 0

# The options that make the real ``test`` backend's read genuinely slow, and
# the resolution without which they do nothing.
#
# The delay is implemented in the backend's reader child as one ``usleep`` per
# buffer written (``backend/test.c``), so it repeats only as often as there are
# buffers.  ``read_limit_size = 1`` makes every buffer a single byte and 1200
# dpi makes there be a great many of them; at a low resolution the whole page
# fits in one buffer and the scan *cannot* be made slow however long the delay
# is.  The resolution is therefore load-bearing, not incidental.
_SLOW_READ_OPTIONS: tuple[tuple[str, object], ...] = (
    ("read_delay", True),
    ("read_delay_duration", 200_000),  # microseconds; the option's maximum
    ("read_limit", True),
    ("read_limit_size", 1),
    ("resolution", 1200),
)

# How long the cancel test lets the read run before bounding it.  Long enough
# that the read is provably under way and short enough to keep the test brisk;
# the measured read was still blocked after three seconds with these options.
_SLOW_READ_TIMEOUT_SECONDS = 0.5


def _page_sink_for(tmp_path: Path) -> SpooledPageSink:
    """
    Build a real sink spooling one pass's pages under ``tmp_path``.

    ``scan_pages`` takes a sink as its third argument, so these tests supply
    one just as the pipeline does.  It is a real ``SpooledPageSink`` and not a
    mock: the point of driving libsane at all is that everything downstream of
    it is real too, and a page that could not be written would otherwise still
    pass.

    Args:
        tmp_path: The per-test temporary directory pytest removes afterwards.

    Returns:
        A sink ready to be handed to ``scan_pages``.

    """
    directory = tmp_path / "spool"
    directory.mkdir(exist_ok=True)
    return SpooledPageSink(directory, _SPOOL_LABEL_A, _NO_FREE_SPACE_RESERVE)


@pytest.fixture(scope="session", autouse=True)
def _sane_config_dir(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """
    Point SANE at a ``dll.conf`` naming only the ``test`` backend.

    Session-scoped deliberately, and this is not a style choice.  It was
    measured that ``SANE_CONFIG_DIR`` is honoured only before the first
    ``sane.init()`` in a process, that ``sane.exit()`` followed by a re-init
    does *not* reset it, and that backends accumulate across re-inits so the
    developer's real scanner never leaves the device list.  The natural thing
    to write -- a function-scoped fixture calling ``setenv`` -- was measured
    to fail.  ``pytest.MonkeyPatch.context()`` is used here because the
    function-scoped fixture of that name is unavailable at session scope, and
    the context manager guarantees the variable is unset at session end.

    Only ``dll.conf`` is written; no ``test.conf`` is copied.  The backend's
    compiled-in defaults already give two devices and a ten-sheet feeder.

    Args:
        tmp_path_factory: Session-scoped temporary directory factory.

    Yields:
        None, once the environment is configured.

    """
    config_dir = tmp_path_factory.mktemp("sane.d")
    (config_dir / "dll.conf").write_text("test\n")
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setenv("SANE_CONFIG_DIR", str(config_dir))
        yield


@pytest.mark.sane_hardware
class TestRealSaneTestBackend:
    """SCNR-08: enumerate, interrogate and scan the real ``test`` device."""

    def test_enumeration_lists_the_test_device(self) -> None:
        """
        The configured backend appears by name.

        The assertion is positive on purpose.  In CI there is no real
        scanner, so an empty device list and a missing test backend look
        identical -- a "list is not empty" assertion would pass for the wrong
        reason with a half-broken fixture, and would also pass on a developer
        machine that had leaked its own scanner into the list.

        The guard check comes first, and it is not defensive padding.  The
        process-global init guard (D-17) makes ``SaneBackend()`` a no-op when
        some earlier test in the same process left it set over a fake ``sane``
        module, and the only symptom is this empty list -- a diagnosis nobody
        would reach from ``assert 'test:0' in []``.  Naming the real cause here
        is what turns that into a one-line answer.
        """
        assert sane_backend_mod._INIT.done is False, (
            "SANE was already marked initialised before this test constructed a "
            "backend, so sane.init() was skipped and the device list is empty "
            "for a reason that has nothing to do with libsane: an earlier test "
            "leaked the process-global init guard (D-17)"
        )
        names = [device.name for device in SaneBackend().get_devices()]
        assert "test:0" in names

    def test_capabilities_report_the_long_feeder_name(self) -> None:
        """The device reports the full ADF source string, not an abbreviation."""
        capabilities = SaneBackend().get_capabilities("test:0")
        assert "Automatic Document Feeder" in capabilities.sources

    def test_capabilities_report_the_resolution_range_the_device_gave(self) -> None:
        """
        SCNR-06 proven against real libsane rather than against a double.

        ``test:0`` constrains resolution with the measured range
        ``(1.0, 1200.0, 1.0)``.  ``get_capabilities`` read only the word-list
        shape, so that answer was discarded and this line would have read
        ``[] == (1.0, 1200.0, 1.0)`` -- this is the assertion plan 24-01
        deliberately deferred, and the one that would have caught N-01.

        The word list stays empty on purpose.  The device gave a range and no
        list, the two are different facts, and neither is synthesised from the
        other: a list expanded out of this range would be saneless's invention
        rather than the scanner's answer.
        """
        capabilities = SaneBackend().get_capabilities("test:0")

        assert capabilities.resolution_range == (1.0, 1200.0, 1.0)
        assert capabilities.resolutions == []

    def test_ten_pages_come_back_through_the_feeder(self, tmp_path: Path) -> None:
        """
        Ten sheets in the feeder yield ten pages.

        This assertion could not have passed before plan 24-04, and that is
        what makes it worth having.  The ``test`` backend's default picture is
        solid black, and the scanner backend used to discard any page whose
        statistics read as pure black -- so all ten sheets were destroyed
        inside ``scan_pages``, this line read ``assert 0 == 10``, and ten skip
        warnings named the pages one by one.

        What it proves now is that the feeder is drained end to end against
        real libsane: the long ADF source name routes to the multi-page path,
        ten sheets are taken off it, are spooled one at a time, and nothing is
        thrown away on the way out.  The count assertion is unchanged from the
        day it was written; only the behaviour underneath it moved (D-05, and
        now D-01).

        Args:
            tmp_path: Where the ten pages are spooled.

        """
        settings = ScanSettings(
            source="Automatic Document Feeder", resolution=75, mode="Gray"
        )
        pages = (
            SaneBackend().scan_pages("test:0", settings, _page_sink_for(tmp_path)).pages
        )
        assert len(pages) == 10
        # Order is the records' own and the files really exist -- against real
        # libsane, not against a double (D-02).
        assert [record.sequence for record in pages] == list(range(1, 11))
        assert all(record.path.exists() for record in pages)

    def test_every_uniformly_black_page_survives_the_scanner_layer(
        self, tmp_path: Path
    ) -> None:
        """
        All ten pages come back, and every one of them is uniformly black.

        SCNR-03 proven against real hardware instead of against a double.  The
        ``test`` backend hands back solid black at mean 0.0 / stddev 0.0 --
        exactly the statistics the deleted content policy keyed on -- so this
        is the strongest evidence available that the scanner layer no longer
        judges a page by what is printed on it.  Whether a blank page is worth
        keeping is decided one layer up, under the profile's
        ``enable_empty_page_detection`` toggle, where the user can see it.

        Asserted twice over, because the two say different things.  The record
        statistics are what ``pipeline._drop_empty_pages`` will actually judge,
        measured once at spool time; the read-back through ``images_of`` proves
        the spooled PNG -- the file the PDF embeds -- really holds those pixels.

        Args:
            tmp_path: Where the ten pages are spooled.

        """
        settings = ScanSettings(
            source="Automatic Document Feeder", resolution=75, mode="Gray"
        )
        batch = SaneBackend().scan_pages("test:0", settings, _page_sink_for(tmp_path))
        assert len(batch.pages) == 10
        assert all(record.mean == 0.0 for record in batch.pages)
        assert all(record.stddev == 0.0 for record in batch.pages)
        assert all(
            image.convert("L").getextrema() == (0, 0) for image in images_of(batch)
        )


@pytest.mark.sane_hardware
class TestRealSaneCancelSequence:
    """
    HARD-03's D-12 sequence, proven once against libsane instead of a double.

    Everywhere else the cancel path is driven through an Event-gated fake, and
    a fake is exactly what cannot answer the question this class asks: whether
    a real ``sane_cancel`` issued from another thread actually releases a real
    ``sane_read``, and whether the handle is still usable afterwards.  Both are
    properties of the C library, and both are the premises the whole timeout
    design rests on.
    """

    def test_a_cancel_unblocks_a_slow_read_and_the_handle_still_closes(self) -> None:
        """
        A read made genuinely slow is cancelled, returns, and the handle closes.

        The backend's own helper drives it, so what is proven is the shipped
        sequence and not a re-implementation of it: the timeout fires, the
        cancel goes out on its own thread, the reader comes back inside the
        grace, and the error therefore does *not* accuse the scanner of
        ignoring the cancel.  ``close()`` afterwards is the last link -- it is
        the call the frontend may make only once the read has returned.
        """
        backend = SaneBackend()
        assert backend is not None  # the constructor is what ran sane.init()
        device = sane_backend_mod.sane.open("test:0")
        try:
            try:
                device.source = "Automatic Document Feeder"
                device.mode = "Gray"
                for name, value in _SLOW_READ_OPTIONS:
                    setattr(device, name, value)
            except AttributeError:
                pytest.skip("this libsane test backend has no read-delay options")

            def start_and_snap() -> Image.Image:
                device.start()
                return device.snap()

            with pytest.raises(ScanError) as raised:
                sane_backend_mod._acquire_with_timeout(
                    device, start_and_snap, "Page 1", _SLOW_READ_TIMEOUT_SECONDS
                )

            message = str(raised.value)
            assert "timed out" in message
            # The real device answers the cancel, so the unresponsive-cancel
            # wording must be absent -- and the backend must not be wedged.
            assert "did not respond" not in message
            assert sane_backend_mod._WEDGE.stuck is False
        finally:
            # Not in a suppression: "close() succeeds after a cancelled read"
            # is one of the two things this test exists to establish, so a
            # failure here has to fail the test rather than be swallowed.
            device.close()
