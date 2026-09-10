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
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from saneless.exceptions import StorageError
from saneless.vocabulary import (
    ACTIVE_STATES,
    BUSY_STATES,
    ErrorCategory,
    JobState,
    ScanOutcome,
)

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = ["ErrorCategory", "Job", "JobState", "JobStore"]

logger = logging.getLogger(__name__)

_COLUMNS: tuple[str, ...] = (
    "id",
    "profile",
    "title",
    "state",
    "error",
    "error_category",
    "tags",
    "correspondent",
    "thumbnail",
    "created_at",
    "outcome",
    "pages_scanned",
    "pages_removed",
    "pages_uploaded",
    "warning",
    "owner_token",
)
"""Every column the jobs table carries at the head schema version.

The one place the live column list is spelled.  Every ``SELECT`` and every
``INSERT`` in this module derives its column list and its placeholders from
this tuple, so a column cannot be added to one statement and forgotten in
another -- which is exactly how the ``thumbnail`` column came to exist in
``CREATE TABLE`` while no statement that needed it ever learned about it.
"""

_S3_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        "profile",
        "title",
        "state",
        "error",
        "error_category",
        "tags",
        "correspondent",
        "thumbnail",
        "created_at",
    }
)
"""The ten columns of the schema this project shipped before ``user_version``.

A frozen historical shape, not a description of the current table: it is what
migration step 2 requires to be present before it upgrades a pre-existing
database.  It must never grow when a later step adds a column, or step 2 would
start rejecting the very databases it had already upgraded.
"""

_V2_COLUMNS: tuple[tuple[str, str], ...] = (
    ("outcome", "TEXT"),
    ("pages_scanned", "INTEGER"),
    ("pages_removed", "INTEGER"),
    ("pages_uploaded", "INTEGER"),
    ("warning", "TEXT"),
    ("owner_token", "TEXT"),
)
"""The six ``(name, SQL type)`` pairs migration step 2 adds to the jobs table.

Every one is nullable and carries no ``DEFAULT``.  ``NULL`` means "never
recorded", which is true of every row written before this migration and of
every job that fails before the scanner opens; ``NOT NULL DEFAULT 0`` would
backfill history with a measured zero that never happened.
"""


def _migrate_v1(conn: sqlite3.Connection, db_path: str) -> None:
    """
    Create the jobs table at the schema version 1 shape.

    Args:
        conn: Open connection to the job database.
        db_path: Path the connection was opened on, for logging.

    """
    logger.debug("Creating the jobs table in %s at schema version 1", db_path)
    conn.execute(
        """CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            profile TEXT NOT NULL,
            title TEXT NOT NULL,
            state TEXT NOT NULL,
            error TEXT,
            error_category TEXT,
            tags TEXT NOT NULL,
            correspondent INTEGER,
            thumbnail TEXT,
            created_at TEXT NOT NULL
        )"""
    )


def _migrate_v2(conn: sqlite3.Connection, db_path: str) -> None:
    """
    Add the six result columns to a jobs table at the version 1 shape.

    Args:
        conn: Open connection to the job database.
        db_path: Path the connection was opened on, named in the failure.

    Raises:
        StorageError: If the jobs table is not at the version 1 shape.  The
            check runs before the first ``ALTER``, so a rejected database is
            left exactly as it was found.

    """
    present = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    missing = _S3_COLUMNS - present
    if missing:
        msg = (
            f"job database at {db_path} has an unsupported schema: "
            f"missing column(s) {', '.join(sorted(missing))}"
        )
        raise StorageError(msg)
    for name, sqltype in _V2_COLUMNS:
        # Both interpolated values come from _V2_COLUMNS, a module-level
        # literal no caller can reach, and SQL cannot parameterise a column
        # name or a type name.
        conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {sqltype}")
    logger.debug("Added the version 2 result columns to %s", db_path)


