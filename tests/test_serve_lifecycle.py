"""
A real ``saneless serve``, stopped with SIGTERM: exit 0, within the stop budget.

A supervisor's SIGTERM is a normal stop: the server exits 0, PID 1 or not,
well inside Docker's 10 s grace period when no scan is running, even with a
scanner listing in flight on the status strip's background thread.

Only a real process shows the exit code and every thread the interpreter
waits for, so each test starts the real ``saneless`` entry point in a child
process and session, on an OS-chosen loopback port, with only the scanner,
the SANE library check and paperless-ngx replaced.  Every wait has a
deadline; a child still running at its bound is killed with its session.
"""

from __future__ import annotations

import contextlib
import os
import re
import select
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import httpx2
import pytest

from saneless.vocabulary import ExitCode
from tests.conftest import poll_until

if TYPE_CHECKING:
    from collections.abc import Generator

    from tests.conftest import SocketGuard

# The repository root: the child imports ``tests.golden_support`` from here.
_REPO_ROOT = Path(__file__).resolve().parents[1]

# How long the child may take to start: an interpreter, the imports, the bind.
_START_BOUND_SECONDS = 30.0

# The longest an idle stop may take, start to exit: Docker's default grace
# period before it sends SIGKILL.
_STOP_BOUND_SECONDS = 10.0

# How long one HTTP call may take before the test gives up on it.  Check
# again waits at most a few seconds for its probe, so this is only a bound on
# a hang.
_HTTP_TIMEOUT_SECONDS = 10.0

# How long the scanner listing child may take to start after the click.
_LISTING_START_SECONDS = 10.0

# How long a child the test has given up on may take to die once killed.
_REAP_BOUND_SECONDS = 10.0

_SERVING = re.compile(rb"Serving on (http://127\.0\.0\.1:\d+)")

# The variable the listing stand-in writes its PID into.  It has no SANELESS_
# prefix, so it survives the strip the launcher applies to the listing
# child's environment.
_PIDFILE_VARIABLE = "LISTING_TEST_PIDFILE"

_CONFIG = """\
[paperless]
url = "http://paperless.invalid:8000"
token = "lifecycle-token"
consume_dir = "{consume_dir}"

[output]
tmp_dir = "{tmp_dir}"
data_dir = "{data_dir}"
"""

# A listing child that records its PID and then never answers, as a scanner
# host that accepted the connection and went quiet does.
_SLEEPER_CHILD = """\
import os
import signal
from pathlib import Path

Path(os.environ["LISTING_TEST_PIDFILE"]).write_text(str(os.getpid()))
signal.pause()
"""

# The real entry point, with only what a test cannot have: the SANE library
# check, the scanner and paperless-ngx.  paperless-ngx is an in-memory double,
# so nothing leaves the machine.  With a listing child named, the scanner's
# list-then-open runs that child through the real launcher, under the abort
# the status check hands it, as the real backend does.  The arguments travel
# in the environment, so the argv below stays a literal, and are taken out of
# it before the CLI starts, because saneless reads every SANELESS_ variable as
# a setting and refuses one it does not know.
_CHILD = """
import os
import sys
from pathlib import Path

config = os.environ.pop("SANELESS_TEST_CONFIG")
listing_child = os.environ.pop("SANELESS_TEST_LISTING_CHILD", "")
os.environ.pop("SANELESS_TEST_PYTHON")
os.environ.pop("SANELESS_TEST_SOURCE")

from saneless import cli as cli_module
from saneless import main
from saneless.scanner import listing
from saneless.scanner.base import DeviceSurvey
from saneless.scanner.listing import ListingRequest, run_listing_child
from saneless.web import app as app_module
from tests.conftest import StubScannerBackend
from tests.golden_support import RecordingPaperless, web_client_builder


class ListingScanner(StubScannerBackend):
    def list_and_open(self, open_if_unlisted, *, abort=None):
        run_listing_child(ListingRequest(), configured_host="", abort=abort)
        return DeviceSurvey(devices=())


def sane_backend(host=""):
    return ListingScanner() if listing_child else StubScannerBackend()


def require_sane():
    return None


if listing_child:
    listing._CHILD_FILE = Path(listing_child)
cli_module.require_sane = require_sane
cli_module.SaneBackend = sane_backend
app_module.PaperlessClient = web_client_builder(RecordingPaperless())
sys.argv = [
    "saneless", "--config", config, "serve", "--host", "127.0.0.1", "--port", "0"
]
sys.exit(main())
"""


