"""The check-strip tests fail under the mutants they exist to catch."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.mutants.harness import Edit, check_mutant

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.mutant

_FAILED_RE_RUN = (
    "tests/test_web_checks.py::TestTheStripSurvivesItsOwnFailure::"
    "test_a_failing_re_run_leaves_the_strip_as_it_stood"
)


def test_a_failed_re_run_that_blanks_the_strip_is_caught(tmp_path: Path) -> None:
    """The failed-click test fails when a raising re-run empties the cache."""
    check_mutant(
        tmp_path,
        _FAILED_RE_RUN,
        [
            Edit(
                "src/saneless/web/refresher.py",
                '            logger.exception("Check refresh failed; keeping the '
                'previous results")\n',
                "            self._cache.store(())\n"
                '            logger.exception("Check refresh failed; keeping the '
                'previous results")\n',
            )
        ],
    )
