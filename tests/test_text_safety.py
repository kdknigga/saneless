"""
Control characters in text are detected, and escaped visibly for display.

A character counts as a control exactly when Unicode files it under the
``Cc`` category: the C0 range, DEL and the C1 range. Display escaping also
covers bidi controls and Unicode separators; accented letters, no-break spaces
and right-to-left text pass through untouched.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import saneless
from saneless.text_safety import (
    has_control_characters,
    neutralise_bounded,
    neutralise_controls,
)

_ELLIPSIS = "\u2026"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("\t", True, id="c0-tab"),
        pytest.param("\x00", True, id="c0-nul"),
        pytest.param("\x1b", True, id="c0-escape"),
        pytest.param("line\nbreak", True, id="c0-newline-inside-text"),
        pytest.param("\x7f", True, id="del"),
        pytest.param("\x85", True, id="c1-nel"),
        pytest.param("\x9b", True, id="c1-csi"),
        pytest.param("", False, id="empty"),
        pytest.param("plain", False, id="ascii"),
        pytest.param("\u00e9", False, id="latin-accented"),
        pytest.param("\u00a0", False, id="no-break-space"),
        pytest.param("\u200b", False, id="zero-width-space-is-format-not-control"),
    ],
)
def test_has_control_characters(*, text: str, expected: bool) -> None:
    """A string holds a control exactly when one of its characters is ``Cc``."""
    assert has_control_characters(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("a\x1b[2Jb", "a\\x1b[2Jb", id="c0-escape-sequence"),
        pytest.param("\t", "\\t", id="c0-tab"),
        pytest.param("\n", "\\n", id="c0-newline"),
        pytest.param("\x00", "\\x00", id="c0-nul"),
        pytest.param("\x7f", "\\x7f", id="del"),
        pytest.param("\x85", "\\x85", id="c1-nel"),
        pytest.param("\x9b31m", "\\x9b31m", id="c1-csi"),
        pytest.param("\u00e9\u00a0", "\u00e9\u00a0", id="accented-and-nbsp-kept"),
        pytest.param("it's \\ fine", "it's \\ fine", id="quote-and-backslash-kept"),
        pytest.param("", "", id="empty"),
        pytest.param("a\u202eb", "a\\u202eb", id="bidi-right-to-left-override"),
        pytest.param("a\u202ab", "a\\u202ab", id="bidi-embedding"),
        pytest.param("a\u2066b\u2069", "a\\u2066b\\u2069", id="bidi-isolate"),
        pytest.param("a\u2028b", "a\\u2028b", id="line-separator"),
        pytest.param("a\u2029b", "a\\u2029b", id="paragraph-separator"),
        pytest.param(
            "\u05e9\u05dc\u05d5\u05dd \u200f!",
            "\u05e9\u05dc\u05d5\u05dd \u200f!",
            id="right-to-left-text-and-mark-kept",
        ),
    ],
)
def test_neutralise_controls(*, text: str, expected: str) -> None:
    """Each control becomes its visible escape; every other character stays."""
    assert neutralise_controls(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("".join(chr(c) for c in range(0x20)), id="every-c0"),
        pytest.param("".join(chr(c) for c in range(0x7F, 0xA0)), id="del-and-c1"),
        pytest.param("mixed \x1b[31mred\x1b[0m \x85 text", id="mixed"),
    ],
)
def test_neutralise_controls_output_has_no_control_character(text: str) -> None:
    """Whatever goes in, what comes out holds no control character."""
    assert not has_control_characters(neutralise_controls(text))


def test_display_hazards_are_not_refused_as_controls() -> None:
    """
    Bidi controls and Unicode separators are escaped for display only.

    ``has_control_characters`` decides which titles are refused, and that
    rule stays exactly the ``Cc`` category.
    """
    for hazard in ("\u202e", "\u2066", "\u2028", "\u2029"):
        assert not has_control_characters(hazard)
        assert neutralise_controls(hazard) != hazard


@pytest.mark.parametrize(
    ("text", "limit", "expected"),
    [
        pytest.param("abc", 10, "abc", id="short-kept"),
        pytest.param("abcde", 5, "abcde", id="exactly-the-limit-kept"),
        pytest.param("abcdef", 5, "abcd" + _ELLIPSIS, id="one-over-cut"),
        pytest.param("\x1b", 10, "\\x1b", id="control-neutralised"),
        pytest.param(
            "\x1b\x1b\x1b", 6, "\\x1b" + "\\" + _ELLIPSIS, id="neutralised-then-cut"
        ),
    ],
)
def test_neutralise_bounded(*, text: str, limit: int, expected: str) -> None:
    """The text is neutralised first, then cut to ``limit`` with an ellipsis."""
    assert neutralise_bounded(text, limit) == expected


def test_neutralise_bounded_long_input_is_cut_to_the_limit() -> None:
    """A 300-character input bounded at 64 is 64 characters ending in an ellipsis."""
    result = neutralise_bounded("x" * 300, 64)

    assert len(result) == 64
    assert result.endswith(_ELLIPSIS)


def test_neutralise_bounded_long_control_input_holds_no_control() -> None:
    """Cutting happens after neutralising, so no raw control survives the cut."""
    result = neutralise_bounded("\x1b" * 100, 10)

    assert len(result) == 10
    assert not has_control_characters(result)


@pytest.mark.parametrize("limit", [0, -1])
def test_neutralise_bounded_refuses_a_limit_below_one(limit: int) -> None:
    """A limit with no room for the ellipsis is a caller bug, refused loudly."""
    with pytest.raises(ValueError, match="limit"):
        neutralise_bounded("abc", limit)


def test_text_safety_is_a_leaf_that_imports_nothing_from_saneless() -> None:
    """
    Loading the module on its own pulls no ``saneless`` module in.

    ``scanner/base.py`` imports it, so any import back into the package would
    risk a cycle. The module is loaded from its file under a private name in
    a fresh interpreter, so ``sys.modules`` shows only what the module itself
    imports, not the packages a dotted import would load on the way.
    """
    module_path = Path(saneless.__file__).parent / "text_safety.py"
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
