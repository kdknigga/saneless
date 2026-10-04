"""
The saneless process never loads python-sane or libsane.

Every SANE call runs in a short-lived child process: a listing child for
listing, open checks and capability reads, and a scan child for a scan.  The
saneless process itself, the CLI or the web server, must never import
python-sane, because loading it loads libsane, and a libsane that hangs or
crashes inside a C call there would take the CLI or the whole server down
with it, where in a child it costs one listing or one scan.

The rule is pinned two ways.  The source-structure test reads every module's
imports and allows python-sane only in the two modules that run as children.
The runtime tests start a real saneless process through ``saneless.main()``,
check python-sane is installed, build the backend and finish a scan, then
read that process's own ``sys.modules`` and ``/proc/self/maps``.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

import saneless

_SRC: Final = Path(saneless.__file__).resolve().parent

# python-sane's two modules: the Python wrapper and the extension that links
# libsane.
_PYTHON_SANE: Final = frozenset({"sane", "_sane"})

# The modules that run as child processes, and so may import python-sane.
_CHILD_SIDE: Final = frozenset({"scanner/scan_session.py", "scanner/_listing_child.py"})

# The scan child's own modules.  Only the child side may import them: a
# parent-side import of either would put the pass code, and through it
# python-sane, within one call of the saneless process.
_SCAN_CHILD_MODULES: Final = frozenset(
    {"saneless.scanner.scan_session", "saneless.scanner._scan_child"}
)
_SCAN_CHILD_SIDE: Final = frozenset(
    {"scanner/scan_session.py", "scanner/_scan_child.py"}
)

# The functions that import a module named by a string.
_IMPORT_CALLS: Final = frozenset({"import_module", "__import__"})

_CHILD_SECONDS: Final = 60
"""How long one probe process may take before the test gives up on it."""

# A stand-in scan child: the standard library and the protocol module only,
# so it loads no python-sane either.  It answers one feeder pass with a single
# 4x3 grey page, as the real child does when the feeder then runs out.
_STAND_IN_CHILD: Final = """\
import os
import sys

from saneless.scanner import scan_protocol as protocol


def send(frame, payload=b""):
    data = memoryview(protocol.encode_frame(frame) + payload)
    while data:
        data = data[os.write(1, data):]


send(protocol.Ready())
for line in sys.stdin.buffer:
    command = protocol.decode_command(line)
    if command is protocol.ControlOp.EXIT:
        send(protocol.Bye())
        os._exit(0)
    if not isinstance(command, protocol.ScanCommand):
        continue
    send(protocol.StageFrame(stage="open", page=None))
    send(protocol.StageFrame(stage="configure", page=None))
    send(
        protocol.Configured(
            resolution=150,
            frame_format="gray",
            last_frame=True,
            pixels_per_line=4,
            lines=3,
            depth=8,
            bytes_per_line=4,
            use_adf=True,
        )
    )
    send(protocol.StageFrame(stage="start", page=1))
    send(protocol.StageFrame(stage="read", page=1))
    pixels = bytes([128]) * 12
    header = protocol.PageHeader(
        number=1, mode="L", width=4, height=3, dpi=150, nbytes=len(pixels)
    )
    send(header, pixels)
    sys.stdin.buffer.readline()
    send(protocol.StageFrame(stage="close", page=None))
    send(protocol.PassDone(resolution=150, rejected=0, substituted_source=None, cap=None))
os._exit(0)
"""

# The saneless process under test.  ``saneless.main()`` runs with its command
# line replaced by the probe, which does what every scanning command does
# before and during a scan, then reports what this process loaded.  An
# ISOLATION_PROBE_CHILD path points the scan launcher at a stand-in child.
_PROBE: Final = """\
import json
import os
import sys
from pathlib import Path

import saneless
import saneless.cli


def probe():
    from saneless.scanner import scan_child
    from saneless.scanner.base import ScanSettings
    from saneless.scanner.sane_backend import SaneBackend, require_sane
    from saneless.spool import SpooledPageSink

    env = os.environ
    if env.get("ISOLATION_PROBE_CHILD"):
        scan_child._CHILD_FILE = Path(env["ISOLATION_PROBE_CHILD"])
    require_sane()
    backend = SaneBackend()
    settings = ScanSettings(
        source=env["ISOLATION_PROBE_SOURCE"], resolution=75, mode="Gray"
    )
    sink = SpooledPageSink(Path(env["ISOLATION_PROBE_SPOOL"]), "a", 0)
    try:
        batch = backend.scan_pages(env["ISOLATION_PROBE_DEVICE"], settings, sink)
    finally:
        backend.close()
    maps = Path("/proc/self/maps").read_text(encoding="utf-8")
    report = {
        "modules": sorted(m for m in ("sane", "_sane") if m in sys.modules),
        "libsane": [line for line in maps.splitlines() if "libsane" in line],
        "pages": len(batch.pages),
    }
    print(json.dumps(report))


