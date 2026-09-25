"""
Make untrusted text safe to print on a terminal or write into a log line.

Device vendors and models, stored job titles and other strings saneless did not
write itself can carry control characters: an ESC that starts a terminal escape
sequence, a newline that forges a further log line, a C1 CSI that some
terminals honour on its own. These helpers turn each such character into its
visible escape.

This is a leaf module. ``scanner/base.py`` imports it, so it imports nothing
from ``saneless`` and can never be part of an import cycle.
"""

from __future__ import annotations

import unicodedata
from typing import Final

__all__ = ["has_control_characters", "neutralise_bounded", "neutralise_controls"]

_CONTROL_CATEGORY: Final = "Cc"
"""
The Unicode category of every control character, and of nothing else.

It covers exactly U+0000 to U+001F (C0, tab and newline included), U+007F
(DEL) and U+0080 to U+009F (C1). Accented letters, the no-break space and
format characters such as the zero-width space fall in other categories.
"""

_ELLIPSIS: Final = "\u2026"
"""What ``neutralise_bounded`` puts in place of the text it cut."""


def _is_control(ch: str) -> bool:
    """
    Return whether one character is a control character.

    Args:
        ch: A single character.

    Returns:
        True when Unicode files ``ch`` under the control category.

    """
    return unicodedata.category(ch) == _CONTROL_CATEGORY


def has_control_characters(text: str) -> bool:
    """
    Return whether any character of ``text`` is a control character.

    Args:
        text: The text to inspect.

    Returns:
        True when ``text`` holds a C0 control, DEL or a C1 control.

    """
    return any(_is_control(ch) for ch in text)


def neutralise_controls(text: str) -> str:
    r"""
    Replace every control character with its visible escape.

    Each control becomes what ``repr`` shows for it without the quotes, so ESC
    becomes ``\x1b`` and a tab becomes ``\t``. That is the convention
    ``config._escape_name`` uses for configuration names. Every other
    character, quotes and backslashes included, is kept as it is, so ordinary
    text reads unchanged.

    Args:
        text: Text from outside saneless, such as a device model or a title.

    Returns:
        The text with no control character left in it.

    """
    return "".join(repr(ch)[1:-1] if _is_control(ch) else ch for ch in text)


def neutralise_bounded(text: str, limit: int) -> str:
    """
    Neutralise ``text``, then cut it to at most ``limit`` characters.

    Neutralising comes first, so the bound applies to what is printed and a
    cut can never leave half of an escape sequence live. When the text is cut,
    its last kept character is an ellipsis.

    Args:
        text: Text from outside saneless, of any length.
        limit: The most characters the result may have.

    Returns:
        The neutralised text, no longer than ``limit``.

    Raises:
        ValueError: If ``limit`` is below one, leaving no room for the
            ellipsis.

    """
    if limit < 1:
        msg = f"limit must be at least 1, got {limit}"
        raise ValueError(msg)
    safe = neutralise_controls(text)
    if len(safe) <= limit:
        return safe
    return safe[: limit - 1] + _ELLIPSIS
