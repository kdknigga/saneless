"""The template tag reader sees markup only, never Jinja or HTML comments."""

from typing import TYPE_CHECKING

from tests.template_support import template_markup, template_start_tags

if TYPE_CHECKING:
    from pathlib import Path


def _write(tmp_path: Path, source: str) -> Path:
    """Write ``source`` to a template file under ``tmp_path`` and return its path."""
    path = tmp_path / "page.html"
    path.write_text(source, encoding="utf-8")
    return path


def test_markup_keeps_only_template_data() -> None:
    """Jinja comments, statements and expressions are dropped; literal text stays."""
    source = '<p>{# note #}Hi {{ name }}{% if x %}!{% endif %}</p><a href="/">x</a>'
    assert template_markup(source) == '<p>Hi !</p><a href="/">x</a>'


def test_attribute_named_only_in_comments_is_not_counted(tmp_path: Path) -> None:
    """An attribute mentioned in a Jinja or HTML comment is not read as a tag."""
    path = _write(
        tmp_path,
        '<button hx-confirm="Sure?">{# hx-confirm is named here #}</button>'
        "<!-- hx-confirm too -->",
    )
    tags = template_start_tags(path)
    assert [tag for tag, attrs in tags if "hx-confirm" in attrs] == ["button"]
    assert tags == [("button", {"hx-confirm": "Sure?"})]


def test_conditional_attribute_is_read_with_its_neighbours(tmp_path: Path) -> None:
    """An attribute inside a Jinja conditional still belongs to its tag."""
    path = _write(
        tmp_path, '<button {% if busy %}disabled{% endif %} id="scan-btn">Scan</button>'
    )
    assert template_start_tags(path) == [
        ("button", {"id": "scan-btn", "disabled": None})
    ]


def test_self_closing_tags_are_collected_once_in_document_order(
    tmp_path: Path,
) -> None:
    """Self-closing tags appear once each, in the order they are written."""
    path = _write(tmp_path, '<div><input name="a"/><br/><img src="x.png"></div>')
    assert [tag for tag, _attrs in template_start_tags(path)] == [
        "div",
        "input",
        "br",
        "img",
    ]
