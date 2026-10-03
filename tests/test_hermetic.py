"""
The suite runs sealed off from the developer's machine.

Every test runs in a fake home, its own working directory and a private temp
directory. A test that reached the real home could read the live config --
Paperless URL and token included -- and write job state beside the real one.
These tests check the isolation from inside an ordinary test, so a change to an
autouse fixture that loosens it fails here, not silently.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import httpx2
import pytest

from saneless.config import OutputConfig
from saneless.paperless import PaperlessClient
from saneless.web import app as web_app

if TYPE_CHECKING:
    from tests.conftest import SocketGuard

# Captured at import, before any fixture runs, so it is the real home.
_REAL_HOME = Path.home()
_TESTS = Path(__file__).resolve().parent
_REPOSITORY = _TESTS.parent

_XDG_BASES = ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME")

# Test data: the shared temp directory no test module may name.
_FIXED_TEMP_NAME = "saneless-test"

# The poisoned environment's names, each aimed at a documentation address,
# besides SSL_CERT_FILE, which names a bundle holding no certificate. Each
# one changes a verdict when the suite fails to clear it: the proxy carries
# every plain-http request the loopback clients make, and the session
# snapshot below sees the rest.
_POISONS = {
    "SANE_NET_HOSTS": "192.0.2.1",
    "saneless_paperless__url": "http://192.0.2.1:8000",
    "HTTP_PROXY": "http://192.0.2.1:3128",
}

# The child run's summary line when every selected test ran and passed.
_ALL_PASSED = re.compile(r"^(?P<passed>[0-9]+) passed in [0-9.]+s", re.MULTILINE)


def test_home_is_neither_the_real_home_nor_in_the_repository() -> None:
    """HOME is a throwaway directory, neither the real home nor the checkout."""
    home = Path(os.environ["HOME"]).resolve()
    assert home != _REAL_HOME.resolve()
    assert not home.is_relative_to(_REPOSITORY)
    assert Path.home().resolve() == home


@pytest.mark.parametrize("variable", _XDG_BASES)
def test_each_xdg_base_is_under_the_fake_home(variable: str) -> None:
    """Every XDG base directory resolves inside the fake HOME."""
    home = Path(os.environ["HOME"])
    assert Path(os.environ[variable]).is_relative_to(home)


def test_xdg_runtime_dir_is_unset() -> None:
    """The per-session runtime directory of the real user is not visible."""
    assert "XDG_RUNTIME_DIR" not in os.environ


def test_working_directory_is_the_tests_own(tmp_path: Path) -> None:
    """The working directory is tmp_path, so no ./saneless.toml is reachable."""
    assert Path.cwd() == tmp_path
    assert not (Path.cwd() / "saneless.toml").exists()


def test_no_saneless_variable_leaks_in() -> None:
    """No SANELESS_* setting from the developer's shell reaches a test."""
    leaked = sorted(key for key in os.environ if key.startswith("SANELESS_"))
    assert leaked == []


def test_playwright_browsers_path_is_pinned() -> None:
    """The browser tests still find Chromium once HOME is a fake directory."""
    assert os.environ.get("PLAYWRIGHT_BROWSERS_PATH")


def test_no_test_module_names_a_fixed_temp_path() -> None:
    """
    No test builds its settings on one shared, process-wide temp directory.

    This module names the forbidden directory once, in the constant it scans
    for, and nowhere else.
    """
    offenders = sorted(
        path.name
        for path in _TESTS.glob("*.py")
        if path.read_text(encoding="utf-8").count(_FIXED_TEMP_NAME)
        > (1 if path == Path(__file__).resolve() else 0)
    )
    assert offenders == []


def test_hermetic_temp_dir_is_a_fresh_private_directory() -> None:
    """``tempfile.tempdir`` is pinned to a new directory only this user can use."""
    fake = Path(tempfile.gettempdir())
    assert tempfile.tempdir == str(fake)
    assert fake.is_dir()
    assert list(fake.iterdir()) == []
    assert fake.stat().st_mode & 0o077 == 0


def test_hermetic_default_tmp_dir_is_under_the_fake_temp_dir() -> None:
    """The default scratch directory lands in the test's temp dir, not in /tmp."""
    fake = Path(tempfile.gettempdir())
    assert OutputConfig().tmp_dir == fake / f"saneless-{os.getuid()}"


