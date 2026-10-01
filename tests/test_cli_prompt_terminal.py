"""
The flip prompt on a real terminal: every ending exits promptly, with its code.

``saneless scan`` with a manual-duplex profile asks the operator to flip the
stack between its two passes.  On a real terminal each way of leaving that
question has to end the process within two seconds, with the documented exit
code:

- Ctrl-C is the operator's cancel: 130.
- No answer before ``operator_wait_timeout_seconds``: 1, the fronts kept in
  ``failed/``.
- End of input (Ctrl-D): a cancel, 130.
- A hangup: an interruption, 129, whichever of the hangup and the end of input
  a closing terminal delivers first.
- An answer: the scan goes on to the backs.

Only a real terminal shows the fault these guard against: a process that has
printed its outcome and then waits at interpreter exit for someone to press
Enter.  With stdout piped it hides, so every test here runs the real
``saneless`` entry point in a child process whose stdin, stdout and stderr are
all one pseudo-terminal, and reads that terminal from this side.

The child is started in its own session, so the terminal is not its
controlling terminal: typing the Ctrl-C byte would generate nothing, and
Ctrl-C is delivered as the SIGINT the line discipline would have sent.  End of
input (the Ctrl-D byte) does go through the line discipline.  Every wait is a ``select`` on
the terminal with a deadline, and a child that outlives its bound is killed and
the test failed, so a hang cannot take the suite down with it.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import select
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from saneless.vocabulary import ExitCode

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

# The repository root: the child imports ``tests.golden_support`` from here.
_REPO_ROOT = Path(__file__).resolve().parents[1]

# How long a prompt ending may take to end the process.
_EXIT_BOUND_SECONDS = 2.0

# The flip wait, in seconds: the smallest value the config accepts.
_FLIP_TIMEOUT_SECONDS = 1

# How long the child may take to start up and reach the flip question: an
# interpreter start, the imports, and one scan pass against a fake scanner.
_QUESTION_BOUND_SECONDS = 30.0

# How long an answered scan may take to scan the backs, build the PDF and
# deliver it to the in-memory paperless-ngx.
_DELIVERY_BOUND_SECONDS = 30.0

# The gap between the two events of a closing terminal, in either order.
_EVENT_GAP_SECONDS = 0.05

# How long a child the test has given up on may take to die once killed.
_REAP_BOUND_SECONDS = 10.0

_PROFILE = "flip-duplex"
_TITLE = "Quarterly Report"

# The question the flip prompt asks, as it appears on the terminal.
_QUESTION = b"Scan the back sides?"

_END_OF_INPUT = b"\x04"

_CONFIG = """\
[scanner]
device = "test:device:001"

[paperless]
url = "http://paperless.invalid:8000"
token = "terminal-token"

[output]
tmp_dir = "{tmp_dir}"
data_dir = "{data_dir}"
log_file = "{log_file}"
operator_wait_timeout_seconds = {timeout}

[profiles.default]

[profiles.{profile}]
source = "ADF"
duplex = "manual"
"""

# The real entry point, with only what a test cannot have replaced: the SANE
# library check, the scanner and paperless-ngx.  The arguments travel in the
# environment, so the argv below stays a literal, and are taken out of it
# before the CLI starts, because saneless reads every SANELESS_ variable as a
# setting and refuses one it does not know.
_CHILD = """
import json
import os
import sys

config = os.environ.pop("SANELESS_TEST_CONFIG")
args = json.loads(os.environ.pop("SANELESS_TEST_ARGS"))
os.environ.pop("SANELESS_TEST_PYTHON")
os.environ.pop("SANELESS_TEST_SOURCE")

from saneless import cli as cli_module
from saneless import main
from tests.golden_support import (
    DistinctPageScanner,
    RecordingPaperless,
    cli_client_builder,
)

scanner = DistinctPageScanner(passes=((0, 2, 4), (5, 3, 1)))


def sane_backend(host=""):
    return scanner


def require_sane():
    return None


