"""
Xfail test stubs for JobStore list and prune operations (Phase 3).

Covers requirements: UI-05, UI-06.
"""

import pytest


@pytest.mark.xfail(reason="stub -- implemented in 03-01")
def test_list_recent() -> None:
    """List recent jobs returns entries in reverse chronological order (UI-05)."""
    pytest.fail("Not implemented")


@pytest.mark.xfail(reason="stub -- implemented in 03-01")
def test_list_recent_empty() -> None:
    """List recent jobs returns empty list when no jobs exist (UI-05)."""
    pytest.fail("Not implemented")


@pytest.mark.xfail(reason="stub -- implemented in 03-01")
def test_prune_by_age() -> None:
    """Prune removes jobs older than specified age (UI-06)."""
    pytest.fail("Not implemented")


@pytest.mark.xfail(reason="stub -- implemented in 03-01")
def test_prune_by_count() -> None:
    """Prune keeps only the N most recent jobs (UI-06)."""
    pytest.fail("Not implemented")


@pytest.mark.xfail(reason="stub -- implemented in 03-01")
def test_prune_no_deletions() -> None:
    """Prune with no matching criteria deletes nothing (UI-06)."""
    pytest.fail("Not implemented")
