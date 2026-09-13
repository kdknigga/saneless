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

        **This test is expected to fail when it is committed, and that is the
        point.** The ``test`` backend's default picture is solid black, so
        today's ``_validate_page_image`` discards every acquired page as
        "pure black" and ``scan_pages`` returns nothing.  The measured
        failure is ``assert 0 == 10`` accompanied by ten
        ``Page N: pure black (mean=0.0, stddev=0.0), skipping`` warnings.

        The feeder name and the routing are already correct; it is the
        content policy that destroys the scan.  Plan 24-04's D-05 removes the
        pure-black check and turns this green.

        It is deliberately left as a plain failure rather than registered as
        an expected one.  Strict expected-failure handling is enabled in this
        project, so registering it would flip the result to an error the
        moment D-05 landed -- and, worse, it would record a defect as
        intended behaviour.
        """
        settings = ScanSettings(
            source="Automatic Document Feeder", resolution=75, mode="Gray"
        )
        pages = list(SaneBackend().scan_pages("test:0", settings))
        assert len(pages) == 10
