"""``python -m saneless`` runs the same command line as the ``saneless`` script."""

from __future__ import annotations

import subprocess
import sys

_CHILD_SECONDS = 30
"""How long the child interpreter may take before the test gives up on it."""


def test_python_dash_m_saneless_runs_the_cli() -> None:
    """The package runs as a module and answers ``--version`` like the script."""
    completed = subprocess.run(
        [sys.executable, "-m", "saneless", "--version"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        check=False,
        timeout=_CHILD_SECONDS,
    )
    assert completed.returncode == 0, completed.stderr
    assert "version" in completed.stdout
