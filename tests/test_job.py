"""
JobStore list, prune, and error category tests.

Covers requirements: UI-05, UI-06, PKG-01.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from saneless.job import ErrorCategory, Job, JobState, JobStore

if TYPE_CHECKING:
    from pathlib import Path


def test_list_recent() -> None:
    """List recent jobs returns entries in reverse chronological order (UI-05)."""
    store = JobStore()
    try:
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
    finally:
        store.close()


def test_list_recent_empty() -> None:
    """List recent jobs returns empty list when no jobs exist (UI-05)."""
    store = JobStore()
    try:
        jobs = store.list_recent()
        assert jobs == []
    finally:
        store.close()


def test_prune_by_age() -> None:
    """Prune removes jobs older than specified age (UI-06)."""
    store = JobStore()
    try:
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
    finally:
        store.close()


def test_prune_by_count() -> None:
    """Prune keeps only the N most recent jobs (UI-06)."""
    store = JobStore()
    try:
        for i in range(5):
            store.create_job(profile="default", title=f"Job {i}")
            time.sleep(0.01)

        store.prune(max_age_days=365, max_rows=3)
        remaining = store.list_recent()
        assert len(remaining) == 3
    finally:
        store.close()


def test_prune_no_deletions() -> None:
    """Prune with no matching criteria deletes nothing (UI-06)."""
    store = JobStore()
    try:
        store.create_job(profile="default", title="Job A")
        store.create_job(profile="default", title="Job B")

        deleted = store.prune(max_age_days=365, max_rows=500)
        assert deleted == 0
    finally:
        store.close()


class TestErrorCategory:
    """ErrorCategory enum and JobStore integration tests."""

    def test_error_category_enum_values(self) -> None:
        """ErrorCategory has all five expected values."""
        assert ErrorCategory.FEEDER == "FEEDER"
        assert ErrorCategory.CONFIG == "CONFIG"
        assert ErrorCategory.SCANNER == "SCANNER"
        assert ErrorCategory.UPLOAD == "UPLOAD"
        assert ErrorCategory.UNKNOWN == "UNKNOWN"

    def test_job_error_category_defaults_none(self) -> None:
        """Job.error_category field defaults to None."""
        job = Job(id="test", profile="default", title="Test")
        assert job.error_category is None

    def test_jobstore_persists_error_category(self) -> None:
        """JobStore persists and retrieves error_category from SQLite."""
        store = JobStore()
        try:
            job = store.create_job("default", "Cat Test")
            store.update_state(
                job.id,
                JobState.ERROR,
                error="boom",
                error_category=ErrorCategory.SCANNER,
            )
            fetched = store.get_job(job.id)
            assert fetched is not None
            assert fetched.error_category == ErrorCategory.SCANNER
        finally:
            store.close()

    def test_jobstore_migration_adds_column(self, tmp_path: Path) -> None:
        """Opening a pre-existing DB without error_category column succeeds."""
        db_path = str(tmp_path / "migrate.db")
        # Create old-schema DB
        conn = sqlite3.connect(db_path)
        conn.execute(
            """CREATE TABLE jobs (
            id TEXT PRIMARY KEY, profile TEXT NOT NULL, title TEXT NOT NULL,
            state TEXT NOT NULL, error TEXT, tags TEXT NOT NULL,
            correspondent INTEGER, thumbnail TEXT, created_at TEXT NOT NULL
        )"""
        )
        conn.commit()
        conn.close()
        # Open with new JobStore -- should add error_category column
        store = JobStore(db_path=db_path)
        try:
            job = store.create_job("default", "Migration Test")
            store.update_state(
                job.id,
                JobState.ERROR,
                error="test",
                error_category=ErrorCategory.CONFIG,
            )
            fetched = store.get_job(job.id)
            assert fetched is not None
            assert fetched.error_category == ErrorCategory.CONFIG
        finally:
            store.close()
