"""Shared test fixtures and helpers for all test modules."""

from __future__ import annotations

import errno
import functools
import html
import ipaddress
import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import httpx2
import pytest
from PIL import Image, ImageDraw

from saneless import cli as cli_mod
from saneless import config as config_mod
from saneless import logging_config
from saneless import worker as worker_mod
from saneless.config import (
    OutputConfig,
    PaperlessConfig,
    ProfileConfig,
    ScannerConfig,
    Settings,
)
from saneless.paperless import ApiDelivery, PaperlessClient, PaperlessTiming, TaskFiled
from saneless.pipeline import FlipCoordinator
from saneless.scanner import _listing_child
from saneless.scanner import listing as listing_mod
from saneless.scanner import sane_backend as sane_backend_mod
from saneless.scanner.base import DeviceCapabilities, ScanBatch, ScannerBackend
from saneless.scanner.listing import ListingReply
from saneless.sigpipe import block_sigpipe
from saneless.thread_unwinder import load_thread_unwinder
from saneless.vocabulary import FlipOutcome
from tests.fake_clock import FakeClock

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from fastapi import FastAPI
    from fastapi.routing import _IncludedRouter
    from fastapi.testclient import TestClient
    from playwright.sync_api import Page
    from starlette.routing import BaseRoute

    from saneless.job import Job, JobStore
    from saneless.scanner.base import (
        DeviceInfo,
        PageRecord,
        PageSink,
        PassCapReached,
        ScanSettings,
    )
    from saneless.scanner.listing import ListingRequest
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

# The backend's real listing launcher, captured before any test replaces it,
# so the tests that need a real child process can put it back.  Looked up
# through the module's namespace because the seam is patched in whether or not
# the backend defines it yet.
_REAL_LAUNCH_LISTING = sane_backend_mod.__dict__.get("_launch_listing")

_NO_REAL_LIBSANE = (
    "the default suite must not start real libsane: patch sane_backend.sane "
    "with a FakeSaneModule, or request real_listing_launcher with a stand-in "
    "child"
)


# The search for config files, captured before any test replaces it, so a test
# about the real candidates can still call it.
real_config_search_paths = config_mod.config_search_paths

# Ports where a real service usually listens on a developer's machine: a local
# Paperless, and saned. A test may never allow them for a child process's
# server; a connect there goes ahead only while this process holds a stream
# socket bound to that very address, as for every other loopback port.
_GUARD_REFUSED_PORTS = frozenset({8000, 6566})

_INET_FAMILIES = (socket.AF_INET, socket.AF_INET6)

# The owner recorded for a refusal made outside any test.
_OUTSIDE_A_TEST = "<session>"

# Environment variables cleared for the whole session, in both cases, besides
# every SANE_* and SANELESS_* variable.
_AMBIENT_VARIABLES = (
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
)


def _is_loopback(host: object) -> bool:
    """
    Tell whether a connect address names this machine.

    Args:
        host: The host part of the address, as the caller passed it.

    Returns:
        Whether it is ``localhost`` or a loopback IP literal.

    """
    if not isinstance(host, str):
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.partition("%")[0]).is_loopback
    except ValueError:
        return False


# The wildcard address of each internet family.
_WILDCARD = {socket.AF_INET: "0.0.0.0", socket.AF_INET6: "::"}


def _is_bound_to(sock: socket.socket, address: tuple[object, object]) -> bool:
    """
    Tell whether a socket still holds ``address``.

    It does when it is bound to exactly that address, or when it listens on
    its family's wildcard address at that port: the kernel lets no other
    socket bind a specific address under a listening wildcard one, so no
    other process can be behind it.

    Returns:
        False for a socket closed since it was bound, even mid-check.

    """
    host, port = address
    try:
        bound_host, bound_port = sock.getsockname()[:2]
        if bound_port != port:
            return False
        if bound_host == host:
            return True
        family = ipaddress.ip_address(str(host).partition("%")[0]).version
        return (
            bound_host == _WILDCARD[sock.family]
            and family == (4 if sock.family == socket.AF_INET else 6)
            and sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN) == 1
        )
    # Two clauses rather than one tuple: ruff format rewrites a tuple into
    # PEP 758's bracketless form, which the debug-statements hook cannot parse.
    except OSError:
        return False
    except ValueError:
        return False


