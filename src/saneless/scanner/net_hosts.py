"""
Decide, in one place, which host list SANE's net backend will read.

Two modules need this fact: the scanner backend, which writes the configured
host into ``SANE_NET_HOSTS`` before SANE starts, and the scanner check, which
probes the hosts SANE will dial. They used to derive it two ways and drifted:
one treated an exported but empty variable as unset, the other as set, so the
check could probe a host SANE never dialled. Both now call this module.

The rule: an exported, non-empty ``SANE_NET_HOSTS`` wins over the configured
host. An exported but empty variable names no host, so it counts as unset and
the configured host is what SANE is given.

What libsane does with the value (sane-backends ``backend/net.c``,
``sane_init``): it splits the variable on ``:``, skips empty entries and has no
port syntax. It reads the variable when the net backend is initialised, which
is not at ``sane_init`` itself: the dll backend initialises the net backend
lazily, at the first device listing or open after ``sane_init``. saneless lists
scanners in a short-lived child process, so each listing child reads the
variable afresh when it starts, and the main process reads it again after
every re-initialisation of SANE, which the server does at the start of each
scan job.

This is a leaf module. It imports only ``os`` and ``typing.Final`` and nothing
from ``saneless``, so ``checks.py`` can import it at runtime and ``doctor``
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
