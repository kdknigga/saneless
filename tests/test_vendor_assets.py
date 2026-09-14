"""
Pinning tests for the vendored htmx and Pico assets (ROBU-09).

The UI must work on a LAN with no internet, so htmx and Pico are served from the
package under ``/static/vendor/`` and ``base.html`` carries a SHA-384 ``integrity``
attribute for each. A browser refuses an asset whose bytes do not match, so these
tests recompute every hash from the files on disk and fail before a browser ever
sees a mismatch.

They also hold the 23.1 Pico coupling contract: only ``pico.min.css`` 2.1.1 is
vendored, ``pico.colors.css`` is never used, ``<html>`` carries no ``data-theme``,
and the ``--saneless-status-fallback`` block in ``app.css`` stays intact.
"""

from __future__ import annotations

import base64
import hashlib
from html.parser import HTMLParser
from typing import TYPE_CHECKING, override

from saneless.web.app import STATIC_DIR, TEMPLATE_DIR

if TYPE_CHECKING:
    from pathlib import Path

VENDOR_DIR = STATIC_DIR / "vendor"
BASE_HTML = TEMPLATE_DIR / "base.html"
APP_CSS = STATIC_DIR / "app.css"

HTMX_BYTES = 51250
PICO_BYTES = 83319
# One <link> for Pico, one <script> for htmx.
PINNED_TAG_COUNT = 2


class _TagCollector(HTMLParser):
    """Collect every start tag and its attributes from an HTML document."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    @override
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def _tags(path: Path) -> list[tuple[str, dict[str, str | None]]]:
    """Return the start tags of an HTML file in document order."""
    collector = _TagCollector()
    collector.feed(path.read_text(encoding="utf-8"))
    collector.close()
    return collector.tags


def sri_sha384(data: bytes) -> str:
    """Return the Subresource Integrity string for ``data`` using SHA-384."""
    digest = hashlib.sha384(data).digest()
    return "sha384-" + base64.b64encode(digest).decode("ascii")


def _static_path(url: str) -> Path:
    """Map a ``/static/...`` URL from a template onto its file under ``STATIC_DIR``."""
    prefix = "/static/"
    assert url.startswith(prefix), f"{url!r} is not a same-origin /static/ URL"
    return STATIC_DIR / url.removeprefix(prefix)


def test_integrity_matches_vendored_bytes() -> None:
    """Every integrity attribute in base.html equals sha384 of its file (ROBU-09)."""
    pinned = [
        (attrs, attrs.get("integrity"))
        for _tag, attrs in _tags(BASE_HTML)
        if attrs.get("integrity") is not None
    ]
    assert len(pinned) == PINNED_TAG_COUNT

    for attrs, integrity in pinned:
        url = attrs.get("href") or attrs.get("src")
        assert url is not None, f"integrity without href/src: {attrs!r}"
        path = _static_path(url)
        assert path.is_file(), f"{url} does not map to a file under STATIC_DIR"
        assert sri_sha384(path.read_bytes()) == integrity, f"SRI mismatch for {url}"


def test_vendored_file_sizes() -> None:
    """The vendored files have the byte lengths of the npm registry builds."""
    assert (VENDOR_DIR / "htmx-2.0.8.min.js").stat().st_size == HTMX_BYTES
    assert (VENDOR_DIR / "pico-2.1.1.min.css").stat().st_size == PICO_BYTES


def test_templates_reference_no_external_url() -> None:
    """No template contains an http:// or https:// URL, so no internet is needed."""
    templates = sorted(TEMPLATE_DIR.rglob("*.html"))
    assert templates
    for template in templates:
        text = template.read_text(encoding="utf-8")
        assert "http://" not in text, f"{template} references http://"
        assert "https://" not in text, f"{template} references https://"


def test_licence_notices_present() -> None:
    """Both upstream licence notices ship next to the vendored files."""
    for name in ("LICENSE-htmx.txt", "LICENSE-pico.md"):
        notice = VENDOR_DIR / name
        assert notice.is_file(), f"missing {name}"
        assert notice.stat().st_size > 0, f"{name} is empty"


def test_no_pico_colors_anywhere() -> None:
    """pico.colors.css is neither vendored nor referenced (23.1 coupling contract)."""
    for root in (STATIC_DIR, TEMPLATE_DIR):
        for path in root.rglob("*"):
            assert "pico.colors" not in path.name, f"{path} is a pico.colors file"
            if path.is_file():
                assert b"pico.colors" not in path.read_bytes(), (
                    f"{path} references pico.colors"
                )


def test_html_tag_has_no_data_theme() -> None:
    """The <html> tag in base.html carries no data-theme (23.1 coupling contract)."""
    html_tags = [attrs for tag, attrs in _tags(BASE_HTML) if tag == "html"]
    assert len(html_tags) == 1
    assert "data-theme" not in html_tags[0]


def test_app_css_status_fallback_block_intact() -> None:
    """app.css keeps the --saneless-status-fallback light/dark block (23.1)."""
    css = APP_CSS.read_text(encoding="utf-8")
    assert "--saneless-status-fallback" in css
    assert "@media only screen and (prefers-color-scheme: dark)" in css
    assert '[data-theme="dark"]' in css
    assert "--pico-color-" not in css


def test_base_html_has_no_crossorigin() -> None:
    """Same-origin SRI needs no crossorigin attribute on any tag in base.html."""
    for tag, attrs in _tags(BASE_HTML):
        assert "crossorigin" not in attrs, f"<{tag}> carries crossorigin"