_MIGRATIONS: tuple[Callable[[sqlite3.Connection, str], None], ...] = (
    _migrate_v1,
    _migrate_v2,
)
"""The ordered migration ladder, where ``index + 1`` is the version it produces.

Both steps take the database path as well as the connection so the tuple stays
homogeneous, even though only step 2 has anything to name in a failure.
"""


def _migrate(conn: sqlite3.Connection, db_path: str) -> None:
    """
    Run every outstanding migration step against a job database.

    Args:
        conn: Open connection to the job database.
        db_path: Path the connection was opened on.

    """
    version: int = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        legacy = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
        ).fetchone()
        if legacy is not None:
            # Nothing ever stamped user_version, so an existing jobs table is
            # at version 1 rather than at nothing.
            version = 1
    for index in range(version, len(_MIGRATIONS)):
        _MIGRATIONS[index](conn, db_path)
        # A PRAGMA argument cannot be bound -- "PRAGMA user_version = ?" is a
        # syntax error -- and index comes from range() over a module-level
        # tuple, so no caller-supplied value reaches this string.
        conn.execute(f"PRAGMA user_version = {index + 1}")
        # Commit per step: a ladder that fails at step N leaves a valid
        # database at version N - 1 rather than a half-applied one.
        conn.commit()


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
        error_category: Categorized error type for programmatic handling.
        created_at: Timezone-aware creation timestamp.
        tags: List of paperless-ngx tag IDs.
        correspondent: Optional paperless-ngx correspondent ID.
        thumbnail: Optional base64-encoded JPEG thumbnail string.
        outcome: How the scan resolved, once a scan has recorded one.
        pages_scanned: Pages the scanner produced, once a scan has counted them.
        pages_removed: Pages discarded as blank, once a scan has counted them.
        pages_uploaded: Pages sent to paperless-ngx, once a scan has counted them.
        warning: A note about something odd that did not fail the scan.
        owner_token: The browser token recorded with the submission, if one was.

    """

    id: str
    profile: str
    title: str
    state: JobState = JobState.PENDING
    error: str | None = None
    error_category: ErrorCategory | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    tags: list[int] = field(default_factory=list)
    correspondent: int | None = None
    thumbnail: str | None = None
    outcome: ScanOutcome | None = None
    pages_scanned: int | None = None
    pages_removed: int | None = None
    pages_uploaded: int | None = None
    warning: str | None = None
    owner_token: str | None = None

    @property
    def is_active(self) -> bool:
        """Whether this job is still in flight (not DONE or ERROR)."""
        return self.state in ACTIVE_STATES

    @property
    def is_busy(self) -> bool:
        """Whether the machine is working (active, but not waiting for a human)."""
        return self.state in BUSY_STATES


class JobStore:
    """
    SQLite-backed persistence for scan jobs.

    Args:
        db_path: Path to SQLite database file, or ":memory:" for
            in-memory storage (default).

    """

    def __init__(self, db_path: str = ":memory:") -> None:
        """Open the job store, enable WAL, and run the migration ladder."""
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL has to be enabled before the connection switches to explicit
        # transaction control: that switch opens a transaction immediately,
        # and SQLite refuses a journal-mode change inside a transaction on a
        # file database while silently reporting "memory" on ":memory:".
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.autocommit = False
        try:
            _migrate(self._conn, db_path)
        except Exception:
            # Release the BEGIN DEFERRED the failed ladder still holds, and
            # the file handle with it, before the caller sees the failure.
            self._conn.rollback()
            self._conn.close()
            raise

    def create_job(
        self,
        profile: str,
        title: str,
        tags: list[int] | None = None,
        correspondent: int | None = None,
        thumbnail: str | None = None,
    ) -> Job:
        """
        Create and persist a new job.

        Args:
            profile: Scan profile name.
            title: Document title.
            tags: Optional list of tag IDs.
            correspondent: Optional correspondent ID.
            thumbnail: Optional base64-encoded JPEG thumbnail.

        Returns:
            The newly created Job instance.

        """
        job = Job(
            id=str(uuid.uuid4()),
            profile=profile,
            title=title,
            tags=tags or [],
            correspondent=correspondent,
            thumbnail=thumbnail,
        )
        self._conn.execute(
            "INSERT INTO jobs (id, profile, title, state, error, error_category, "
            "tags, correspondent, thumbnail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                job.id,
                job.profile,
                job.title,
                job.state.value,
                job.error,
                job.error_category.value if job.error_category else None,
                json.dumps(job.tags),
                job.correspondent,
                job.thumbnail,
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
            "SELECT id, profile, title, state, error, error_category, "
            "tags, correspondent, thumbnail, created_at "
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
            error_category=ErrorCategory(row[5]) if row[5] else None,
            tags=json.loads(row[6]),
            correspondent=row[7],
            thumbnail=row[8],
            created_at=datetime.fromisoformat(row[9]),
        )

    def update_state(
        self,
        job_id: str,
        state: JobState,
        error: str | None = None,
        error_category: ErrorCategory | None = None,
    ) -> None:
        """
        Update the state (and optionally error) of a job.

        Args:
            job_id: The UUID string of the job.
            state: New job state.
            error: Optional error message (typically set with ERROR state).
            error_category: Optional error category for programmatic handling.

        """
        self._conn.execute(
            "UPDATE jobs SET state = ?, error = ?, error_category = ? WHERE id = ?",
            (
                state.value,
                error,
                error_category.value if error_category else None,
                job_id,
            ),
        )
        self._conn.commit()
        logger.debug("Job %s -> %s", job_id, state.value)

    def update_thumbnail(self, job_id: str, thumbnail: str) -> None:
        """
        Update the thumbnail of a job.

        Args:
            job_id: The UUID string of the job.
            thumbnail: Base64-encoded JPEG thumbnail string.

        """
        self._conn.execute(
            "UPDATE jobs SET thumbnail = ? WHERE id = ?",
            (thumbnail, job_id),
        )
        self._conn.commit()

    def list_recent(self, limit: int = 50) -> list[Job]:
        """
        Fetch the most recent jobs ordered newest-first.

        Args:
            limit: Maximum number of jobs to return.

        Returns:
            List of Job instances ordered by creation time descending.

        """
        rows = self._conn.execute(
            "SELECT id, profile, title, state, error, error_category, "
            "tags, correspondent, thumbnail, created_at "
            "FROM jobs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            Job(
                id=row[0],
                profile=row[1],
                title=row[2],
                state=JobState(row[3]),
                error=row[4],
                error_category=ErrorCategory(row[5]) if row[5] else None,
                tags=json.loads(row[6]),
                correspondent=row[7],
                thumbnail=row[8],
                created_at=datetime.fromisoformat(row[9]),
            )
            for row in rows
        ]

    def prune(self, max_age_days: int = 7, max_rows: int = 500) -> int:
        """
        Remove old jobs by age and count limits.

        First deletes jobs older than max_age_days, then trims to
        max_rows keeping the most recent entries.

        Args:
            max_age_days: Maximum age in days before a job is pruned.
            max_rows: Maximum number of jobs to retain.

        Returns:
            Total number of jobs deleted.

        """
        before_count = self._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

        cutoff = (datetime.now(tz=UTC) - timedelta(days=max_age_days)).isoformat()
        self._conn.execute(
            "DELETE FROM jobs WHERE created_at < ?",
            (cutoff,),
        )

        self._conn.execute(
            "DELETE FROM jobs WHERE id NOT IN "
            "(SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?)",
            (max_rows,),
        )
        self._conn.commit()

        after_count = self._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

        deleted = before_count - after_count
        if deleted > 0:
            logger.debug("Pruned %d old jobs", deleted)
        return deleted

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()
