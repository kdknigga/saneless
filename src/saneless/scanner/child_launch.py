"""
Start, and stop, the short-lived child interpreters that call libsane.

Every child saneless runs for SANE work is started the same way, described in
docs/explanation/decisions/0002-listing-in-a-child-process.md: a clean exec of
this interpreter in isolated mode, in a session of its own, with saneless's
own settings stripped from its environment.  A child that overruns is killed
with its whole process group and reaped.

The argv is the literal ``/bin/sh -c 'exec "$P" -I "$C"'``, with the two paths
passed in the environment, because ruff's S603 accepts only a literal argv,
and because argv is what ``ps`` shows every local user: no path chosen at run
time and no device id ever appears there.  ``exec`` replaces the shell, so the
child's PID is the interpreter's own and killing it leaves no grandchild.
``-I`` ignores every ``PYTHON*`` variable, so python-sane must be importable
from the interpreter's own site-packages.

This is a leaf: it imports no python-sane, and nothing from the scanner
backends or the health checks.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import TYPE_CHECKING, Final

from saneless.scanner.net_hosts import SANE_NET_HOSTS, effective_sane_net_hosts
from saneless.sigpipe import sigpipe_unblocked

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "OWN_PREFIX",
    "child_environment",
    "kill_and_reap",
    "signal_name",
    "start_child",
]

# The child's environment keeps everything SANE and its backends read (SANE
# settings, locale, proxies, certificates) and drops saneless's own settings,
# which include the Paperless token.  Matched ignoring case, because the
# settings loader reads its variables ignoring case.
OWN_PREFIX: Final = "saneless_"

# The two variables the child's literal argv expands.  They are set after the
# environment is stripped, so they are the only SANELESS_ variables a child
# sees.
_PYTHON_VARIABLE: Final = "SANELESS_CHILD_PYTHON"
_SCRIPT_VARIABLE: Final = "SANELESS_CHILD_SCRIPT"


def child_environment(configured_host: str) -> dict[str, str]:
    """
    Build the environment a child runs in.

    It is this process's environment minus every ``SANELESS_*`` variable, in
    any case, so no saneless setting or secret reaches the child.
    ``SANE_NET_HOSTS`` is set from the same derivation the scanner check
    probes, never copied from whatever this process's environment holds at
    the moment, and removed when that derivation names no host.

    Args:
        configured_host: The ``scanner.host`` setting, possibly empty.

    Returns:
        The child's environment, before the launcher adds its own two
        variables.

    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.casefold().startswith(OWN_PREFIX)
    }
    hosts = effective_sane_net_hosts(configured_host)
    if hosts:
        env[SANE_NET_HOSTS] = hosts
    else:
        env.pop(SANE_NET_HOSTS, None)
    return env


def start_child(script: Path, configured_host: str) -> subprocess.Popen[bytes]:
    """
    Start one child interpreter running ``script``.

    The child gets pipes on stdin and stdout, inherits stderr, closes every
    other descriptor, and leads a session of its own, so a Ctrl-C sent to
    saneless's process group does not reach it.

    Args:
        script: The file the child interpreter runs.
        configured_host: The ``scanner.host`` setting, possibly empty.

    Returns:
        The running child.  The caller owns it, and must reap it.

    Raises:
        OSError: The fork or the exec failed, for instance under a process or
            memory limit; the caller decides how to report that.

    """
    env = child_environment(configured_host)
    env[_PYTHON_VARIABLE] = sys.executable
    env[_SCRIPT_VARIABLE] = str(script)
    # The child inherits this thread's signal mask, and must not start with
    # SIGPIPE blocked the way saneless runs.
    with sigpipe_unblocked():
        return subprocess.Popen(
            (
                "/bin/sh",
                "-c",
                'exec "$SANELESS_CHILD_PYTHON" -I "$SANELESS_CHILD_SCRIPT"',
            ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            env=env,
            close_fds=True,
            start_new_session=True,
        )


def kill_and_reap(proc: subprocess.Popen[bytes]) -> None:
    """
    Kill the child and its process group, and wait for the child.

    The child leads a process group of its own, so killing the group also
    stops a helper program a backend started, or a fork of the child, that
    would otherwise outlive it, still holding a device or the reply's pipe.
    Only the child is waited for: the others are not this process's children.

    Args:
        proc: The child process.

    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        # The group is already gone, or holds nothing this process may signal.
        proc.kill()
    proc.wait()


def signal_name(signum: int) -> str:
    """
    Name a signal a child died from, for a log line or a message.

    Args:
        signum: The signal's number.

    Returns:
        Its name, such as ``SIGSEGV``, or ``signal N`` for a number this
        platform does not name.

    """
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"signal {signum}"