class _SocketGuardState:
    """
    What the socket guard knows, shared by every thread in the process.

    Attributes:
        held: Every internet stream socket this process bound, held weakly;
            a socket closed since, or collected, permits nothing.
        allowed: Ports a test declared for a server in a child process.
        refusals: ``(owner, thread name, address)`` for each refused connect,
            where owner is the node id of the test running at the time.
        owner: The node id of the test running now, or ``<session>``.

    """

    def __init__(self) -> None:
        """Start knowing no ports and holding no refusals."""
        self.lock = threading.Lock()
        self.held: weakref.WeakSet[socket.socket] = weakref.WeakSet()
        self.allowed: set[int] = set()
        self.refusals: list[tuple[str, str, tuple[object, ...]]] = []
        self.owner = _OUTSIDE_A_TEST

    def permits(self, host: object, port: object) -> bool:
        """
        Tell whether a connect to ``host:port`` may go ahead.

        Returns:
            Whether the host is loopback and either this process holds a
            stream socket bound to that address right now (exactly, or as a
            listening wildcard of its family), or a test allowed the port.

        """
        if not _is_loopback(host):
            return False
        with self.lock:
            held = list(self.held)
            allowed = port in self.allowed
        return allowed or any(_is_bound_to(sock, (host, port)) for sock in held)

    def audit(self, event: str, args: tuple[Any, ...]) -> None:
        """
        Refuse and record a ``socket.connect`` the guard does not permit.

        Args:
            event: The audit event's name.
            args: The event's arguments: the socket and the address.

        Raises:
            ConnectionRefusedError: For an internet connect not permitted.

        """
        if event != "socket.connect":
            return
        sock, address = args
        if sock.family not in _INET_FAMILIES or not isinstance(address, tuple):
            return
        if sock.type == socket.SOCK_DGRAM:
            # Connecting a datagram socket only picks the route; nothing is sent.
            return
        if self.permits(address[0], address[1]):
            return
        with self.lock:
            self.refusals.append((self.owner, threading.current_thread().name, address))
        raise ConnectionRefusedError(
            errno.ECONNREFUSED, "refused by the test socket guard"
        )

    def take(self, owner: str) -> list[tuple[str, str, tuple[object, ...]]]:
        """
        Remove and return the refusals recorded for ``owner``.

        Returns:
            Those refusals, oldest first.

        """
        with self.lock:
            taken = [entry for entry in self.refusals if entry[0] == owner]
            self.refusals[:] = [e for e in self.refusals if e[0] != owner]
        return taken


_SOCKET_GUARD: dict[str, _SocketGuardState] = {}


def _install_socket_guard() -> _SocketGuardState:
    """
    Install the process-wide socket guard once, and return its state.

    One audit hook refuses every internet ``connect`` except to a loopback
    address where this process holds a bound stream socket at that moment,
    or to a port a test allowed for a child process. Ports 8000 and 6566 can
    never be allowed, so a local Paperless or saned is never reached. A
    datagram socket on the same port proves nothing, because TCP and UDP
    ports are separate, and a port an earlier test bound and released may
    since belong to a real service, so neither permits a connect. The guard
    sees connects from every thread, httpx and asyncio included, and records
    each refusal, so a worker thread that swallows the error still fails the
    test. Bound sockets are learnt by wrapping ``socket.socket.bind``: the
    bind audit event fires before the bind, with port 0 for an ephemeral
    port.

    Audit hooks cannot be removed, so a second call returns the first
    installation rather than stacking another hook and another wrapper. It
    starts no thread.

    C code raises no audit event, so libsane's own sockets -- its ``net``
    backend dialling saned -- are invisible to it; only Python's connects
    are guarded. Datagram sockets are let through: connecting one sends
    nothing, and the suite uses it only to ask which local address routes out.

    Returns:
        The guard's shared state.

    """
    installed = _SOCKET_GUARD.get("state")
    if installed is not None:
        return installed
    state = _SocketGuardState()
    real_bind = socket.socket.bind

    def recording_bind(
        sock: socket.socket, address: tuple[object, ...] | str | bytes, /
    ) -> None:
        real_bind(sock, address)
        if sock.family in _INET_FAMILIES and sock.type == socket.SOCK_STREAM:
            with state.lock:
                state.held.add(sock)

    # Never undone: the wrapper lasts as long as the hook it feeds.
    pytest.MonkeyPatch().setattr(socket.socket, "bind", recording_bind)
    sys.addaudithook(state.audit)
    _SOCKET_GUARD["state"] = state
    return state


_GUARD_STATE = _install_socket_guard()


class SocketGuard:
    """
    One test's handle on the socket guard.

    Request it as the ``socket_guard`` fixture.
    """

    def __init__(self, state: _SocketGuardState, owner: str) -> None:
        """Bind the handle to the guard's state and the test's node id."""
        self._state = state
        self._owner = owner
        self._allowed: set[int] = set()

    @property
    def violations(self) -> list[tuple[str, str, tuple[object, ...]]]:
        """The test's refused connects so far, as ``(owner, thread, address)``."""
        with self._state.lock:
            return [e for e in self._state.refusals if e[0] == self._owner]

    def clear(self) -> None:
        """Forget the test's refusals, once it has asserted on them."""
        self._state.take(self._owner)

    def allow_port(self, port: int) -> None:
        """
        Let the test connect to a loopback port this process does not hold.

        For a server in a child process, or a port the test closed on purpose
        so that the kernel, not the guard, refuses the connect.

        Args:
            port: The port, allowed until the test ends.

        """
        if port in _GUARD_REFUSED_PORTS:
            msg = f"port {port} is never allowed"
            raise ValueError(msg)
        with self._state.lock:
            self._state.allowed.add(port)
        self._allowed.add(port)

    def release(self) -> None:
        """Withdraw every port this handle allowed."""
        with self._state.lock:
            self._state.allowed -= self._allowed
        self._allowed.clear()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    """Charge connects made from now on to the test about to be set up."""
    _GUARD_STATE.owner = item.nodeid


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Fail the run for any refused connect no test was failed for."""
    with _GUARD_STATE.lock:
        left = list(_GUARD_STATE.refusals)
    if not left:
        return
    lines = "\n".join(
        f"  {owner} [{thread}] {address}" for owner, thread, address in left
    )
    sys.stderr.write(f"\nthe test socket guard refused these connects:\n{lines}\n")
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True)
def _socket_guard_verdict(request: pytest.FixtureRequest) -> Iterator[None]:
    """
    Fail a test that made a connect the socket guard refused.

    The refusal is raised in the connecting thread too, but a worker thread
    may swallow it, so the record is what decides.

    Yields:
        Nothing; the check runs at teardown.

    """
    yield
    refused = _GUARD_STATE.take(request.node.nodeid)
    if refused:
        shown = ", ".join(f"{address} from {thread}" for _, thread, address in refused)
        pytest.fail(f"the test socket guard refused: {shown}")


@pytest.fixture
def socket_guard(request: pytest.FixtureRequest) -> Iterator[SocketGuard]:
    """
    Give the test a handle on the socket guard.

    Yields:
        The handle; ports it allowed are withdrawn after the test.

    """
    guard = SocketGuard(_GUARD_STATE, request.node.nodeid)
    try:
        yield guard
    finally:
        guard.release()


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add ``--mutants``, which runs the hand-written mutant checks."""
    parser.addoption(
        "--mutants",
        action="store_true",
        default=False,
        help=(
            "run the tests marked mutant, which re-run named tests against "
            "mutated copies of the repository"
        ),
    )


