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
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

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


def test_a_command_that_does_not_serve_loads_no_web_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    ``jobs`` and ``doctor`` run to the end without FastAPI, Starlette or uvicorn.

    The command bodies run, not only their ``--help``, so an import made inside
    a body is seen.  The child reads an empty config file named on the command
    line, and libsane an empty backend list, so neither reaches past the test's
    own directories.

    Args:
        tmp_path: Holds the config file and the empty SANE configuration.
        monkeypatch: Points the child's libsane at the empty configuration.

    """
    config = tmp_path / "saneless.toml"
    config.write_text("", encoding="utf-8")
    sane_dir = tmp_path / "sane.d"
    sane_dir.mkdir()
    (sane_dir / "dll.conf").write_text("", encoding="utf-8")
    monkeypatch.setenv("SANE_CONFIG_DIR", str(sane_dir))
    loaded = _loaded_top_level_modules(
        f"""
        from click.testing import CliRunner

        from saneless.cli import cli

        runner = CliRunner()
        for args in (["jobs"], ["jobs", "--json"], ["doctor"], ["doctor", "--help"]):
            result = runner.invoke(cli, ["--config", {str(config)!r}, *args])
            assert result.exception is None or isinstance(
                result.exception, SystemExit
            ), (args, result.output, result.exception)
            assert result.exit_code in (0, 2), (args, result.output)
        """
    )

    assert not loaded.intersection(_WEB_STACK), sorted(loaded.intersection(_WEB_STACK))


def test_the_probe_sees_the_web_stack_once_the_app_is_imported() -> None:
    """The same probe reports FastAPI after the web app is imported."""
    loaded = _loaded_top_level_modules("import saneless.web.app\n")

    assert "fastapi" in loaded