saneless.cli.cli = probe
saneless.main()
"""


def _relative(path: Path) -> str:
    """
    Name a source file the way the allow-lists do.

    Returns:
        The path below the package directory, with forward slashes.

    """
    return path.relative_to(_SRC).as_posix()


def _package_of(path: Path) -> str:
    """
    Name the package a source file's relative imports resolve against.

    Returns:
        The dotted package name.

    """
    parts = ("saneless", *path.relative_to(_SRC).parent.parts)
    return ".".join(parts)


def _imported_names(path: Path) -> set[str]:
    """
    List every module name one source file imports, however it imports it.

    ``import a.b``, ``from a import b`` (both ``a`` and ``a.b``), relative
    imports resolved against the file's package, and a string literal passed
    to ``importlib.import_module`` or ``__import__``.

    Returns:
        The dotted names.

    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = _package_of(path)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                anchor = ".".join(parts[: len(parts) - node.level + 1])
                base = f"{anchor}.{node.module}" if node.module else anchor
            else:
                base = node.module or ""
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call):
            func = node.func
            called = func.attr if isinstance(func, ast.Attribute) else None
            if isinstance(func, ast.Name):
                called = func.id
            if (
                called in _IMPORT_CALLS
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                names.add(node.args[0].value)
    return names


def _importers(wanted: frozenset[str], *, top_level: bool) -> set[str]:
    """
    Find the source files that import any of ``wanted``.

    Args:
        wanted: The module names looked for.
        top_level: Compare only each imported name's first component, so
            ``sane`` also matches ``sane.something``.

    Returns:
        The files, named as the allow-lists name them.

    """
    found: set[str] = set()
    for path in sorted(_SRC.rglob("*.py")):
        names = _imported_names(path)
        if top_level:
            names = {name.split(".", 1)[0] for name in names}
        if names & wanted:
            found.add(_relative(path))
    return found


@pytest.mark.source_structure
def test_only_child_modules_import_python_sane() -> None:
    """
    python-sane is imported only by the modules that run as children.

    And the scan child's modules are imported only by each other, so no
    parent-side module is one call away from the pass code.
    """
    assert _importers(_PYTHON_SANE, top_level=True) == _CHILD_SIDE
    scan_child_importers = _importers(_SCAN_CHILD_MODULES, top_level=False)
    # The scan child does import its pass code, so the walk cannot pass by
    # finding nothing.
    assert "scanner/_scan_child.py" in scan_child_importers
    assert scan_child_importers <= _SCAN_CHILD_SIDE, scan_child_importers


def _run_probe(tmp_path: Path, **variables: str) -> dict[str, object]:
    """
    Run one saneless process through the probe and read its report.

    Args:
        tmp_path: Where the scanned page is spooled.
        **variables: The ``ISOLATION_PROBE_*`` settings for this run.

    Returns:
        The process's report: the python-sane modules it had loaded, its
        ``libsane`` mappings and how many pages the scan returned.

    """
    spool = tmp_path / "spool"
    spool.mkdir()
    env = {**os.environ, "ISOLATION_PROBE_SPOOL": str(spool), **variables}
    completed = subprocess.run(
        [sys.executable, "-I", "-c", _PROBE],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=env,
        check=False,
        timeout=_CHILD_SECONDS,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert isinstance(report, dict)
    return report


def test_the_saneless_process_never_loads_libsane_with_a_stand_in_child(
    tmp_path: Path,
) -> None:
    """
    Checking for python-sane, building the backend and scanning load neither.

    The scan runs in a stand-in child that loads no python-sane either, so
    anything the report finds was loaded by the saneless process itself.

    Args:
        tmp_path: Holds the stand-in child and the spooled page.

    """
    if sys.platform != "linux":
        pytest.skip("reads /proc/self/maps")
    if any(importlib.util.find_spec(name) is None for name in sorted(_PYTHON_SANE)):
        pytest.skip("python-sane is not installed, so require_sane would refuse")
    child = tmp_path / "stand_in_scan_child.py"
    child.write_text(_STAND_IN_CHILD, encoding="utf-8")

    report = _run_probe(
        tmp_path,
        ISOLATION_PROBE_CHILD=str(child),
        ISOLATION_PROBE_DEVICE="test:0",
        ISOLATION_PROBE_SOURCE="ADF",
    )

    assert report["pages"] == 1, report
    assert report["modules"] == [], report
    assert report["libsane"] == [], report


@pytest.mark.sane_hardware
def test_the_saneless_process_never_loads_libsane_with_a_real_child(
    tmp_path: Path, sane_test_backend_config: None
) -> None:
    """
    A real flatbed scan of ``test:0`` leaves the saneless process without libsane.

    The real scan child loads python-sane and libsane in its own process; the
    saneless process that started it must not have loaded either.

    Args:
        tmp_path: Holds the spooled page.
        sane_test_backend_config: Names only SANE's ``test`` backend.

    """
    _ = sane_test_backend_config  # side effect: SANE_CONFIG_DIR is set
    if sys.platform != "linux":
        pytest.skip("reads /proc/self/maps")

    report = _run_probe(
        tmp_path, ISOLATION_PROBE_DEVICE="test:0", ISOLATION_PROBE_SOURCE="Flatbed"
    )

    assert report["pages"] == 1, report
    assert report["modules"] == [], report
    assert report["libsane"] == [], report
