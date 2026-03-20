"""
JobStore list and prune operation tests.

Covers requirements: UI-05, UI-06.
"""

import time
from datetime import UTC, datetime, timedelta

from saneless.job import JobStore


def test_list_recent() -> None:
    """List recent jobs returns entries in reverse chronological order (UI-05)."""
    store = JobStore()
    titles = [f"Job {i}" for i in range(5)]
    for title in titles:
        store.create_job(profile="default", title=title)
        time.sleep(0.01)  # Ensure distinct timestamps

    jobs = store.list_recent(limit=3)
    assert len(jobs) == 3
    # Most recent first
    assert jobs[0].title == "Job 4"
    assert jobs[1].title == "Job 3"
    assert jobs[2].title == "Job 2"


def test_list_recent_empty() -> None:
    """List recent jobs returns empty list when no jobs exist (UI-05)."""
    store = JobStore()
    jobs = store.list_recent()
    assert jobs == []


def test_prune_by_age() -> None:
    """Prune removes jobs older than specified age (UI-06)."""
    store = JobStore()
    old_job = store.create_job(profile="default", title="Old Job")
    # Backdate via raw SQL
    old_date = (datetime.now(tz=UTC) - timedelta(days=10)).isoformat()
    store._conn.execute(
        "UPDATE jobs SET created_at = ? WHERE id = ?",
        (old_date, old_job.id),
    )
    store._conn.commit()

    store.create_job(profile="default", title="Fresh Job")

    deleted = store.prune(max_age_days=7, max_rows=500)
    assert deleted >= 1
    remaining = store.list_recent()
    assert len(remaining) == 1
    assert remaining[0].title == "Fresh Job"


def test_prune_by_count() -> None:
    """Prune keeps only the N most recent jobs (UI-06)."""
    store = JobStore()
    for i in range(5):
        store.create_job(profile="default", title=f"Job {i}")
        time.sleep(0.01)

    store.prune(max_age_days=365, max_rows=3)
    remaining = store.list_recent()
    assert len(remaining) == 3


def test_prune_no_deletions() -> None:
    """Prune with no matching criteria deletes nothing (UI-06)."""
    store = JobStore()
    store.create_job(profile="default", title="Job A")
    store.create_job(profile="default", title="Job B")

    deleted = store.prune(max_age_days=365, max_rows=500)
    assert deleted == 0