cli_module.require_sane = require_sane
cli_module.SaneBackend = sane_backend
cli_module.PaperlessClient = cli_client_builder(RecordingPaperless())
sys.argv = ["saneless", "--config", config, *args]
sys.exit(main())
"""


@dataclass
class _PtyRun:
    """
    A ``saneless scan`` child on a pseudo-terminal, and what it wrote so far.

    Attributes:
        proc: The child process.
        master: This side of the terminal.
        failed_dir: Where the child keeps the pages of a failed scan.
        output: Everything read from the terminal so far.

    """

    proc: subprocess.Popen[bytes]
    master: int
    failed_dir: Path
    output: bytearray = field(default_factory=bytearray)

    def drain(self, timeout: float) -> bool:
        """
        Read what the child writes within ``timeout`` seconds, if anything.

        Args:
            timeout: The longest to wait for something to read.

        Returns:
            Whether the terminal is still open: ``False`` once every copy of
            the child's side is closed.

        """
        ready, _, _ = select.select([self.master], [], [], max(0.0, timeout))
        if not ready:
            return True
        try:
            chunk = os.read(self.master, 4096)
        except OSError as exc:
            # Linux reports a terminal whose other side is closed as EIO.
            if exc.errno == errno.EIO:
                return False
            raise
        self.output.extend(chunk)
        return bool(chunk)

    def shown(self) -> str:
        """
        Decode the output so far, for a failure message.

        Returns:
            The output, with undecodable bytes replaced.

        """
        return self.output.decode(errors="replace")

    def kill(self) -> None:
        """Kill the child's whole session, if it is still running, and reap it."""
        if self.proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait(timeout=_REAP_BOUND_SECONDS)


@pytest.fixture
def spawn_scan(tmp_path: Path) -> Generator[Callable[[], _PtyRun]]:
    """
    Start ``saneless scan`` children on terminals, and clean up after them.

    Every child is killed if it is still running when the test ends, and
    every terminal is closed, whatever the test did.

    Yields:
        A function that starts one child and returns it.

    """
    runs: list[_PtyRun] = []

    def spawn() -> _PtyRun:
        """
        Start ``saneless scan`` with stdin, stdout and stderr on one new terminal.

        Returns:
            The running child.

        """
        run = _spawn_scan(tmp_path)
        runs.append(run)
        return run

    yield spawn
    for run in runs:
        try:
            run.kill()
        finally:
            os.close(run.master)


def _spawn_scan(tmp_path: Path) -> _PtyRun:
    """
    Start ``saneless scan`` with stdin, stdout and stderr on one new terminal.

    Every argv element is a literal and the per-run values travel in the
    environment, as ``tests.conftest.leave_killed_workspace`` does, which keeps
    the call on ruff's S603 allow-list without a suppression.

    Args:
        tmp_path: The test's scratch directory, for the config and every path
            the scan writes.

    Returns:
        The running child.

    """
    try:
        master, slave = os.openpty()
    except OSError as exc:
        pytest.skip(f"no pseudo-terminal available: {exc}")
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = tmp_path / "saneless.toml"
    config.write_text(
        _CONFIG.format(
            tmp_dir=tmp_path / "scratch",
            data_dir=data_dir,
            log_file=tmp_path / "logs" / "saneless.log",
            timeout=_FLIP_TIMEOUT_SECONDS,
            profile=_PROFILE,
        )
    )
    env = {
        **os.environ,
        "SANELESS_TEST_PYTHON": sys.executable,
        "SANELESS_TEST_SOURCE": _CHILD,
        "SANELESS_TEST_CONFIG": str(config),
        "SANELESS_TEST_ARGS": json.dumps(
            ["scan", "--profile", _PROFILE, "--title", _TITLE]
        ),
    }
    try:
        proc = subprocess.Popen(
            [
                "/bin/sh",
                "-c",
                'exec "$SANELESS_TEST_PYTHON" -c "$SANELESS_TEST_SOURCE"',
            ],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=env,
            cwd=_REPO_ROOT,
            start_new_session=True,
        )
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    return _PtyRun(proc=proc, master=master, failed_dir=data_dir / "failed")


def _read_until(run: _PtyRun, needle: bytes, deadline: float) -> None:
    """
    Read the terminal until ``needle`` has appeared, failing at ``deadline``.

    Args:
        run: The child.
        needle: The bytes to wait for.
        deadline: The ``time.monotonic`` reading to give up at.

    """
    while needle not in run.output:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pytest.fail(f"{needle!r} never appeared; the terminal read:\n{run.shown()}")
        if not run.drain(remaining):
            pytest.fail(
                f"the child closed the terminal before {needle!r}; "
                f"exit {run.proc.poll()}, the terminal read:\n{run.shown()}"
            )


