"""
JobStore migration, list, prune, and error category tests.

Covers requirements: UI-05, UI-06, PKG-01, STOR-01, STOR-02, STOR-03, STOR-04,
STOR-05.
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from saneless import job as job_module
from saneless.exceptions import StorageError
from saneless.job import ErrorCategory, Job, JobState, JobStore
from saneless.vocabulary import ScanOutcome

if TYPE_CHECKING:
    from pathlib import Path

HEAD_VERSION = 2
"""The schema version a fully migrated job database reports."""

HEAD_COLUMN_COUNT = 16
"""The number of columns the jobs table carries at HEAD_VERSION."""

S2_COLUMNS = (
    "id",
    "profile",
    "title",
    "state",
    "error",
    "tags",
    "correspondent",
    "thumbnail",
    "created_at",
)
"""The nine columns of the 5bd6158 (S2) shape -- no error_category."""

V2_COLUMNS = (
    "outcome",
    "pages_scanned",
    "pages_removed",
    "pages_uploaded",
    "warning",
    "owner_token",
)
"""The six columns migration step 2 adds, spelled out independently of job.py."""


def _build_s3_schema(db_path: str) -> None:
    """Write a jobs table at the dc9b8af (S3) ten-column shape holding one row."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """CREATE TABLE jobs (
            id TEXT PRIMARY KEY, profile TEXT NOT NULL, title TEXT NOT NULL,
            state TEXT NOT NULL, error TEXT, error_category TEXT,
            tags TEXT NOT NULL, correspondent INTEGER, thumbnail TEXT,
            created_at TEXT NOT NULL
        )"""
        )
        conn.execute(
            "INSERT INTO jobs (id, profile, title, state, error, error_category, "
            "tags, correspondent, thumbnail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-1",
                "default",
                "Legacy Doc",
                JobState.DONE.value,
                None,
                None,
                "[]",
                None,
                None,
                datetime.now(tz=UTC).isoformat(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _build_s2_schema(db_path: str) -> None:
    """Write a jobs table at the 5bd6158 (S2) shape, which has no error_category."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """CREATE TABLE jobs (
            id TEXT PRIMARY KEY, profile TEXT NOT NULL, title TEXT NOT NULL,
            state TEXT NOT NULL, error TEXT, tags TEXT NOT NULL,
            correspondent INTEGER, thumbnail TEXT, created_at TEXT NOT NULL
        )"""
        )
        conn.commit()
    finally:
        conn.close()


def _read_schema(conn: sqlite3.Connection) -> tuple[int, list[str]]:
    """Return the (user_version, column names) a connection reports for jobs."""
    version: int = conn.execute("PRAGMA user_version").fetchone()[0]
    columns = [row[1] for row in conn.execute("PRAGMA table_info(jobs)")]
    return version, columns


def _job_source() -> str:
    """Return the source text of saneless.job for the structural assertions."""
    return inspect.getsource(job_module)


def _count_job_constructions(node: ast.AST) -> int:
    """Count ``Job(...)`` constructor calls anywhere beneath an AST node."""
    return sum(
        1
        for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == "Job"
    )


def _read_schema_raw(db_path: str) -> tuple[int, list[str]]:
    """Return the schema a fresh raw connection sees, bypassing JobStore."""
    conn = sqlite3.connect(db_path)
    try:
        return _read_schema(conn)
    finally:
        conn.close()


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


class TestMigrationLadder:
    """PRAGMA user_version migration ladder tests."""

    def test_fresh_store_runs_the_whole_migration(self) -> None:
        """A fresh in-memory store opens at the head schema version (STOR-02)."""
        store = JobStore()
        try:
            version, columns = _read_schema(store._conn)
            assert version == HEAD_VERSION
            assert len(columns) == HEAD_COLUMN_COUNT
        finally:
            store.close()

    def test_migration_legacy_s3_database_joins_the_ladder(
        self, tmp_path: Path
    ) -> None:
        """A legacy S3 database migrates to head and keeps its rows (STOR-02)."""
        db_path = str(tmp_path / "legacy.db")
        _build_s3_schema(db_path)

        store = JobStore(db_path=db_path)
        try:
            version, columns = _read_schema(store._conn)
            assert version == HEAD_VERSION
            assert len(columns) == HEAD_COLUMN_COUNT
            assert set(V2_COLUMNS) <= set(columns)

            legacy = store.get_job("legacy-1")
            assert legacy is not None
            assert legacy.title == "Legacy Doc"
        finally:
            store.close()

    def test_migration_guard_rejects_an_s2_database(self, tmp_path: Path) -> None:
        """An S2 database raises StorageError and is left untouched (STOR-02)."""
        db_path = str(tmp_path / "s2.db")
        _build_s2_schema(db_path)

        with pytest.raises(StorageError, match="error_category") as exc_info:
            JobStore(db_path=db_path)

        assert db_path in str(exc_info.value)

        version, columns = _read_schema_raw(db_path)
        assert version == 0
        assert columns == list(S2_COLUMNS)

    def test_migration_idempotent_on_reopen(self, tmp_path: Path) -> None:
        """Reopening an already-migrated database changes nothing (STOR-03)."""
        db_path = str(tmp_path / "reopen.db")

        store = JobStore(db_path=db_path)
        job_id = store.create_job("default", "Persistent Doc").id
        store.close()

        reopened = JobStore(db_path=db_path)
        try:
            version, columns = _read_schema(reopened._conn)
            assert version == HEAD_VERSION
            assert len(columns) == HEAD_COLUMN_COUNT

            fetched = reopened.get_job(job_id)
            assert fetched is not None
            assert fetched.title == "Persistent Doc"
        finally:
            reopened.close()

    def test_journal_mode_is_wal_on_a_file_database(self, tmp_path: Path) -> None:
        """
        A file-backed store reads back WAL journalling (STOR-02).

        WAL must be set before the connection flips to explicit transaction
        control: SQLite refuses the switch inside a transaction on a file
        database while returning "memory" without error on ":memory:", so an
        in-memory-only suite cannot see the ordering mistake.
        """
        db_path = str(tmp_path / "wal.db")
        store = JobStore(db_path=db_path)
        try:
            mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode == "wal"
        finally:
            store.close()


class TestResultColumns:
    """The six result columns, the one column list, and the one row mapping."""

    def test_job_result_columns_default_to_none(self) -> None:
        """A bare Job exposes all six result columns, every one None (STOR-03)."""
        job = Job(id="test", profile="default", title="Test")
        assert job.outcome is None
        assert job.pages_scanned is None
        assert job.pages_removed is None
        assert job.pages_uploaded is None
        assert job.warning is None
        assert job.owner_token is None

    def test_created_job_result_columns_stay_none(self) -> None:
        """Nothing in this phase writes the six result columns (STOR-03)."""
        store = JobStore()
        try:
            created = store.create_job("default", "Unwritten")
            fetched = store.get_job(created.id)
            assert fetched is not None
            assert fetched.outcome is None
            assert fetched.pages_scanned is None
            assert fetched.pages_removed is None
            assert fetched.pages_uploaded is None
            assert fetched.warning is None
            assert fetched.owner_token is None
        finally:
            store.close()

    def test_outcome_roundtrip_preserves_value_and_python_type(self) -> None:
        """Each result column reads back with its value and its type (STOR-03)."""
        store = JobStore()
        try:
            created = store.create_job("default", "Result Doc")
            store._conn.execute(
                "UPDATE jobs SET outcome = ?, pages_scanned = ?, pages_removed = ?, "
                "pages_uploaded = ?, warning = ?, owner_token = ? WHERE id = ?",
                (
                    ScanOutcome.SUCCESS.value,
                    12,
                    1,
                    11,
                    "front/back mismatch",
                    "tok-abc",
                    created.id,
                ),
            )
            store._conn.commit()

            fetched = store.get_job(created.id)
            assert fetched is not None
            # The value AND the Python type: sqlite3.Row.__getitem__ is typed
            # Any, so neither ty nor pyrefly can catch a column that comes back
            # as the wrong type.  These assertions are the only control.
            assert fetched.outcome is ScanOutcome.SUCCESS
            assert isinstance(fetched.outcome, ScanOutcome)
            assert fetched.pages_scanned == 12
            assert isinstance(fetched.pages_scanned, int)
            assert fetched.pages_removed == 1
            assert isinstance(fetched.pages_removed, int)
            assert fetched.pages_uploaded == 11
            assert isinstance(fetched.pages_uploaded, int)
            assert fetched.warning == "front/back mismatch"
            assert isinstance(fetched.warning, str)
            assert fetched.owner_token == "tok-abc"
            assert isinstance(fetched.owner_token, str)
        finally:
            store.close()

    def test_outcome_roundtrip_of_a_null_column_is_none(self) -> None:
        """A NULL outcome reads back as None, not as a ScanOutcome (STOR-03)."""
        store = JobStore()
        try:
            created = store.create_job("default", "Null Outcome")
            store._conn.execute(
                "UPDATE jobs SET outcome = NULL WHERE id = ?",
                (created.id,),
            )
            store._conn.commit()

            fetched = store.get_job(created.id)
            assert fetched is not None
            assert fetched.outcome is None
            assert not isinstance(fetched.outcome, ScanOutcome)
        finally:
            store.close()

    def test_outcome_roundtrip_survives_list_recent(self) -> None:
        """list_recent uses the same mapping get_job does (STOR-04)."""
        store = JobStore()
        try:
            created = store.create_job("default", "Listed Doc")
            store._conn.execute(
                "UPDATE jobs SET outcome = ?, pages_scanned = ? WHERE id = ?",
                (ScanOutcome.FALLBACK.value, 7, created.id),
            )
            store._conn.commit()

            listed = store.list_recent()
            assert len(listed) == 1
            assert listed[0].outcome is ScanOutcome.FALLBACK
            assert listed[0].pages_scanned == 7
            assert isinstance(listed[0].pages_scanned, int)
        finally:
            store.close()

    def test_single_mapping_column_list_matches_the_live_schema(self) -> None:
        """_COLUMNS is exactly the fresh table's column set (STOR-04)."""
        assert len(job_module._COLUMNS) == HEAD_COLUMN_COUNT
        store = JobStore()
        try:
            _version, columns = _read_schema(store._conn)
            assert set(job_module._COLUMNS) == set(columns)
        finally:
            store.close()

    def test_single_mapping_ladder_reconciles_with_the_column_list(self) -> None:
        """The ladder and the live column list cannot drift apart (STOR-04)."""
        ladder = job_module._S3_COLUMNS | {
            name for name, _sqltype in job_module._V2_COLUMNS
        }
        assert ladder == set(job_module._COLUMNS)

    def test_single_mapping_has_no_handwritten_select_list(self) -> None:
        """No hand-written SELECT column list survives in job.py (STOR-04)."""
        assert "SELECT id, profile" not in _job_source()

    def test_single_mapping_defines_exactly_one_row_to_job(self) -> None:
        """Exactly one function converts a database row into a Job (STOR-04)."""
        tree = ast.parse(_job_source())
        definitions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == "_row_to_job"
        ]
        assert len(definitions) == 1

    def test_single_mapping_constructs_a_job_in_one_place(self) -> None:
        """JobStore builds a Job only inside _row_to_job (STOR-04)."""
        tree = ast.parse(_job_source())
        store = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == "JobStore"
        )
        row_to_job = next(
            node
            for node in store.body
            if isinstance(node, ast.FunctionDef) and node.name == "_row_to_job"
        )
        assert _count_job_constructions(store) == 1
        assert _count_job_constructions(row_to_job) == 1
