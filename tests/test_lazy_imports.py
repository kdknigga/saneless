"""
What each command path loads, probed in a fresh interpreter.

A leaf module (the shared wording, the paper sizes, the text neutraliser and
the SANE_NET_HOSTS derivation) loads no ``pydantic_settings``: importing it by
its dotted name runs only the package ``__init__``, and that imports nothing
heavy.  A command that does not serve (``jobs``, ``doctor``) loads no web
stack: FastAPI, Starlette and uvicorn are imported by ``serve`` alone, so the
commands an owner runs most often do not pay for a server they never start.

Each probe runs ``sys.executable -c`` without a shell, so the parent's
``sys.modules`` cannot hide or supply a module the child would load.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap

import pytest

_WEB_STACK = ("fastapi", "starlette", "uvicorn")

_LEAF_MODULES = (
    "saneless.vocabulary",
    "saneless.paper_sizes",
    "saneless.text_safety",
    "saneless.scanner.net_hosts",
)


def _loaded_top_level_modules(script: str) -> set[str]:
    """
    Run ``script`` in a fresh interpreter and return its top-level modules.

    The script runs first, then the child prints the sorted top-level names in
    its ``sys.modules``; a child that fails fails the test with its stderr.
    """
    probe = (
        textwrap.dedent(script)
        + "\nimport sys\n"
        + "print(sorted({name.partition('.')[0] for name in sys.modules}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    loaded = ast.literal_eval(result.stdout.strip().splitlines()[-1])
    return set(loaded)


@pytest.mark.parametrize("module", _LEAF_MODULES)
def test_a_leaf_module_loads_no_pydantic_settings(module: str) -> None:
    """Importing a leaf module by its dotted name leaves the settings library out."""
    loaded = _loaded_top_level_modules(f"import {module}\n")

    assert "pydantic_settings" not in loaded


def test_a_command_that_does_not_serve_loads_no_web_stack() -> None:
    """``jobs --help`` and ``doctor --help`` run without FastAPI, Starlette or uvicorn."""
    loaded = _loaded_top_level_modules(
        """
        from click.testing import CliRunner

        from saneless.cli import cli

        runner = CliRunner()
        for command in ("jobs", "doctor"):
            result = runner.invoke(cli, [command, "--help"])
            assert result.exit_code == 0, (command, result.output)
        """
    )

    assert not loaded.intersection(_WEB_STACK), sorted(loaded.intersection(_WEB_STACK))


def test_the_probe_sees_the_web_stack_once_the_app_is_imported() -> None:
    """The same probe reports FastAPI after the web app is imported."""
    loaded = _loaded_top_level_modules("import saneless.web.app\n")

    assert "fastapi" in loaded
