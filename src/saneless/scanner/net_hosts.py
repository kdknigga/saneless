"""
Decide, in one place, which host list SANE's net backend will read.

The scanner backend, the listing child's environment and the saned pre-probe
all call this module, so the hosts the probe dials are the hosts SANE dials.
An exported, non-empty ``SANE_NET_HOSTS`` wins over the configured host; an
exported but empty variable names no host, so it counts as unset.

libsane splits the value on ``:``, skips empty entries and has no port syntax.
It reads the variable when the dll backend first initialises the net backend,
at the first listing or open after ``sane_init``, not at ``sane_init`` itself.

This is a leaf module. It imports only ``os`` and ``typing.Final`` and nothing
from ``saneless``, so the health checks can reach it at runtime and ``doctor``
still runs on a machine without python-sane.
"""

from __future__ import annotations

import os
from typing import Final

__all__ = ["SANE_NET_HOSTS", "effective_sane_net_hosts", "exported_sane_net_hosts"]

SANE_NET_HOSTS: Final = "SANE_NET_HOSTS"
"""The environment variable libsane's net backend reads its host list from."""


def exported_sane_net_hosts() -> str:
    """
    Return the non-empty ``SANE_NET_HOSTS`` value exported to this process.

    An exported but empty variable names no host, so it is reported the same
    way as an absent one.

    Returns:
        The exported colon-separated host list, or ``""`` when the variable is
        absent or empty.

    """
    return os.environ.get(SANE_NET_HOSTS) or ""


def effective_sane_net_hosts(configured_host: str) -> str:
    """
    Return the host list SANE's net backend will read.

    A non-empty exported ``SANE_NET_HOSTS`` wins; otherwise the configured
    host is the value saneless writes before SANE starts.

    Args:
        configured_host: The ``scanner.host`` setting, possibly empty.

    Returns:
        The colon-separated host list SANE will dial, possibly empty.

    """
    return exported_sane_net_hosts() or configured_host
