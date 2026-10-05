"""
Integration tests that drive the real SANE ``test`` backend.

Every other scanner test in this suite talks to a double.  This module talks
to libsane, so that a fake which has drifted from the library cannot keep a
defect green on its own.  It is gated behind the ``sane_hardware``
marker and deselected by the default command.

No extra apt package is needed to run these: ``libsane-dev`` depends on
``libsane1``, which ships ``libsane-test.so.1``, and both CI jobs already
install it.  The device used throughout is ``test:0``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

import saneless.scanner.scan_session as scan_session_mod
from saneless.exceptions import ScanError
from saneless.pipeline import _SPOOL_LABEL_A
from saneless.scanner import child_launch
from saneless.scanner.base import ScanSettings
from saneless.scanner.sane_backend import SaneBackend
from saneless.spool import SpooledPageSink
from tests.conftest import images_of, libsane_in_this_process
from tests.fake_saned import SanedBehaviour, fake_saned

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

# The free-space reserve these sinks keep beyond the page being written.  Zero
# for the same reason the in-process scanner tests use zero: these tests are
# about libsane, not about the spool's shortfall arithmetic, and a positive reserve
# would fail them on a CI runner with a nearly full disk.
_NO_FREE_SPACE_RESERVE = 0

# The only port libsane's net backend dials: it resolves every host with
# getaddrinfo(name, "sane-port"), and SANE_NET_HOSTS has no port syntax.
_SANE_PORT = 6566

# How long one lost-connection subprocess may run.  A bound, not a pause: the
# measured runs finish in well under a second, and this only stops a hung
# libsane from hanging the suite.
_SUBPROCESS_TIMEOUT_SECONDS = 60

# Raw python-sane listing twice in one process: the in-process call sequence
# that SaneBackend.get_devices() avoids by listing in a child.
_RAW_LISTS_TWICE = (
    "import sane; sane.init(); "
    "print(len(sane.get_devices()), flush=True); "
    "print(len(sane.get_devices()), flush=True)"
)

# saneless listing twice from one process, the way the web app does.
_SANELESS_LISTS_TWICE = (
    "from saneless.scanner.sane_backend import SaneBackend; "
    'b = SaneBackend(host="127.0.0.1"); '
    "print(len(b.get_devices()), flush=True); "
    "print(len(b.get_devices()), flush=True)"
)


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


def _run_against_loopback_saned(
    code: str, tmp_path: Path
) -> subprocess.CompletedProcess[str]:
    """
    Run ``code`` in a fresh interpreter whose SANE sees only the loopback saned.

    The child gets its own config directory, with only the ``net`` backend and
    an empty ``net.conf``, and ``SANE_NET_HOSTS=127.0.0.1``.  Both are set
    explicitly: the session fixture's ``SANE_CONFIG_DIR`` (the ``test``
    backend) is in ``os.environ`` and would otherwise be inherited.

    Args:
        code: The Python source the child runs.
        tmp_path: The per-test temporary directory pytest removes afterwards.

    Returns:
        The finished child, with its stdout and stderr captured as text.

    """
    config_dir = tmp_path / "sane.d"
    config_dir.mkdir()
    (config_dir / "dll.conf").write_text("net\n")
    (config_dir / "net.conf").write_text("")
    env = {
        **os.environ,
        "SANE_CONFIG_DIR": str(config_dir),
        "SANE_NET_HOSTS": "127.0.0.1",
    }
    return subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=_SUBPROCESS_TIMEOUT_SECONDS,
    )


@pytest.fixture(scope="session", autouse=True)
def _sane_config_dir(sane_test_backend_config: None) -> None:
    """
    Give every test in this module the ``test``-only SANE configuration.

    The configuration itself is ``sane_test_backend_config`` in
    ``tests/conftest.py``, shared with the fake's contract table, whose
    docstring explains why it has to be session-scoped.  This wrapper only
    makes it apply to the whole module without each test naming it.

    Args:
        sane_test_backend_config: Requested for its effect on the environment.

    """


@pytest.mark.sane_hardware
class TestRealSaneTestBackend:
    """The real ``test`` device enumerates, reports its capabilities and scans."""

    def test_enumeration_lists_the_test_device(self) -> None:
        """
        The configured backend appears by name.

        The assertion is positive: with no real scanner in CI, an empty list and
        a missing test backend look identical, and "list is not empty" would pass
        with a half-broken fixture or a developer's own scanner leaked in.
        """
        names = [device.name for device in SaneBackend().get_devices()]
        assert "test:0" in names

    def test_capabilities_report_the_long_feeder_name(self) -> None:
        """The device reports the full ADF source string, not an abbreviation."""
        capabilities = SaneBackend().get_capabilities("test:0")
        assert "Automatic Document Feeder" in capabilities.sources

    def test_capabilities_report_the_resolution_range_the_device_gave(self) -> None:
        """
        The device's resolution range is reported as the range it gave.

        ``test:0`` constrains resolution with the range ``(1.0, 1200.0, 1.0)``,
        and real libsane, not a double, is what shows ``get_capabilities`` reads
        a range as well as a word list.

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

        The ``test`` backend's default picture is solid black, so a backend that
        discarded black pages would destroy all ten here.  The feeder is drained
        end to end against real libsane: the long ADF source name routes to the
        multi-page path, ten sheets are taken off it and spooled one at a time,
        and nothing is thrown away on the way out.

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
        # libsane, not against a double.
        assert [record.sequence for record in pages] == list(range(1, 11))
        assert all(record.path.exists() for record in pages)

    def test_a_lowercase_feeder_name_drains_the_feeder(self, tmp_path: Path) -> None:
        """
        The feeder's name in another case scans all ten sheets through it.

        libsane would take the lowercase name on its own, but saneless decides
        routing from the name it resolved, so the match has to be saneless's:
        refused, or matched to the platen, this reads ``0`` or ``1`` pages.

        Args:
            tmp_path: Where the ten pages are spooled.

        """
        settings = ScanSettings(
            source="automatic document feeder", resolution=75, mode="Gray"
        )
        pages = (
            SaneBackend().scan_pages("test:0", settings, _page_sink_for(tmp_path)).pages
        )
        assert len(pages) == 10

    def test_a_prefix_of_the_feeder_name_is_refused(self, tmp_path: Path) -> None:
        """
        'adf' is not taken as a prefix of the feeder, and nothing is spooled.

        ``test:0`` names its feeder "Automatic Document Feeder", which 'adf'
        is not a case-insensitive spelling of, and saneless does no prefix
        matching of its own.

        Args:
            tmp_path: Where any page would have been spooled.

        """
        settings = ScanSettings(source="adf", resolution=75, mode="Gray")
        with pytest.raises(ScanError) as excinfo:
            SaneBackend().scan_pages("test:0", settings, _page_sink_for(tmp_path))
        message = str(excinfo.value)
        assert "'adf'" in message
        assert "'Automatic Document Feeder'" in message
        assert not list((tmp_path / "spool").iterdir())

    def test_every_uniformly_black_page_survives_the_scanner_layer(
        self, tmp_path: Path
    ) -> None:
        """
        All ten pages come back, and every one of them is uniformly black.

        The ``test`` backend hands back solid black, so this shows the scanner
        layer does not judge a page by what is printed on it; whether a blank
        page is kept is a profile setting one layer up.  The record's measurement
        is what the pipeline's blank-page filter judges, and the read-back
        through ``images_of`` proves the spooled PNG really holds those pixels.

        Args:
            tmp_path: Where the ten pages are spooled.

        """
        settings = ScanSettings(
            source="Automatic Document Feeder", resolution=75, mode="Gray"
        )
        batch = SaneBackend().scan_pages("test:0", settings, _page_sink_for(tmp_path))
        assert len(batch.pages) == 10
        assert all(record.paper_white == 0 for record in batch.pages)
        assert all(
            image.convert("L").getextrema() == (0, 0) for image in images_of(batch)
        )

    def test_a_scan_reads_its_pages_in_a_scan_child_process(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A real scan returns its page from a child process the scan started.

        ``SaneBackend`` reads pages in a scan child, so a scan must start one,
        running the scan child's script; a scan made in this process instead
        starts none.

        Args:
            tmp_path: Where the page is spooled.
            monkeypatch: Records each child the launcher starts.

        """
        started: list[str] = []
        real_start = child_launch.start_child

        def recording_start(
            script: Path, configured_host: str
        ) -> subprocess.Popen[bytes]:
            started.append(script.name)
            return real_start(script, configured_host)

        monkeypatch.setattr(child_launch, "start_child", recording_start)
        settings = ScanSettings(source="Flatbed", resolution=75, mode="Gray")

        batch = SaneBackend().scan_pages("test:0", settings, _page_sink_for(tmp_path))

        assert [record.sequence for record in batch.pages] == [1]
        assert started == ["_scan_child.py"]


# What ``test:0`` reports for a 75 dpi gray frame over its default 80 x 100 mm
# scan area: 236 pixels by 295 lines, at 8 bits and at 16 alike.  python-sane
# hands back a 16-bit frame as an image twice as tall, 236 x 590.
_DEFAULT_AREA = (("tl_x", 0.0), ("tl_y", 0.0), ("br_x", 80.0), ("br_y", 100.0))
_EIGHT_BIT_PAGE = (236, 295)

# The option python-sane reaches through ``getattr``, as the protocol does not
# declare it.
_DEPTH = "depth"


@pytest.mark.sane_hardware
class TestRealSaneDepth:
    """
    A device left at 16 bits per sample scans at 8, and a 16-bit frame is refused.

    ``test:0`` keeps its ``depth`` from one handle to the next, so setting 16 on
    a raw handle and closing it leaves the device exactly as an earlier program
    might have.  ``test:0`` always offers 8, so a scan through ``scan_pages``
    never reaches the refusal; the refusal helper is called on a real handle
    instead, and the refusal through ``scan_pages`` is proven on the double.
    """

    @pytest.fixture
    def left_at_sixteen_bits(self) -> Iterator[None]:
        """
        Leave ``test:0`` at depth 16 over its default scan area, then put 8 back.

        Yields:
            None, while the device is at depth 16.

        """
        libsane_in_this_process()
        sane = scan_session_mod._ensure_sane()
        handle = sane.open("test:0")
        try:
            for name, value in _DEFAULT_AREA:
                setattr(handle, name, value)
            setattr(handle, _DEPTH, 16)
        finally:
            handle.close()
        try:
            yield
        finally:
            restorer = sane.open("test:0")
            try:
                setattr(restorer, _DEPTH, 8)
            finally:
                restorer.close()

    @pytest.mark.usefixtures("left_at_sixteen_bits")
    def test_a_device_left_at_sixteen_bits_scans_an_eight_bit_page(
        self, tmp_path: Path
    ) -> None:
        """
        The page is 236 x 295, never the double-height 236 x 590.

        Args:
            tmp_path: Where the page is spooled.

        """
        settings = ScanSettings(source="Flatbed", resolution=75, mode="Gray")

        batch = SaneBackend().scan_pages("test:0", settings, _page_sink_for(tmp_path))

        assert [image.size for image in images_of(batch)] == [_EIGHT_BIT_PAGE]

    @pytest.mark.usefixtures("left_at_sixteen_bits")
    def test_the_refusal_helper_refuses_a_real_sixteen_bit_frame(self) -> None:
        """
        The parameters a real 16-bit handle reports are refused.

        Source, mode and resolution are set here, not trusted: a freshly
        initialised ``test:0`` reads its resolution back as a fraction of a
        dpi, and reports a one-pixel frame.
        """
        handle = scan_session_mod._ensure_sane().open("test:0")
        try:
            handle.source = "Flatbed"
            handle.mode = "Gray"
            handle.resolution = 75
            parameters = scan_session_mod._read_parameters(handle, "test:0")

            assert parameters.depth == 16
            assert (parameters.pixels_per_line, parameters.lines) == _EIGHT_BIT_PAGE
            with pytest.raises(ScanError) as raised:
                scan_session_mod._refuse_sixteen_bit(parameters, "test:0")
            assert "test:0" in str(raised.value)
        finally:
            handle.close()


@pytest.mark.sane_hardware
class TestLostControlConnection:
    """
    A saned that drops the control connection crashes raw libsane only.

    libsane's net backend keeps one control connection per host open between
    listings.  Once the saned at the other end restarts, the next listing
    reads a reply that was never filled in, and the process dies; the fake
    saned here lists once and closes, which is exactly that.

    All three tests run their client in a subprocess, so a crash fails one
    test rather than the whole ``sane_hardware`` run.  They listen on port
    6566, the only port libsane dials, and fail naming it if it is taken.
    """

    def test_raw_libsane_dies_when_the_control_connection_drops(
        self, tmp_path: Path
    ) -> None:
        """
        The control: raw python-sane listing twice dies by a signal.

        This is what proves the fake reproduces the defect.  The crash reads
        a reply that was never filled in, so which signal ends the process
        depends on the libsane build -- SIGABRT, SIGBUS and SIGSEGV have all
        been measured -- and any signal counts.
        """
        with fake_saned(SanedBehaviour.LIST_THEN_DROP, port=_SANE_PORT):
            result = _run_against_loopback_saned(_RAW_LISTS_TWICE, tmp_path)

        assert result.returncode < 0, (
            "the fake saned did not reproduce the lost-connection crash: raw "
            f"python-sane exited {result.returncode} instead of dying by a "
            f"signal\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_raw_libsane_survives_when_the_connection_is_kept(
        self, tmp_path: Path
    ) -> None:
        """
        The negative control: the same code exits 0 when nothing drops.

        This is what proves the control can fail: its signal comes from the
        dropped connection, not from the fake's replies or from listing twice.
        """
        with fake_saned(SanedBehaviour.LIST_AND_KEEP, port=_SANE_PORT) as fake:
            result = _run_against_loopback_saned(_RAW_LISTS_TWICE, tmp_path)

        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == ["1", "1"]
        assert fake.listings == [1, 1]

    def test_saneless_survives_a_dropped_control_connection(
        self, tmp_path: Path
    ) -> None:
        """
        The fix path: ``SaneBackend.get_devices()`` twice returns both times.

        Each listing runs in a fresh child with its own libsane, so the
        second listing never meets the connection the first one's saned
        dropped.  The fake serving exactly two listings proves each listing
        dialled afresh and was dropped; without that, this test would also
        pass against a fake that never drops.
        """
        with fake_saned(SanedBehaviour.LIST_THEN_DROP, port=_SANE_PORT) as fake:
            result = _run_against_loopback_saned(_SANELESS_LISTS_TWICE, tmp_path)

        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == ["1", "1"]
        assert len(fake.listings) == 2