def _exit_code_within(run: _PtyRun, seconds: float) -> int:
    """
    Wait at most ``seconds`` for the child to exit, reading what it writes.

    A child still running at the bound is killed, with its whole session, and
    the test fails naming the hang, so a regression costs the bound rather
    than the suite.

    Args:
        run: The child.
        seconds: The bound, from now.

    Returns:
        The child's exit code.

    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and run.drain(deadline - time.monotonic()):
        if run.proc.poll() is not None:
            break
    with contextlib.suppress(subprocess.TimeoutExpired):
        return run.proc.wait(timeout=max(0.0, deadline - time.monotonic()))
    run.kill()
    pytest.fail(
        f"saneless was still running {seconds:.1f}s later and was killed "
        f"(the process hung); the terminal read:\n{run.shown()}"
    )


def _at_the_question(spawn_scan: Callable[[], _PtyRun]) -> _PtyRun:
    """
    Start a manual-duplex scan and wait until the flip question is on screen.

    Args:
        spawn_scan: The fixture's spawner.

    Returns:
        The child, waiting at the flip question.

    """
    run = spawn_scan()
    _read_until(run, _QUESTION, time.monotonic() + _QUESTION_BOUND_SECONDS)
    return run


def _gap(run: _PtyRun) -> None:
    """
    Let a moment pass between two events, reading the terminal meanwhile.

    Args:
        run: The child.

    """
    deadline = time.monotonic() + _EVENT_GAP_SECONDS
    while (remaining := deadline - time.monotonic()) > 0 and run.drain(remaining):
        pass


def test_ctrl_c_at_the_flip_prompt_exits_130_promptly(
    spawn_scan: Callable[[], _PtyRun],
) -> None:
    """Ctrl-C at the flip question: the process is gone within 2 s, exit 130."""
    run = _at_the_question(spawn_scan)

    os.kill(run.proc.pid, signal.SIGINT)
    code = _exit_code_within(run, _EXIT_BOUND_SECONDS)

    assert code == ExitCode.CANCELLED, run.shown()


def test_a_flip_timeout_exits_1_promptly_and_keeps_the_fronts(
    spawn_scan: Callable[[], _PtyRun],
) -> None:
    """Nobody answers: exit 1 within 2 s of the deadline, the fronts in failed/."""
    run = _at_the_question(spawn_scan)

    code = _exit_code_within(run, _FLIP_TIMEOUT_SECONDS + _EXIT_BOUND_SECONDS)

    assert code == ExitCode.SCAN, run.shown()
    kept = sorted(run.failed_dir.iterdir()) if run.failed_dir.is_dir() else []
    assert [path for path in kept if path.stem.endswith("-fronts")], kept


def test_end_of_input_at_the_flip_prompt_exits_130(
    spawn_scan: Callable[[], _PtyRun],
) -> None:
    """Ctrl-D at the flip question is a cancel: exit 130 within 2 s."""
    run = _at_the_question(spawn_scan)

    os.write(run.master, _END_OF_INPUT)
    code = _exit_code_within(run, _EXIT_BOUND_SECONDS)

    assert code == ExitCode.CANCELLED, run.shown()


def test_a_hangup_at_the_flip_prompt_exits_129(
    spawn_scan: Callable[[], _PtyRun],
) -> None:
    """A hangup at the flip question is an interruption: exit 129 within 2 s."""
    run = _at_the_question(spawn_scan)

    os.kill(run.proc.pid, signal.SIGHUP)
    code = _exit_code_within(run, _EXIT_BOUND_SECONDS)

    assert code == ExitCode.HANGUP, run.shown()


def test_hangup_then_end_of_input_exits_129(
    spawn_scan: Callable[[], _PtyRun],
) -> None:
    """A closing terminal's SIGHUP, then its end of input: still exit 129."""
    run = _at_the_question(spawn_scan)

    os.kill(run.proc.pid, signal.SIGHUP)
    _gap(run)
    # The child may already be gone, taking its side of the terminal with it.
    with contextlib.suppress(OSError):
        os.write(run.master, _END_OF_INPUT)
    code = _exit_code_within(run, _EXIT_BOUND_SECONDS)

    assert code == ExitCode.HANGUP, run.shown()


def test_end_of_input_then_hangup_exits_129(
    spawn_scan: Callable[[], _PtyRun],
) -> None:
    """End of input first, the hangup 50 ms later: the hangup wins, exit 129."""
    run = _at_the_question(spawn_scan)

    os.write(run.master, _END_OF_INPUT)
    _gap(run)
    with contextlib.suppress(ProcessLookupError):
        os.kill(run.proc.pid, signal.SIGHUP)
    code = _exit_code_within(run, _EXIT_BOUND_SECONDS)

    assert code == ExitCode.HANGUP, run.shown()


def test_an_answer_at_the_flip_prompt_continues(
    spawn_scan: Callable[[], _PtyRun],
) -> None:
    """Answering yes scans the backs and delivers the document: exit 0."""
    run = _at_the_question(spawn_scan)

    os.write(run.master, b"y\n")
    code = _exit_code_within(run, _DELIVERY_BOUND_SECONDS)

    assert code == ExitCode.SUCCESS, run.shown()
    assert b"Scanning reverse sides" in run.output, run.shown()
