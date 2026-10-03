"""
The web route tests fail under the mutants they exist to catch.

Each check copies the repository, confirms the named test passes on the clean
copy, applies one hand-written mutant and confirms the same test then fails.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.mutants.harness import Edit, check_mutant

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.mutant

_ROUTES = "src/saneless/web/routes.py"
_STATUS_TEMPLATE = "src/saneless/web/templates/partials/status.html"


def test_owner_cookie_entropy_test_kills_a_constant_token(tmp_path: Path) -> None:
    """The owner-cookie entropy test fails when every browser gets one fixed token."""
    check_mutant(
        tmp_path,
        "tests/test_web.py::TestOwnerCookie::"
        "test_owner_cookie_carries_at_least_32_bytes_of_entropy",
        [Edit(_ROUTES, "secrets.token_urlsafe(32)", '"a" * 43')],
    )


@pytest.mark.parametrize(
    "edit",
    [
        Edit(
            _STATUS_TEMPLATE,
            'hx-get="{{ poll_url }}"',
            'hx-get="/api/jobs/current"',
        ),
        Edit(
            _STATUS_TEMPLATE,
            'hx-trigger="every {{ poll_interval }}s"',
            'hx-trigger="every 5s"',
        ),
    ],
    ids=["url", "interval"],
)
def test_status_poll_test_kills_a_moved_poll(tmp_path: Path, edit: Edit) -> None:
    """The status-poll test fails when the poll goes to another URL or interval."""
    check_mutant(tmp_path, "tests/test_web.py::test_status_polling_active_job", [edit])
