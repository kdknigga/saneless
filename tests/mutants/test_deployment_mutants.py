"""
The deployment tests fail under the mutants they exist to catch.

Each check here breaks one fact a deployment test is named for -- the check
count a page states, the build context's allow-list, a workflow step that
swallows a failure -- in a copy of the repository, and asserts the named test
fails there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.mutants.harness import Edit, check_mutant

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.mutant

_DEPLOYMENT_TESTS = "tests/test_deployment_config.py"
_CHECK_COUNT_TEST = (
    f"{_DEPLOYMENT_TESTS}::test_docs_that_list_the_checks_name_every_check"
)
_BUILD_CONTEXT_TEST = (
    f"{_DEPLOYMENT_TESTS}::test_dockerignore_reincludes_everything_the_build_needs"
)
_SUPPRESSION_TEST = (
    f"{_DEPLOYMENT_TESTS}::test_no_workflow_or_hook_file_silences_a_checker_with_a_flag"
)

_CI_WORKFLOW = ".github/workflows/ci.yml"
# The lint job's first hook run. It occurs once in the workflow.
_LINT_STEP = "      - run: uv run prek run --all-files --show-diff-on-failure\n"


def test_a_page_that_miscounts_the_checks_fails_the_check_count_test(
    tmp_path: Path,
) -> None:
    """A page counting one check too many fails the check-count test."""
    check_mutant(
        tmp_path,
        _CHECK_COUNT_TEST,
        [
            Edit(
                "docs/how-to/cli-scripting.md",
                "six health checks",
                "seven health checks",
            )
        ],
    )


def test_dropping_the_lock_from_the_build_context_fails_its_test(
    tmp_path: Path,
) -> None:
    """A build context without ``uv.lock`` fails the allow-list test."""
    check_mutant(
        tmp_path,
        _BUILD_CONTEXT_TEST,
        [Edit(".dockerignore", "!uv.lock\n", "")],
    )


def test_an_exit_zero_lint_step_fails_the_suppression_test(tmp_path: Path) -> None:
    """A lint step told to exit 0 over its findings fails the suppression test."""
    check_mutant(
        tmp_path,
        _SUPPRESSION_TEST,
        [
            Edit(
                _CI_WORKFLOW,
                _LINT_STEP,
                _LINT_STEP.replace("\n", " --exit-zero\n"),
            )
        ],
    )


def test_a_lint_step_allowed_to_fail_fails_the_suppression_test(
    tmp_path: Path,
) -> None:
    """A lint step whose failure marks the step passed fails the suppression test."""
    check_mutant(
        tmp_path,
        _SUPPRESSION_TEST,
        [
            Edit(
                _CI_WORKFLOW,
                _LINT_STEP,
                f"{_LINT_STEP}        continue-on-error: true\n",
            )
        ],
    )


def test_a_lint_step_that_swallows_its_status_fails_the_suppression_test(
    tmp_path: Path,
) -> None:
    """A lint step whose exit status is swallowed fails the suppression test."""
    check_mutant(
        tmp_path,
        _SUPPRESSION_TEST,
        [
            Edit(
                _CI_WORKFLOW,
                _LINT_STEP,
                _LINT_STEP.replace("\n", " || true\n"),
            )
        ],
    )
