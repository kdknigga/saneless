"""
Structural template checks read tags, not text.

A check that greps a template's source for an attribute name also counts every
comment that mentions it, so a comment explaining the attribute breaks the
check.  These helpers strip the Jinja syntax first and then parse the HTML that
is left, so comments of either kind may name attributes freely:

- ``template_markup`` returns a template's literal text with every Jinja
  comment, statement and expression removed.
- ``template_start_tags`` returns every start tag of a template file, with its
  attributes, in document order.  HTML comments never yield a tag.
- ``markup_start_tags`` does the same for rendered HTML, such as a response
  body, so a check can compare attributes whatever order they are written in.

A Jinja expression inside an attribute value leaves the value empty, and an
attribute written inside a Jinja conditional is kept on its tag, so a check
sees every attribute the template can render.

``tests/`` is a package, so pytest's default prepend mode imports it as
``tests.*``, and the helper is imported by that package name.  Import it as
``from tests.template_support import template_start_tags``.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import TYPE_CHECKING, override

import jinja2

if TYPE_CHECKING:
    from pathlib import Path


class _StartTagCollector(HTMLParser):
    """Collect every start tag and self-closing tag with its attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    @override
    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def template_markup(source: str) -> str:
    """
    Return the literal text of a Jinja template, without any Jinja syntax.

    Only the lexer's ``data`` tokens are kept, joined in order, so Jinja
    comments, statements and expressions all disappear while the HTML around
    them is untouched.  Import it as
    ``from tests.template_support import template_markup``.
    """
    return "".join(
        value
        for _lineno, token, value in jinja2.Environment(autoescape=True).lex(source)
        if token == "data"
    )


def template_start_tags(path: Path) -> list[tuple[str, dict[str, str | None]]]:
    """
    Return the start tags of a template file and their attributes, in order.

    The file's Jinja syntax is removed with ``template_markup`` and the rest is
    parsed as HTML, so neither Jinja nor HTML comments contribute a tag.
    Import it as ``from tests.template_support import template_start_tags``.
    """
    collector = _StartTagCollector()
    collector.feed(template_markup(path.read_text(encoding="utf-8")))
    collector.close()
    return collector.tags


def markup_start_tags(markup: str) -> list[tuple[str, dict[str, str | None]]]:
    """
    Return the start tags of rendered HTML and their attributes, in order.

    Import it as ``from tests.template_support import markup_start_tags``.
    """
    collector = _StartTagCollector()
    collector.feed(markup)
    collector.close()
    return collector.tags
