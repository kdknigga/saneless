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

from saneless.scanner.base import ScanSettings
from saneless.scanner.sane_backend import SaneBackend

if TYPE_CHECKING:
    from collections.abc import Iterator


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
        """
        names = [device.name for device in SaneBackend().get_devices()]
        assert "test:0" in names

    def test_capabilities_report_the_long_feeder_name(self) -> None:
        """
        The device reports the full ADF source string, not an abbreviation.

        Nothing is asserted about ``resolutions`` here: the device reports
        resolution as the range ``(1.0, 1200.0, 1.0)`` and ``get_capabilities``
        returns an empty list for it today.  Plan 24-08 adds that assertion
        when D-13 makes it true.
        """
        capabilities = SaneBackend().get_capabilities("test:0")
        assert "Automatic Document Feeder" in capabilities.sources

    def test_ten_pages_come_back_through_the_feeder(self) -> None:
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
        ten sheets are taken off it, and nothing is thrown away on the way
        out.  The assertion is unchanged from the day it was written; only the
        behaviour underneath it moved (D-05).
        """
        settings = ScanSettings(
            source="Automatic Document Feeder", resolution=75, mode="Gray"
        )
        pages = SaneBackend().scan_pages("test:0", settings).pages
        assert len(pages) == 10

    def test_every_uniformly_black_page_survives_the_scanner_layer(self) -> None:
        """
        All ten pages come back, and every one of them is uniformly black.

        SCNR-03 proven against real hardware instead of against a double.  The
        ``test`` backend hands back solid black at mean 0.0 / stddev 0.0 --
        exactly the statistics the deleted content policy keyed on -- so this
        is the strongest evidence available that the scanner layer no longer
        judges a page by what is printed on it.  Whether a blank page is worth
        keeping is decided one layer up, under the profile's
        ``enable_empty_page_detection`` toggle, where the user can see it.
        """
        settings = ScanSettings(
            source="Automatic Document Feeder", resolution=75, mode="Gray"
        )
        pages = SaneBackend().scan_pages("test:0", settings).pages
        assert len(pages) == 10
        assert all(page.convert("L").getextrema() == (0, 0) for page in pages)
