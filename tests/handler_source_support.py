"""
Structural checks over the web handlers read every module the handlers span.

The handlers live in ``saneless.web.routes``, and the view-models and helpers
they call live in modules beside it.  A check that parses only ``routes.py``
would stop seeing a call the moment the code making it moved next door, and
would then pass on nothing.  ``handler_family_tree`` parses every module in
``HANDLER_FAMILY`` and joins their bodies into one syntax tree, so a check
asks its question of the handlers and everything they were split into at once.

Import it as ``from tests.handler_source_support import handler_family_tree``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

from saneless.web import metadata_view, owner, routes, strip_view

if TYPE_CHECKING:
    from types import ModuleType

__all__ = ["HANDLER_FAMILY", "handler_family_tree"]

HANDLER_FAMILY: tuple[ModuleType, ...] = (routes, owner, strip_view, metadata_view)
"""The routes module and every module split out of it, routes first."""


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