@dataclass
class _Served:
    """
    A ``saneless serve`` child, and what it wrote to stderr so far.

    Attributes:
        proc: The child process.
        stderr: Everything read from its stderr so far.
        url: The address it announced, once read.

    """

    proc: subprocess.Popen[bytes]
    stderr: bytearray = field(default_factory=bytearray)
    url: str = ""

    def drain(self, timeout: float) -> bool:
        """
        Read what the child writes to stderr within ``timeout`` seconds.

        Args:
            timeout: The longest to wait for something to read.

        Returns:
            Whether stderr is still open: ``False`` once the child closed it.

        """
        pipe = self.proc.stderr
        if pipe is None:
            return False
        ready, _, _ = select.select([pipe], [], [], max(0.0, timeout))
        if not ready:
            return True
        chunk = os.read(pipe.fileno(), 65_536)
        self.stderr.extend(chunk)
        return bool(chunk)

    def shown(self) -> str:
        """
        Decode stderr so far, for a failure message.

        Returns:
            The output, with undecodable bytes replaced.

        """
        return self.stderr.decode(errors="replace")

    def kill(self) -> None:
        """Kill the child's whole session, if it is still running, and reap it."""
        if self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait(timeout=_REAP_BOUND_SECONDS)


@dataclass
class _Spawner:
    """
    Start ``saneless serve`` children for one test, and clean up after them.

    Attributes:
        tmp_path: The test's scratch directory.
        guard: The socket guard, told the port each child serves on.
        runs: Every child started, for the teardown.
        pidfile: Where a listing stand-in writes its PID.

    """

    tmp_path: Path
    guard: SocketGuard
    runs: list[_Served] = field(default_factory=list)
    pidfile: Path | None = None

    def start(self, *, slow_listing: bool = False) -> _Served:
        """
        Start ``saneless serve`` and wait for the address it announces.

        Args:
            slow_listing: Whether the scanner's listing runs a child that never
                answers.

        Returns:
            The running child, with its URL read.

        """
        data_dir = self.tmp_path / "data"
        consume_dir = self.tmp_path / "consume"
        consume_dir.mkdir()
        config = self.tmp_path / "saneless.toml"
        config.write_text(
            _CONFIG.format(
                consume_dir=consume_dir,
                tmp_dir=self.tmp_path / "scratch",
                data_dir=data_dir,
            ),
            encoding="utf-8",
        )
        env: dict[str, str] = {
            **os.environ,
            "SANELESS_TEST_PYTHON": sys.executable,
            "SANELESS_TEST_SOURCE": _CHILD,
            "SANELESS_TEST_CONFIG": str(config),
        }
        # No saned host from the developer's environment: the status check
        # would dial it.
        env.pop("SANE_NET_HOSTS", None)
        if slow_listing:
            child = self.tmp_path / "listing_child_sleeper.py"
            child.write_text(_SLEEPER_CHILD, encoding="utf-8")
            self.pidfile = self.tmp_path / "listing.pid"
            env["SANELESS_TEST_LISTING_CHILD"] = str(child)
            env[_PIDFILE_VARIABLE] = str(self.pidfile)
        proc = subprocess.Popen(
            [
                "/bin/sh",
                "-c",
                'exec "$SANELESS_TEST_PYTHON" -c "$SANELESS_TEST_SOURCE"',
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
            cwd=_REPO_ROOT,
            start_new_session=True,
        )
        served = _Served(proc=proc)
        self.runs.append(served)
        served.url = _read_url(served, time.monotonic() + _START_BOUND_SECONDS)
        # The child bound the port, so this process must be told about it.
        self.guard.allow_port(int(served.url.rpartition(":")[2]))
        return served

    def listing_pid(self) -> int | None:
        """
        Read the PID the listing stand-in wrote, if it has written it yet.

        Returns:
            The PID, or None before the child has written all of it.

        """
        if self.pidfile is None:
            return None
        try:
            text = self.pidfile.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        return int(text) if text.isdigit() else None

    def close(self) -> None:
        """Kill every child still running, the listing stand-in too, and reap."""
        for run in self.runs:
            try:
                run.kill()
            finally:
                if run.proc.stderr is not None:
                    run.proc.stderr.close()
        # The listing child runs in a session of its own, so killing the
        # server's session does not reach it.  It is killed by PID only while
        # that PID still runs the stand-in script, never a process that reused
        # the number.
        pid = self.listing_pid()
        if pid is not None and _runs_the_sleeper(pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


@pytest.fixture
def spawner(tmp_path: Path, socket_guard: SocketGuard) -> Generator[_Spawner]:
    """
    Start ``saneless serve`` children, and kill whatever is left at the end.

    Yields:
        The spawner.

    """
    spawn = _Spawner(tmp_path=tmp_path, guard=socket_guard)
    try:
        yield spawn
    finally:
        spawn.close()


def _runs_the_sleeper(pid: int) -> bool:
    """
    Tell whether ``pid`` is still the listing stand-in, alive.

    Args:
        pid: The process id the stand-in wrote.

    Returns:
        Whether the process exists, is not a zombie, and runs the stand-in.

    """
    if _is_dead(pid):
        return False
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except FileNotFoundError, ProcessLookupError:
        return False
    return b"listing_child_sleeper.py" in cmdline


def _is_dead(pid: int) -> bool:
    """
    Tell whether a process that is not this process's child has ended.

    Such a process is reaped by whichever process adopted it, so it can
    still show briefly as a zombie after it died.

    Args:
        pid: The process id.

    Returns:
        Whether the process is gone or a zombie.

    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except FileNotFoundError, ProcessLookupError:
        return True
    return stat.rpartition(")")[2].split()[0] in {"Z", "X"}


def _read_url(run: _Served, deadline: float) -> str:
    """
    Read stderr until the server announces its address, failing at ``deadline``.

    Args:
        run: The child.
        deadline: The ``time.monotonic`` reading to give up at.

    Returns:
        The announced ``http://127.0.0.1:<port>``.

    """
    while (match := _SERVING.search(run.stderr)) is None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            run.kill()
            pytest.fail(f"serve never announced its address; stderr:\n{run.shown()}")
        if not run.drain(remaining):
            run.kill()
            pytest.fail(
                f"serve closed stderr before announcing its address; "
                f"exit {run.proc.poll()}, stderr:\n{run.shown()}"
            )
    return match.group(1).decode("ascii")


def _exit_within(run: _Served, seconds: float) -> tuple[int, float]:
    """
    Wait at most ``seconds`` for the child to exit, reading its stderr.

    A child still running at the bound is killed, with its whole session, and
    the test fails naming the hang, so a hang costs the bound rather than
    the suite.

    Args:
        run: The child.
        seconds: The bound, from now.

    Returns:
        The child's exit code, and how long it took to exit.

    """
    began = time.monotonic()
    deadline = began + seconds
    while time.monotonic() < deadline and run.drain(deadline - time.monotonic()):
        if run.proc.poll() is not None:
            break
    with contextlib.suppress(subprocess.TimeoutExpired):
        code = run.proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        return code, time.monotonic() - began
    run.kill()
    pytest.fail(
        f"serve was still running {seconds:.1f}s after SIGTERM and was killed "
        f"(the stop hung); stderr:\n{run.shown()}"
    )


def test_sigterm_stops_an_idle_server_with_exit_0(spawner: _Spawner) -> None:
    """
    An idle server stops on SIGTERM, exit 0, well inside Docker's grace period.

    The server is not PID 1 here, so the kernel's default action would end it
    by the signal; a stop the server asked for is a normal stop.
    """
    run = spawner.start()
    with httpx2.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        health = client.get(f"{run.url}/health")
    assert health.status_code == 200, run.shown()

    os.kill(run.proc.pid, signal.SIGTERM)
    code, _ = _exit_within(run, _STOP_BOUND_SECONDS)

    assert code == ExitCode.SUCCESS, run.shown()


def test_sigterm_during_a_slow_listing_stops_within_10_seconds(
    spawner: _Spawner,
) -> None:
    """
    A stop with a scanner listing in flight: exit 0 within 10 s, listing gone.

    Check again asks the background thread for a probe, and the scanner check
    in that probe starts a listing child that never answers.  The request
    returns after its bounded wait; the listing is still running when SIGTERM
    arrives.  The stop aborts it, and the server is gone, exit 0, inside the
    idle budget, leaving no listing child behind.
    """
    run = spawner.start(slow_listing=True)
    with httpx2.Client(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        clicked = client.post(
            f"{run.url}/api/checks/refresh", headers={"Origin": run.url}
        )
    assert clicked.status_code == 200, run.shown()
    assert poll_until(
        lambda: spawner.listing_pid() is not None, _LISTING_START_SECONDS
    ), run.shown()
    pid = spawner.listing_pid()
    assert pid is not None
    assert not _is_dead(pid), "the listing child ended before the stop"

    os.kill(run.proc.pid, signal.SIGTERM)
    code, took = _exit_within(run, _STOP_BOUND_SECONDS)

    assert code == ExitCode.SUCCESS, f"exit {code} after {took:.2f}s\n{run.shown()}"
    assert took < _STOP_BOUND_SECONDS
    assert poll_until(lambda: _is_dead(pid), _REAP_BOUND_SECONDS)
