"""
Import hygiene for ``saneless.scanner.saned_probe``, the saned pre-probe.

The probe is a leaf: ``checks.py`` imports it at runtime to build the scanner
row, and ``doctor`` must still run on a machine without python-sane. So the
module may import nothing from ``saneless`` at runtime beyond
``saneless.scanner.net_hosts``, and loading it must not load ``sane``.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import saneless
from saneless import checks
from saneless.scanner import saned_probe

_PROBE_PATH = Path(saneless.__file__).parent / "scanner" / "saned_probe.py"


def _is_type_checking_guard(node: ast.If) -> bool:
    """
    Tell whether an ``if`` statement tests ``TYPE_CHECKING``.

    Args:
        node: The ``if`` statement.

    Returns:
        True for ``if TYPE_CHECKING:`` and ``if typing.TYPE_CHECKING:``.

    """
    test = node.test
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    return (
        isinstance(test, ast.Attribute)
        and test.attr == "TYPE_CHECKING"
        and isinstance(test.value, ast.Name)
        and test.value.id == "typing"
    )


def _runtime_saneless_imports(source: str) -> list[str]:
    """
    List the ``saneless`` modules ``source`` imports when it runs.

    Every import statement at any depth counts, including one inside a
    function, except those in the body of an ``if TYPE_CHECKING:`` block,
    which never run. Relative imports are resolved against
    ``saneless.scanner``, where the probe lives. A ``from X import name``
    yields ``X``; a ``from . import name`` yields the package's ``name``,
    because with no module after the dots each name is a submodule.

    Args:
        source: Source text of a module in ``saneless.scanner``.

    Returns:
        The ``saneless`` module names reached, sorted and without duplicates.

    """
    package_parts = ["saneless", "scanner"]
    modules: set[str] = set()

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.If) and _is_type_checking_guard(node):
            for child in node.orelse:
                visit(child)
            return
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base_parts = (
                package_parts[: len(package_parts) - node.level + 1]
                if node.level
                else []
            )
            if node.module:
                modules.add(".".join([*base_parts, node.module]))
            else:
                base = ".".join(base_parts)
                modules.update(f"{base}.{alias.name}" for alias in node.names)
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(ast.parse(source))
    return sorted(
        module
        for module in modules
        if module == "saneless" or module.startswith("saneless.")
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param("import saneless.config\n", ["saneless.config"], id="plain"),
        pytest.param(
            "from saneless.scanner.base import DeviceInfo\n",
            ["saneless.scanner.base"],
            id="from-import",
        ),
        pytest.param(
            "from . import sane_backend\n",
            ["saneless.scanner.sane_backend"],
            id="relative",
        ),
        pytest.param(
            "def late() -> None:\n    import saneless.checks\n",
            ["saneless.checks"],
            id="inside-a-function",
        ),
        pytest.param(
            '"""Never import saneless.config here."""\n'
            "# import saneless.checks\n"
            "import socket\n",
            [],
            id="docstring-and-comment",
        ),
        pytest.param(
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from saneless.config import Settings\n",
            [],
            id="type-checking-only",
        ),
        pytest.param(
            "import typing\n"
            "if typing.TYPE_CHECKING:\n"
            "    from saneless.config import Settings\n",
            [],
            id="typing-dot-type-checking",
        ),
        pytest.param(
            "import flags\n"
            "if flags.TYPE_CHECKING:\n"
            "    from saneless.config import Settings\n",
            ["saneless.config"],
            id="another-modules-type-checking-runs",
        ),
    ],
)
def test_the_import_reader_finds_runtime_imports_only(
    source: str, expected: list[str]
) -> None:
    """
    The reader reports every runtime import and nothing else.

    A reader that matched lines of text would miss an import inside a function
    and flag a docstring; one that counted ``TYPE_CHECKING`` imports would
    flag the annotation-only ``Settings`` import the probe needs.

    Args:
        source: Module text to read.
        expected: The ``saneless`` modules it must report.

    """
    assert _runtime_saneless_imports(source) == expected


def test_saned_probe_imports_only_net_hosts_from_saneless() -> None:
    """The probe's one runtime ``saneless`` import is the host-list rule."""
    source = _PROBE_PATH.read_text(encoding="utf-8")

    assert _runtime_saneless_imports(source) == ["saneless.scanner.net_hosts"]


def test_importing_saned_probe_loads_no_sane_module() -> None:
    """
    A fresh interpreter that imports the probe has not loaded python-sane.

    The import runs the package ``__init__`` modules as ``doctor`` would, so
    this also catches a ``sane`` import reached through them.
    """
    script = textwrap.dedent(
        """
        import sys

        import saneless.scanner.saned_probe

        print(sorted(k for k in sys.modules if k in ("sane", "_sane")))
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_checks_defines_no_name_the_probe_exports() -> None:
    """
    ``checks`` neither defines nor re-exports anything the probe module exports.

    A copy or alias left behind would be a second patch target: a test that
    patched it would change nothing the probe reads.
    """
    assert saned_probe.__all__
    assert [name for name in saned_probe.__all__ if hasattr(checks, name)] == []