def pytest_configure() -> None:
    """
    Block SIGPIPE for the whole run, the way ``saneless.main()`` runs.

    It also loads the C library's thread unwinder, the next thing
    ``saneless.main()`` does, for the reason given at the call below.

    The suite drives libsane in this very process, as the shipped program
    does, and libsane puts SIGPIPE back to its default action after a read
    that ends with an error status.  ``saneless.main()`` blocks the signal
    before anything else, but pytest never calls it, so without this a later
    test's write to a closed socket would kill the whole run.

    A hook rather than a session fixture, so it runs before any test starts:
    the mask belongs to a thread and is copied to the threads it starts, and
    pytest-timeout's thread method starts a timer thread for each test before
    that test's fixtures are set up.  The check below makes a late call fail
    loudly instead of leaving some threads unprotected.
    """
    assert threading.current_thread() is threading.main_thread()
    assert threading.active_count() == 1, threading.enumerate()
    block_sigpipe()
    # And the unwinder is loaded up front, as ``saneless.main()`` loads it
    # next: otherwise the first libsane reader thread to end loads it, and a
    # cancel landing in that load leaves the loader's lock held, which hangs
    # the run in its exit handlers after the last test has passed.
    load_thread_unwinder()


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """
    Deselect every ``mutant`` test unless ``--mutants`` was given.

    Deselecting rather than skipping keeps them out of the summary, and an
    ``-m`` expression on the command line cannot bring them back by accident.
    """
    if config.getoption("--mutants"):
        return
    kept = [item for item in items if item.get_closest_marker("mutant") is None]
    if len(kept) != len(items):
        dropped = [item for item in items if item.get_closest_marker("mutant")]
        config.hook.pytest_deselected(items=dropped)
        items[:] = kept


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
    Fail the run if the suite creates or changes ``./saneless.toml``.

    Nothing in the suite may write a config file into the working directory,
    and the file is gitignored, so ``git status`` after a run cannot show a
    stray copy.  The path is fixed when the session starts, before any test
    changes the working directory.

    Yields:
        Nothing; the check runs after the last test.

    """
    path = Path.cwd() / "saneless.toml"
    before = _config_file_stamp(path)
    yield
    if _config_file_stamp(path) != before:
        pytest.fail(f"the test suite created or modified {path}")


@pytest.fixture(autouse=True, scope="session")
def _no_ambient_environment_for_the_session() -> Iterator[None]:
    """
    Keep the developer's environment away from the whole suite.

    Removed before any other session fixture runs: every ``SANE_*`` and every
    ``SANELESS_*`` variable in any case, the CA bundle variables, and the
    proxy variables in both cases. Each one changes a verdict when exported:
    a bogus ``SSL_CERT_FILE`` breaks every client built with TLS, a
    lower-case ``saneless_paperless__url`` is still a setting, and a
    non-empty ``SANE_NET_HOSTS`` makes the Scanner check dial the machines it
    names. ``sane_test_backend_config`` sets ``SANE_CONFIG_DIR`` afterwards.

    Session-scoped as well as per test, because a per-test fixture cannot
    reach a session-scoped server: the browser suite's server starts before
    any function-scoped fixture runs. Everything is restored when the session
    ends.

    Yields:
        Nothing; the variables are restored after the last test.

    """
    ambient = {name.upper() for name in _AMBIENT_VARIABLES}
    with pytest.MonkeyPatch.context() as session_patch:
        for key in list(os.environ):
            upper = key.upper()
            if upper in ambient or upper.startswith(("SANE_", "SANELESS_")):
                session_patch.delenv(key, raising=False)
        yield


@pytest.fixture(autouse=True)
def _no_ambient_sane_net_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Start every test with ``SANE_NET_HOSTS`` unset.

    The session fixture above clears the developer's value once; this one
    also keeps a value an earlier test left behind -- set directly, or
    written by the scanner backend during SANE initialisation -- out of the
    next test's body.  It sets the variable before deleting it, so the undo
    removes a value the test's own code creates.  A test that wants the
    variable set says so with ``monkeypatch.setenv`` in its own body.

    Args:
        monkeypatch: pytest's environment patcher.

    """
    monkeypatch.setenv("SANE_NET_HOSTS", "unset")
    monkeypatch.delenv("SANE_NET_HOSTS")


