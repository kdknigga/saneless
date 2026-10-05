"""
Structural checks over the web handlers read every module the handlers span.

The handlers live in ``saneless.web.routes``, and the view-models and helpers
they call live in modules beside it.  A check that parses only ``routes.py``
would stop seeing a call the moment the code making it moved next door, and
would then pass on nothing.  ``handler_family_tree`` parses every module in
``HANDLER_FAMILY`` and joins their bodies into one syntax tree, so a check
asks its question of the handlers and the code they call at once.

Import it as ``from tests.handler_source_support import handler_family_tree``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

from saneless.web import (
    job_view,
    metadata_view,
    owner,
    profile_view,
    routes,
    scan_block,
    status_view,
    strip_view,
)

if TYPE_CHECKING:
    from types import ModuleType

__all__ = ["HANDLER_FAMILY", "NOT_IN_HANDLER_FAMILY", "handler_family_tree"]

HANDLER_FAMILY: tuple[ModuleType, ...] = (
    routes,
    owner,
    strip_view,
    metadata_view,
    status_view,
    profile_view,
    scan_block,
    job_view,
)
"""The routes module and the modules holding the code its handlers call."""

NOT_IN_HANDLER_FAMILY: dict[str, str] = {
    "saneless.web.services": "the typed accessor itself, which holds no handler code",
    "saneless.web.refresher": "the collaborator the probe runs in, which must probe",
    "saneless.web.errors": (
        "the exception handlers, whose catch-all logs an unhandled exception's "
        "traceback on purpose"
    ),
}
"""The ``saneless.web`` modules routes imports that the family leaves out, and why."""


def handler_family_tree() -> ast.Module:
    """
    Parse every module in ``HANDLER_FAMILY`` into one syntax tree.

    Returns:
        A module whose body is each family module's body in turn, so comments
        and docstrings never count and every existing walk sees all of them.

    """
    body: list[ast.stmt] = []
    for module in HANDLER_FAMILY:
        source = Path(str(module.__file__)).read_text(encoding="utf-8")
        body.extend(ast.parse(source).body)
    return ast.Module(body=body, type_ignores=[])