def test_hermetic_import_of_config_touches_no_temp_dir() -> None:
    """
    Importing ``saneless.config`` does not resolve the temp directory.

    ``tempfile.gettempdir()`` probes the file system on its first call and
    caches the answer in ``tempfile.tempdir``, so a module that computes its
    default at import leaves it set. A fresh interpreter shows it still unset.
    """
    result = subprocess.run(
        ["/bin/sh", "-c", 'exec "$SANELESS_TEST_PYTHON" -c "$SANELESS_TEST_CODE"'],
        env={
            **os.environ,
            "SANELESS_TEST_PYTHON": sys.executable,
            "SANELESS_TEST_CODE": (
                "import tempfile, saneless.config; print(tempfile.tempdir)"
            ),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "None"


def _pending_connections(listener: socket.socket) -> int:
    """
    Count the connections waiting in a listener's backlog, accepting each.

    Returns:
        How many connections were waiting.

    """
    listener.settimeout(0.0)
    count = 0
    while True:
        try:
            accepted, _ = listener.accept()
        except BlockingIOError:
            return count
        accepted.close()
        count += 1


def _loopback_listener() -> socket.socket:
    """
    Bind and listen on an ephemeral loopback port.

    Returns:
        The listening socket; the caller closes it.

    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    return listener


def _probe_through_the_app_client(port: int) -> None:
    """Probe a loopback Paperless through the client class the web app builds."""
    client = web_app.PaperlessClient(url=f"http://127.0.0.1:{port}", token="t")
    try:
        client.test_connection(timeout=httpx2.Timeout(0.5))
    finally:
        client.close()


def test_the_socket_guard_refuses_a_port_nobody_bound(
    socket_guard: SocketGuard,
) -> None:
    """
    A connect to 127.0.0.1:8000 is refused and recorded, from any thread.

    A worker thread that swallows the refusal still leaves the record, which
    is what fails the test that made it.
    """
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", 8000), timeout=1)

    errors: list[BaseException] = []

    def connect_from_a_thread() -> None:
        try:
            socket.create_connection(("127.0.0.1", 8000), timeout=1)
        except OSError as error:
            errors.append(error)

    helper = threading.Thread(
        target=connect_from_a_thread, name="guard-helper", daemon=True
    )
    helper.start()
    helper.join(timeout=10)
    assert not helper.is_alive()
    assert [type(error) for error in errors] == [ConnectionRefusedError]

    recorded = [(thread, address) for _, thread, address in socket_guard.violations]
    assert recorded == [
        ("MainThread", ("127.0.0.1", 8000)),
        ("guard-helper", ("127.0.0.1", 8000)),
    ]
    socket_guard.clear()


def test_the_socket_guard_allows_a_loopback_port_the_test_bound(
    socket_guard: SocketGuard,
) -> None:
    """A loopback port this process bound accepts a connection."""
    listener = _loopback_listener()
    try:
        port = listener.getsockname()[1]
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        assert _pending_connections(listener) == 1
    finally:
        listener.close()
    assert socket_guard.violations == []


def test_the_socket_guard_refuses_a_non_loopback_address(
    socket_guard: SocketGuard,
) -> None:
    """A connect to a documentation address is refused before it is sent."""
    with pytest.raises(ConnectionRefusedError, match="test socket guard"):
        socket.create_connection(("192.0.2.1", 80), timeout=1)
    assert [address for _, _, address in socket_guard.violations] == [("192.0.2.1", 80)]
    socket_guard.clear()


def test_a_datagram_socket_on_8000_does_not_open_8000_to_tcp(
    socket_guard: SocketGuard,
) -> None:
    """
    A UDP socket bound to 127.0.0.1:8000 lets no TCP connect through.

    TCP and UDP ports are separate, so a real Paperless can listen on TCP
    8000 while this process holds UDP 8000.
    """
    datagram = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        datagram.bind(("127.0.0.1", 8000))
        with pytest.raises(ConnectionRefusedError, match="test socket guard"):
            socket.create_connection(("127.0.0.1", 8000), timeout=1)
    finally:
        datagram.close()
    assert [address for _, _, address in socket_guard.violations] == [
        ("127.0.0.1", 8000)
    ]
    socket_guard.clear()


def test_a_released_loopback_port_is_refused(socket_guard: SocketGuard) -> None:
    """
    A loopback port this process bound and then closed is refused.

    Once released, the port may belong to a real service, so having bound it
    earlier in the session permits nothing.
    """
    listener = _loopback_listener()
    port = listener.getsockname()[1]
    listener.close()
    with pytest.raises(ConnectionRefusedError, match="test socket guard"):
        socket.create_connection(("127.0.0.1", port), timeout=1)
    assert [address for _, _, address in socket_guard.violations] == [
        ("127.0.0.1", port)
    ]
    socket_guard.clear()


def test_paperless_requests_are_refused_by_default() -> None:
    """The web app's Paperless client sends nothing, even to a live listener."""
    assert web_app.PaperlessClient is not PaperlessClient
    listener = _loopback_listener()
    try:
        _probe_through_the_app_client(listener.getsockname()[1])
        assert _pending_connections(listener) == 0
    finally:
        listener.close()


@pytest.mark.real_paperless_transport
def test_the_marker_keeps_the_real_paperless_transport() -> None:
    """Under the opt-out marker the web app's client really connects."""
    assert web_app.PaperlessClient is PaperlessClient
    listener = _loopback_listener()
    try:
        _probe_through_the_app_client(listener.getsockname()[1])
        assert _pending_connections(listener) >= 1
    finally:
        listener.close()


@pytest.fixture(scope="session")
def environment_before_any_test_body() -> frozenset[str]:
    """
    Record the environment's names as session-scoped fixtures see it.

    A session fixture runs before any function-scoped fixture clears a
    variable for one test, so it sees only what the session-wide clearing
    left.

    Returns:
        The names present at the time.

    """
    return frozenset(os.environ)


def test_session_fixtures_see_none_of_the_poisons(
    environment_before_any_test_body: frozenset[str],
) -> None:
    """
    No poisoned variable is visible to a session-scoped fixture.

    The browser suite's server starts at session scope, before any per-test
    fixture runs, so the session-wide clearing is all that keeps an exported
    variable away from it.
    """
    poisons = {"SSL_CERT_FILE", *_POISONS}
    assert not poisons & environment_before_any_test_body


def test_a_poisoned_environment_changes_no_verdict(tmp_path: Path) -> None:
    """
    Config, TLS, SANE host, Paperless and web scan tests pass when poisoned.

    The child run exports a CA bundle that holds no certificate, a SANE host
    list, a Paperless URL in the lower-case spelling settings accept, and a
    plain-http proxy, all aimed at a documentation address. The suite clears
    them before any test runs, so every selected test runs and passes; a
    skipped, failed or missing test fails this check.
    """
    bundle = tmp_path / "bogus-ca.pem"
    bundle.write_text("this is not a certificate\n", encoding="utf-8")
    result = subprocess.run(
        [
            "/bin/sh",
            "-c",
            'exec "$SANELESS_TEST_PYTHON" -m pytest -p no:cacheprovider -q'
            " tests/test_hermetic.py::test_session_fixtures_see_none_of_the_poisons"
            " tests/test_config.py::TestConfigSources"
            " tests/test_config.py::TestPaperlessTokenAndUrlAtLoad"
            " tests/test_paperless.py::TestLoopbackClientSideProtocolErrors"
            " tests/test_paperless.py::TestProbeConnection"
            " tests/test_net_hosts.py"
            " tests/test_checks.py::TestScannerCheck"
            " tests/test_cli.py::TestServeCommand"
            " tests/test_web.py::TestOwnerCookie"
            " tests/test_golden_e2e.py::test_web_polls_once_and_stores_the_outcome",
        ],
        cwd=_REPOSITORY,
        env={
            **os.environ,
            "SANELESS_TEST_PYTHON": sys.executable,
            "SSL_CERT_FILE": str(bundle),
            **_POISONS,
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    shown = result.stdout[-4000:] + result.stderr[-2000:]
    assert result.returncode == 0, shown
    assert _ALL_PASSED.search(result.stdout), shown