@pytest.fixture(scope="session")
def sane_test_backend_config(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[None]:
    """
    Point SANE at a ``dll.conf`` naming only the ``test`` backend.

    Requested by every test that drives real libsane: the hardware module
    requests it for all of its tests, and the contract table requests it for
    its libsane rows only.  It is not autouse, because a test that never
    touches libsane has no reason to carry the variable.

    Session-scoped deliberately, and this is not a style choice.
    ``SANE_CONFIG_DIR`` is honoured only before the first ``sane.init()`` in a
    process, ``sane.exit()`` followed by a re-init does *not* reset it, and
    backends accumulate across re-inits so the developer's real scanner never
    leaves the device list.  A function-scoped fixture calling ``setenv``
    therefore does not work.  ``pytest.MonkeyPatch.context()`` is used here because the
    function-scoped fixture of that name is unavailable at session scope, and
    the context manager guarantees the variable is unset at session end.

    Only ``dll.conf`` is written; no ``test.conf`` is copied.  The backend's
    compiled-in defaults already give two devices and a ten-sheet feeder.
    Naming only ``test`` also keeps the ``net`` backend out, so no network
    scanner is dialled while it is in force.

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


def reset_sane_process_state() -> None:
    """
    Return SANE to "never initialised, not wedged" for the next test.

    ``_INIT`` and ``_WEDGE`` are process-global by design: the
    first ``SaneBackend`` built in a process initialises SANE and every later
    one deliberately does not, and a read that never came back refuses the
    next scan on a *different* backend object.  Both are exactly the kind of
    state a test cannot be trusted to leave behind, so the suite resets them
    around every test rather than asking each module to remember.

    The reset goes through the public ``shutdown()`` and not into the guard's
    own fields, because "after a shutdown a later init is allowed" is the
    behaviour the backend promises; reaching past it would let that promise
    rot while the tests kept passing.

    ``shutdown()`` has one documented refusal: it leaves the guard armed when a
    read is still recorded as outstanding, because ``sane_exit()`` closes every
    open handle and SANE forbids that while an operation is in flight.  A test
    that wedged the backend and did not release it would therefore strand
    ``_INIT.done`` at ``True`` -- the very leak this helper exists to stop --
    so that one case is finished off by hand, and pointedly *without* calling
    ``sane_exit()``, which would be unsafe for the same reason ``shutdown()``
    declined to.

    The open-handle record is cleared on every path, because a test that wedged
    a handle and did not release it leaves that handle counted, which would
    refuse every later test's ``reinitialise()``.  Its cancel record and any
    iterator parked on it go too: a parked iterator would otherwise outlive
    its test, and a recorded cancel could match a later handle given the same
    ``id()``.
    """
    sane_backend_mod.shutdown()
    handles = sane_backend_mod._OPEN_HANDLES
    handles.handles.clear()
    handles.cancelled.clear()
    handles.parked.clear()
    if not sane_backend_mod._INIT.done:
        return
    record = sane_backend_mod._WEDGE
    record.stuck = False
    record.done = None
    record.device = None
    record.device_id = ""
    record.page_label = ""
    record.settling = False
    record.outstanding = set()
    sane_backend_mod._restore_sane_net_hosts()
    sane_backend_mod._INIT.done = False
    sane_backend_mod._INIT.host = ""
    sane_backend_mod._INIT.effective = ""
    sane_backend_mod._INIT.version = None


@pytest.fixture(autouse=True)
def sane_process_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """
    Give every test in the suite an uninitialised, unwedged SANE.

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


class ListingSeam:
    """
    What the in-process listing seam was asked for, in order.

    Attributes:
        calls: One ``(request, configured_host)`` pair per listing, as the
            backend passed them to its launcher.
        aborts: The abort Event each of those listings was given, or
            ``None`` for one given none, in the same order.

    """

    def __init__(self) -> None:
        """Start with no listings recorded."""
        self.calls: list[tuple[ListingRequest, str]] = []
        self.aborts: list[threading.Event | None] = []


@pytest.fixture(autouse=True)
def listing_seam(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> ListingSeam:
    """
    Run every scanner listing in this process, over the patched fake module.

    The backend lists scanners in a child process, and a child cannot see
    ``monkeypatch.setattr(sane_backend, "sane", FakeSaneModule())``: it
    imports the real python-sane and asks the real libsane.  Every test that
    drives a real ``SaneBackend`` over the fake -- directly, or through the
    app, the worker, the CLI or the health checks -- would otherwise start
    real libsane, and see its devices rather than the fake's.

    So the backend's launcher is replaced here, suite-wide and not per module
    for the reason ``sane_process_state`` gives: a module that forgot would
    list through real libsane without anyone noticing.  The replacement runs
    the child's own ``respond()`` over whatever is patched into
    ``sane_backend.sane``, and decodes the result with the launcher's own
    decoder, so the child's logic and the reply schema are still what a test
    exercises.  With nothing patched it fails the test instead of listing.

    A test that needs a real child process requests ``real_listing_launcher``,
    which puts the real launcher back and points it at a stand-in script, so
    the real child, and real libsane, still cannot run by accident.  Tests
    marked ``sane_hardware`` exist to drive real libsane, and are left alone.

    Args:
        monkeypatch: Undoes the replacement after the test.
        request: The test's request, to read its markers.

    Returns:
        The record of every listing the seam served.

    """
    seam = ListingSeam()
    if request.node.get_closest_marker("sane_hardware") is not None:
        return seam

    def launch_in_process(
        listing_request: ListingRequest,
        *,
        configured_host: str,
        abort: threading.Event | None = None,
    ) -> ListingReply:
        seam.calls.append((listing_request, configured_host))
        seam.aborts.append(abort)
        module = sane_backend_mod.sane
        if module is None:
            raise AssertionError(_NO_REAL_LIBSANE)
        reply = _listing_child.respond(
            {"open": listing_request.open, "alarm": 0}, module
        )
        return ListingReply.from_stdout((json.dumps(reply) + "\n").encode())

    monkeypatch.setattr(
        sane_backend_mod, "_launch_listing", launch_in_process, raising=False
    )
    return seam


@pytest.fixture
def real_listing_launcher(
    listing_seam: ListingSeam, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    Give the test the backend's real launcher, running a harmless stand-in.

    The launcher then starts a real child process, but the script it runs is
    one that exits with status 3 at once, which the launcher reports as no
    answer.  A test replaces it with its own script through
    ``stand_in_listing_child``.  The real child script is never the default,
    because it imports python-sane and would list through real libsane.

    Args:
        listing_seam: Requested so its replacement is in place to be undone.
        monkeypatch: Restores the seam and the child script after the test.
        tmp_path: Where the stand-in script is written.

    """
    _ = listing_seam  # ordering only: the seam must be patched before undoing it
    if _REAL_LAUNCH_LISTING is None:
        pytest.fail(
            "sane_backend has no _launch_listing to restore, so no test can "
            "run a real listing child"
        )
    monkeypatch.setattr(sane_backend_mod, "_launch_listing", _REAL_LAUNCH_LISTING)
    stand_in = tmp_path / "listing_child_exits.py"
    stand_in.write_text("import sys\n\nsys.exit(3)\n")
    monkeypatch.setattr(listing_mod, "_CHILD_FILE", stand_in)


@pytest.fixture
def stand_in_listing_child(
    real_listing_launcher: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Callable[[str], Path]:
    """
    Return a function that makes the real launcher run a script of the test's.

    Args:
        real_listing_launcher: Puts the real launcher back first.
        monkeypatch: Restores the child script after the test.
        tmp_path: Where the script is written.

    Returns:
        A function taking the script's source and returning its path, after
        pointing the launcher at it.

    """
    _ = real_listing_launcher  # the real launcher, not the seam, runs the script

    def use(source: str) -> Path:
        script = tmp_path / "listing_child_stand_in.py"
        script.write_text(source)
        monkeypatch.setattr(listing_mod, "_CHILD_FILE", script)
        return script

    return use


@pytest.fixture(autouse=True)
def library_logger_levels() -> Iterator[None]:
    """
    Return the HTTP library loggers to NOTSET after every test.

    ``configure_logging`` sets a level on these shared library loggers, and a
    few tests outside ``test_logging.py`` run the real function. A level left
    behind changes what a later test's ``caplog`` or log file captures from
    httpx2 and friends, so it is reset for the whole suite, not per module.

    Yields:
        Nothing; the reset runs after the test.

    """
    yield
    for name in logging_config._LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)


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
    ``Settings()`` would read ``~/.config/saneless/saneless.toml`` -- live URL
    and token included -- and a ``JobStore`` would write under
    ``~/.local/state``.
    The working directory matters the same way, because ``./saneless.toml`` is
    the first config search path and a developer's checkout usually has one.

    HOME and the XDG variables are set, not ``Path.home()`` patched, so
    ``os.path.expanduser``, every library and any subprocess see the fake home
    too. The fake home comes from ``tmp_path_factory`` rather than from inside
    ``tmp_path``, because some tests assert exactly what ``tmp_path`` holds.

    A test about the unset-XDG fallback deletes the variable itself.

    The system config file, ``/etc/saneless/saneless.toml``, is replaced in
    the config search by a path in an empty directory, in every module that
    imports the search by name. ``SANELESS_*`` variables are removed in any
    case, because settings read them in any case.

    The temp directory is faked too, by pinning ``tempfile.tempdir``: the
    default ``output.tmp_dir`` is computed from ``tempfile.gettempdir()`` when
    a ``Settings`` is built, so without this any test that builds default
    settings and starts the app or a scan would create the real
    ``/tmp/saneless-<uid>`` on the developer's machine. pytest's own temp
    directories are already decided by then, so they are unaffected.

    Args:
        tmp_path: The test's own directory, which becomes the working directory.
        tmp_path_factory: Source of a fresh fake home and temp directory.
        monkeypatch: Undoes every change after the test.

    """
    for key in list(os.environ):
        if key.upper().startswith("SANELESS_"):
            monkeypatch.delenv(key, raising=False)
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    for variable, parts in _XDG_BASES:
        monkeypatch.setenv(variable, str(home.joinpath(*parts)))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", _BROWSERS)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path_factory.mktemp("tmp")))
    system_config = tmp_path_factory.mktemp("etc-saneless") / "saneless.toml"

    def search_paths_without_the_system_file() -> tuple[Path, ...]:
        working, user, _ = real_config_search_paths()
        return working, user, system_config

    for module in (config_mod, cli_mod, worker_mod):
        monkeypatch.setattr(
            module, "config_search_paths", search_paths_without_the_system_file
        )


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
        "paperless": PaperlessConfig(url="http://paperless.invalid", token=auth),
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


def _refuse_every_request(request: httpx2.Request) -> httpx2.Response:
    """
    Fail a Paperless request the way an unreachable server does.

    Args:
        request: The request the client tried to send.

    Raises:
        httpx2.ConnectError: Always, as a refused connection would.

    """
    msg = "paperless unreachable in tests"
    raise httpx2.ConnectError(msg, request=request)


def refusing_paperless_client(clock: FakeClock) -> Callable[..., PaperlessClient]:
    """
    Return a builder for Paperless clients whose every request is refused.

    The builder takes the arguments ``create_app`` passes to
    ``PaperlessClient`` and returns a real client with only its transport
    replaced: every request fails with the ``ConnectError`` a refused
    connection raises, and no socket is opened. Its retry and before-send
    waits run on ``clock``, so they are recorded instead of slept.

    Import it as ``from tests.conftest import refusing_paperless_client``.

    Args:
        clock: The fake clock the clients wait on.

    Returns:
        A drop-in replacement for the ``PaperlessClient`` class.

    """

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
            transport=httpx2.MockTransport(_refuse_every_request),
            timing=PaperlessTiming(clock=clock.now, sleep=clock.sleep),
        )

    return build_client


@pytest.fixture(autouse=True)
def offline_paperless(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> list[float]:
    """
    Give the web app a Paperless client that never leaves the process.

    Every test gets it: a test that submits a scan would otherwise have its
    worker connect to the configured Paperless and, on a developer machine
    running one, deliver with the test token. The client is the one
    ``refusing_paperless_client`` builds, so its waits run on a ``FakeClock``
    and the upload's before-send budget is spent at once. Tests that replace
    ``app.state.paperless`` methods are unaffected.

    A test that needs the real HTTP transport -- to exercise TLS set-up, say
    -- is marked ``real_paperless_transport`` and is left alone.

    Args:
        monkeypatch: Undoes the patch after the test.
        request: The test's request, to read its markers.

    Returns:
        The backoff delays the client asked for, in order; empty for a test
        that keeps the real transport.

    """
    clock = FakeClock()
    if request.node.get_closest_marker("real_paperless_transport") is not None:
        return clock.waits
    monkeypatch.setattr(
        "saneless.web.app.PaperlessClient", refusing_paperless_client(clock)
    )
    return clock.waits


def _inked_page() -> Image.Image:
    """
    Return a 100x100 page with a black square on it.

    Inked rather than blank, because a blank page is what
    ``pipeline._drop_blank_pages`` exists to remove: a stub handing back a
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
    substituted_source: str | None = None,
    cap_reached: PassCapReached | None = None,
) -> ScanBatch:
    """
    Build a ScanBatch for a stubbed scanner.

    ``scan_pages`` returns a record rather than yielding, so a stub handing back
    ``iter([...])`` does not model the backend at all -- and because a
    ``MagicMock`` will return whatever it is given, that mismatch surfaces as a
    confusing failure deep in the pipeline rather than at the stub.

    ``pages`` is the ordered ``PageRecord`` tuple a sink produced, not a list of
    images. A test that only needs "a scan happened" should not hand-build
    records for this: ``spooling`` runs real pages through the caller's own sink
    and the records come back from there, so the spooled files behind them
    actually exist.

    The extra facts default to "nothing surprising happened": the device
    honoured the resolution it was asked for, rejected no sheets, scanned
    from the source the profile named, and ended before any per-pass cap.
    A test that cares about any of them passes it explicitly.

    Args:
        pages: The records the stubbed scan produced, in document order.
        resolution: The resolution the device reports having actually used.
        rejected: How many fed sheets failed their integrity checks.
        substituted_source: The requested source the device's Auto stood in
            for on the feeder path, or None.
        cap_reached: The per-pass cap the stubbed scan reached, and the sheet
            it fed but did not keep, or None.

    Returns:
        A ScanBatch carrying those records and every fact.

    """
    return ScanBatch(
        pages=tuple(pages),
        actual_resolution=resolution,
        pages_rejected=rejected,
        substituted_source=substituted_source,
        cap_reached=cap_reached,
    )


def spooling(
    pages: Sequence[Image.Image],
    *,
    resolution: int = 300,
    rejected: int = 0,
    substituted_source: str | None = None,
    cap_reached: PassCapReached | None = None,
) -> Callable[[str, ScanSettings, PageSink], ScanBatch]:
    """
    Build the ``side_effect`` a stubbed ``scan_pages`` needs.

    A factory returning a callable, rather than a ready-made batch for
    ``return_value``, because the sink does not exist until the call happens:
    it is the pipeline's, created inside the per-job workspace, and the records
    in the batch are whatever *that* sink gives back. A batch built in advance
    could only carry records the pipeline never made, pointing at files that
    were never written.

    The same argument ``scan_batch`` records still applies to the shape of the
    stub itself: a ``MagicMock`` returns whatever it is given, so a stub that
    does not model the backend surfaces as a confusing failure deep in the
    pipeline rather than at the stub. Modelling the backend means taking
    the sink and filling it.

    Args:
        pages: The pages the stubbed scan hands to the sink, in order.
        resolution: The resolution the device reports having actually used.
        rejected: How many fed sheets failed their integrity checks.
        substituted_source: The requested source the device's Auto stood in
            for on the feeder path, or None.
        cap_reached: The per-pass cap the stubbed scan reached, and the sheet
            it fed but did not keep, or None.

    Returns:
        One callable with ``scan_pages``' own ``(device_id, settings, sink)``
        shape, ready to assign to ``MagicMock.side_effect``.

    """

    def _spool_pages(
        device_id: str, settings: ScanSettings, sink: PageSink
    ) -> ScanBatch:
        """Spool every page into the sink the caller supplied."""
        records = [sink.add(page, dpi=resolution) for page in pages]
        return scan_batch(
            records,
            resolution=resolution,
            rejected=rejected,
            substituted_source=substituted_source,
            cap_reached=cap_reached,
        )

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
    pipeline first treats it as a batch. The
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
        records = [sink.add(page, dpi=resolution) for page in page_lists[index]]
        return scan_batch(records, resolution=resolution)

    return _spool_next


def images_of(batch: ScanBatch) -> list[Image.Image]:
    """
    Read a batch's spooled pages back, in record order.

    The pages a scan produced are files, so an assertion about what was
    scanned has to open them. This is the one place that happens: an image
    assertion is handed ``images_of(batch)`` rather than ``batch.pages``.

    Order comes from the record list and nothing else: the directory is never
    sorted or globbed, so a scan whose files sort differently from their page
    order still reads back in page order.

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

    Subclasses the ABC rather than duck-typing it, so the type checkers catch
    a stub that falls out of step with the backend contract.

    Import it as ``from tests.conftest import StubScannerBackend``; the bare
    ``conftest`` form raises ``ModuleNotFoundError``, because ``tests/`` is a
    package, so pytest's default prepend mode imports it as ``tests.*``.
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
            settings: Only ``resolution`` is used, as the pages' dpi and the batch's.
            sink: The caller's sink, which receives the one page.

        Returns:
            A batch of the single record the sink returned.

        """
        record = sink.add(_inked_page(), dpi=settings.resolution)
        return scan_batch([record], resolution=settings.resolution)


class AlwaysContinueFlipCoordinator(FlipCoordinator):
    """
    A flip coordinator whose operator flips the stack the instant it is asked.

    For pipeline tests that exercise a manual-duplex run but are not about the
    flip wait itself.  A manual-duplex request has to carry a coordinator, and
    this one answers ``CONTINUED`` at once, so pass B starts straight away.

    Subclasses the ABC rather than duck-typing it, so the type checkers catch
    a coordinator that falls out of step with the contract.

    Import it as ``from tests.conftest import AlwaysContinueFlipCoordinator``;
    the bare ``conftest`` form raises ``ModuleNotFoundError``, because
    ``tests/`` is a package, so pytest's default prepend mode imports it as
    ``tests.*``.
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
    paperless.upload_document.return_value = ApiDelivery(task_id="mock-task-uuid")
    # A poll that filed the document; a failed one raises, and a duplicate
    # refusal returns a TaskDuplicate, which a test sets for itself.
    paperless.poll_task.return_value = TaskFiled(task={"status": "SUCCESS"})
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

    Every pause is followed by another read, including the last one after the
    deadline, so a state reached while the helper slept is still returned.

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

    def arrived() -> Job | None:
        """Read the job once, recording its state, and return it if awaited."""
        nonlocal observed
        job = store.get_job(job_id)
        if job is None:
            return None
        observed = job.state.value
        return job if job.state in wanted else None

    deadline = time.monotonic() + timeout
    tick = threading.Event()  # never set: each wait() is a bounded pause
    while True:
        found = arrived()
        if found is not None:
            return found
        tick.wait(_POLL_INTERVAL)
        if time.monotonic() >= deadline:
            break
    found = arrived()
    if found is not None:
        return found
    names = ", ".join(sorted(s.value for s in wanted))
    msg = (
        f"Job {job_id} did not reach {names} within {timeout}s "
        f"(last observed: {observed})"
    )
    raise RuntimeError(msg)


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


def browser_quiet_window(page: Page, milliseconds: float) -> None:
    """
    Let the page run for ``milliseconds`` while nothing is expected to happen.

    The browser counterpart of ``quiet_window``: Playwright's synchronous API
    dispatches page events only during its own calls, so the wait goes through
    the page, which keeps receiving events throughout. A request or swap that
    should not happen is then recorded by the test's listeners. Import it as
    ``from tests.conftest import browser_quiet_window``.

    Args:
        page: The page to leave running.
        milliseconds: How long to leave it alone. Keep it short.

    """
    page.wait_for_timeout(milliseconds)


# The child enters a real JobWorkspace, spools blank pages the way the scanner
# does (a PNG with a 300 dpi pHYs chunk), says where it is, and dies without
# running a single ``finally`` or ``__exit__``: what a SIGKILL, the OOM killer
# or a power cut leaves behind.
_KILLED_WORKSPACE_CHILD = """\
import os
import signal
from pathlib import Path

from PIL import Image

from saneless.workspace import SPOOL_DIR_NAME, JobWorkspace

with JobWorkspace(
    Path(os.environ["SANELESS_TEST_TMP_DIR"]),
    job_id=os.environ["SANELESS_TEST_JOB_ID"],
    title=os.environ["SANELESS_TEST_TITLE"],
    profile=os.environ["SANELESS_TEST_PROFILE"],
) as path:
    spool = path / SPOOL_DIR_NAME
    for name in os.environ["SANELESS_TEST_PAGES"].split(","):
        Image.new("L", (64, 64), 255).save(spool / name, dpi=(300, 300))
    print(path, flush=True)
    os.kill(os.getpid(), signal.SIGKILL)
"""

_KILLED_CHILD_TIMEOUT_SECONDS = 30


def leave_killed_workspace(
    tmp_dir: Path,
    *,
    job_id: str,
    title: str,
    profile: str = "default",
    pages: Sequence[str] = ("a-0001.png", "a-0002.png"),
) -> Path:
    """
    Leave behind the workspace of a scan whose process was SIGKILLed.

    A child process enters a real ``JobWorkspace`` in ``tmp_dir``, spools
    ``pages`` into it, and SIGKILLs itself, so the kernel -- not saneless --
    releases the workspace's lock. Every argv element is a literal and the
    per-run values travel in the environment, as ``test_pdf._measure_assembly``
    does: ``sys.executable`` in the argv would take the call off ruff's S603
    allow-list, and this project adds no suppressions.

    Import it as ``from tests.conftest import leave_killed_workspace``.

    Args:
        tmp_dir: The existing scratch directory to create the workspace in.
        job_id: The job id the workspace records.
        title: The title the workspace records.
        profile: The profile name the workspace records.
        pages: The spool file names to write, each a blank 64x64 page.

    Returns:
        The orphaned workspace directory.

    """
    env = {
        **os.environ,
        "SANELESS_TEST_PYTHON": sys.executable,
        "SANELESS_TEST_SOURCE": _KILLED_WORKSPACE_CHILD,
        "SANELESS_TEST_TMP_DIR": str(tmp_dir),
        "SANELESS_TEST_JOB_ID": job_id,
        "SANELESS_TEST_TITLE": title,
        "SANELESS_TEST_PROFILE": profile,
        "SANELESS_TEST_PAGES": ",".join(pages),
    }
    completed = subprocess.run(
        ["/bin/sh", "-c", 'exec "$SANELESS_TEST_PYTHON" -c "$SANELESS_TEST_SOURCE"'],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=_KILLED_CHILD_TIMEOUT_SECONDS,
    )
    assert completed.returncode == -signal.SIGKILL, completed.stderr
    workspace = Path(completed.stdout.strip())
    assert workspace.parent == tmp_dir, completed.stdout
    return workspace


def leaf_routes(app: FastAPI) -> list[BaseRoute]:
    """
    Every leaf route the app serves, flattening FastAPI's included routers.

    FastAPI represents an included router as one opaque wrapper object in
    ``app.routes`` rather than splicing its routes in, so a plain filter over
    ``app.routes`` finds none of them.  Raising on an empty result makes a
    further change to that shape fail here, once, instead of quietly emptying
    every caller's filter -- and an emptied filter is what would leave the
    cross-origin coverage guard green while proving nothing.

    ``Mount`` objects are yielded alongside ``APIRoute`` ones, so a caller
    enumerating ``Route | Mount`` still sees ``/static``.  Each caller keeps
    its own ``isinstance`` narrowing, which is also what lets it reach
    ``.path``.

    Import it as ``from tests.conftest import leaf_routes``.  There is
    deliberately no fixture wrapper: the call sites need this for three
    different app objects -- one built by a helper, one extracted from a
    ``TestClient``, one from a fixture -- so a fixture would need factory
    machinery for no added safety.

    Args:
        app: The application whose routes to enumerate.

    Returns:
        Every leaf route, in the order the app declares them.

    Raises:
        AssertionError: If the app appears to serve no routes at all.

    """
    found = _flatten_routes(app.routes)
    if not found:
        msg = (
            "enumerating the app's routes found none, so every caller's filter "
            "would be empty; FastAPI's included-router wrapper has changed shape"
        )
        raise AssertionError(msg)
    return found


def _flatten_routes(routes: Sequence[BaseRoute]) -> list[BaseRoute]:
    """
    Replace each included-router wrapper with the routes it stands for.

    Selection is by ``isinstance``, never by probing for the attribute: a
    ``getattr`` defaulting to ``None`` when ``original_router`` is missing
    would silently return nothing if that attribute went away while the
    wrapper remained, which is exactly the hole the raise in
    ``leaf_routes`` exists to close.  The private class is imported when the
    routes are walked, so its disappearance fails the tests that walk routes,
    loudly, without stopping the rest of the suite from importing this module.

    Args:
        routes: The routes to walk, at any depth.

    Returns:
        The leaves, with every wrapper expanded in place.

    """
    included: type[_IncludedRouter] = import_module("fastapi.routing")._IncludedRouter
    found: list[BaseRoute] = []
    for route in routes:
        if isinstance(route, included):
            found.extend(_flatten_routes(route.original_router.routes))
        else:
            found.append(route)
    return found


# The page's hidden loader, and the option its Profile select opens on.  The
# page is the only thing that renders either, so a test that means "what the
# person sees once the page has loaded" reads both from the page it has.
_LIST_LOADER = 'id="metadata-loader"'
_OPENING_PROFILE = re.compile(
    r'<select name="profile" id="profile-select".*?'
    r'<option value="(?P<name>[^"]*)" selected>',
    re.DOTALL,
)


def load_the_lists(client: TestClient, page: str) -> str:
    """
    Ask for the lists the way the page's loader does, and return the answer.

    ``/`` renders the tag list and the correspondent select loading, and a
    hidden loader asks ``/api/metadata`` for both, naming the profile the
    Profile select shows.  This sends that request: an assertion about the
    ticks, the options or the profile markers a person sees once the page has
    loaded reads them from its answer.  A page with no loader, because it
    shows neither list, asks nothing, and the answer is empty.

    Args:
        client: The browser that rendered ``page``.
        page: The rendered ``/``.

    Returns:
        The lazy list load's response body, or ``""`` when the page has no
        loader.

    """
    if _LIST_LOADER not in page:
        return ""
    opening = _OPENING_PROFILE.search(page)
    assert opening is not None, "the page's Profile select opens on no option"
    response = client.get(
        "/api/metadata", params={"profile": html.unescape(opening.group("name"))}
    )
    assert response.status_code == 200, response.text
    return response.text
