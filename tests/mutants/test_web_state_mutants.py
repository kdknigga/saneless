"""
The state-rendering tests fail under the mutants they exist to catch.

Each mutant deletes the one thing a test is named for, in the one place it
lives, and the named test must fail in a copy of the repository carrying it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.mutants.harness import Edit, check_mutant

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.mutant

_APP_CSS = "src/saneless/web/static/app.css"
_SCAN_BUTTON = "src/saneless/web/templates/partials/scan_button.html"
_RENDERING = "tests/test_web_state_rendering.py"


def test_the_touch_target_test_fails_when_the_summary_loses_its_min_height(
    tmp_path: Path,
) -> None:
    """The disclosure summary's touch-target test fails without its min-height."""
    check_mutant(
        tmp_path,
        f"{_RENDERING}::TestStatusAreaError::"
        "test_the_summary_clears_the_touch_target_floor",
        [
            Edit(
                _APP_CSS,
                "details.tech-details > summary {\n"
                "    display: flex;\n"
                "    align-items: center;\n"
                "    min-height: 2.75rem;\n"
                "}",
                "details.tech-details > summary {\n"
                "    display: flex;\n"
                "    align-items: center;\n"
                "}",
            )
        ],
    )


def test_the_muted_pages_test_fails_when_the_page_counts_rule_is_emptied(
    tmp_path: Path,
) -> None:
    """The page-counts rule test fails when the rule loses its block and colour."""
    check_mutant(
        tmp_path,
        f"{_RENDERING}::TestPageCounts::test_the_stylesheet_gains_one_muted_pages_rule",
        [
            Edit(
                _APP_CSS,
                ".page-counts {\n"
                "    display: block;\n"
                "    color: var(--pico-muted-color);\n"
                "}",
                ".page-counts {\n}",
            )
        ],
    )


def test_the_scan_button_test_fails_when_the_button_is_never_disabled(
    tmp_path: Path,
) -> None:
    """The scan-button state test fails when an active job leaves Scan enabled."""
    check_mutant(
        tmp_path,
        f"{_RENDERING}::test_scan_button_disabled_and_busy_split",
        [Edit(_SCAN_BUTTON, "{% if scan_disabled %}disabled{% endif %}", "")],
    )
