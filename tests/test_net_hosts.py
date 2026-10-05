"""
Tests for ``saneless.scanner.net_hosts``, the one derivation of SANE_NET_HOSTS.

An exported non-empty ``SANE_NET_HOSTS`` wins over the configured host. An
exported but empty variable names no host, so it counts as unset and the
configured host is what SANE will be given.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import saneless
from saneless.scanner.net_hosts import (
    SANE_NET_HOSTS,
    effective_sane_net_hosts,
    exported_sane_net_hosts,
)


def test_constant_names_the_variable() -> None:
    """The constant is the environment variable's name, spelt once."""
    assert SANE_NET_HOSTS == "SANE_NET_HOSTS"


@pytest.mark.parametrize(
    ("environment", "configured", "expected_exported", "expected_effective"),
    [
        pytest.param(None, "scanbox.lan", "", "scanbox.lan", id="unset"),
        pytest.param("", "scanbox.lan", "", "scanbox.lan", id="exported-empty"),
        pytest.param(
            "ext-a:ext-b", "scanbox.lan", "ext-a:ext-b", "ext-a:ext-b", id="exported"
        ),
        pytest.param(None, "", "", "", id="unset-and-nothing-configured"),
    ],
)
def test_effective_host_list_truth_table(
    monkeypatch: pytest.MonkeyPatch,
    environment: str | None,
    configured: str,
    expected_exported: str,
    expected_effective: str,
) -> None:
    """Each environment state yields the host list SANE's net backend will read."""
    if environment is None:
        monkeypatch.delenv("SANE_NET_HOSTS", raising=False)
    else:
        monkeypatch.setenv("SANE_NET_HOSTS", environment)

    assert exported_sane_net_hosts() == expected_exported
    assert effective_sane_net_hosts(configured) == expected_effective


def test_net_hosts_is_a_leaf_that_imports_nothing_from_saneless() -> None:
    """
    Loading the module on its own pulls no ``saneless`` module in.

    ``checks.py`` imports it at runtime, and ``doctor`` must still run on a
    machine without python-sane, so it may import nothing from the package.
    The module is loaded from its file under a private name in a fresh
    interpreter, so ``sys.modules`` shows only what the module itself imports,
    not the packages a dotted import would load on the way.
    """
    module_path = Path(saneless.__file__).parent / "scanner" / "net_hosts.py"
    script = textwrap.dedent(
        """
        import importlib.util
        import os
        import sys

        spec = importlib.util.spec_from_file_location(
            "leaf_probe", os.environ["SANELESS_TEST_MODULE"]
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        print(sorted(k for k in sys.modules if k.startswith("saneless")))
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        env={
            **os.environ,
            "SANELESS_TEST_MODULE": str(module_path),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
