"""
Job model with state machine and SQLite persistence.

Tracks scan jobs through their lifecycle (PENDING -> SCANNING ->
ASSEMBLING -> UPLOADING -> DONE) with SQLite-backed persistence
for crash recovery and history.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

__all__ = ["Job", "JobState", "JobStore"]

logger = logging.getLogger(__name__)


class JobState(StrEnum):
    """States in the scan job lifecycle."""

    PENDING = "PENDING"
    SCANNING = "SCANNING"
    ASSEMBLING = "ASSEMBLING"
    UPLOADING = "UPLOADING"
    DONE = "DONE"
    ERROR = "ERROR"


@dataclass
class Job:
    """
    A scan job with metadata and state tracking.

    Attributes:
        id: Unique job identifier (UUID).
        profile: Name of the scan profile to use.
        title: Document title for paperless-ngx.
        state: Current job lifecycle state.
        error: Error message if state is ERROR.
        created_at: Timezone-aware creation timestamp.
        tags: List of paperless-ngx tag IDs.
        correspondent: Optional paperless-ngx correspondent ID.

    """

    id: str
    profile: str
    title: str
    state: JobState = JobState.PENDING
    error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    tags: list[int] = field(default_factory=list)
    correspondent: int | None = None


class JobStore:
    """
    SQLite-backed persistence for scan jobs.

    Args:
        db_path: Path to SQLite database file, or ":memory:" for
            in-memory storage (default).

    """

    def __init__(self, db_path: str = ":memory:") -> None:
        """Initialize the job store with a SQLite database connection."""
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                profile TEXT NOT NULL,
                title TEXT NOT NULL,
                state TEXT NOT NULL,
                error TEXT,
                tags TEXT NOT NULL,
                correspondent INTEGER,
                created_at TEXT NOT NULL
            )"""
        )
        self._conn.commit()

    def create_job(
        self,
        profile: str,
        title: str,
        tags: list[int] | None = None,
        correspondent: int | None = None,
    ) -> Job:
        """
        Create and persist a new job.

        Args:
            profile: Scan profile name.
            title: Document title.
            tags: Optional list of tag IDs.
            correspondent: Optional correspondent ID.

        Returns:
            The newly created Job instance.

        """
        job = Job(
            id=str(uuid.uuid4()),
            profile=profile,
            title=title,
            tags=tags or [],
            correspondent=correspondent,
        )
        self._conn.execute(
            "INSERT INTO jobs (id, profile, title, state, error, tags, correspondent, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job.id,
                job.profile,
                job.title,
                job.state.value,
                job.error,
                json.dumps(job.tags),
                job.correspondent,
                job.created_at.isoformat(),
            ),
        )
        self._conn.commit()
        logger.debug("Created job %s: %s", job.id, job.title)
        return job

    def get_job(self, job_id: str) -> Job | None:
        """
        Fetch a job by ID.

        Args:
            job_id: The UUID string of the job.

        Returns:
            The Job if found, None otherwise.

        """
        row = self._conn.execute(
            "SELECT id, profile, title, state, error, tags, correspondent, created_at "
            "FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return Job(
            id=row[0],
            profile=row[1],
            title=row[2],
            state=JobState(row[3]),
            error=row[4],
            tags=json.loads(row[5]),
            correspondent=row[6],
            created_at=datetime.fromisoformat(row[7]),
        )

    def update_state(
        self,
        job_id: str,
        state: JobState,
        error: str | None = None,
    ) -> None:
        """
        Update the state (and optionally error) of a job.

        Args:
            job_id: The UUID string of the job.
            state: New job state.
            error: Optional error message (typically set with ERROR state).

        """
        self._conn.execute(
            "UPDATE jobs SET state = ?, error = ? WHERE id = ?",
            (state.value, error, job_id),
        )
        self._conn.commit()
        logger.debug("Job %s -> %s", job_id, state.value)

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
