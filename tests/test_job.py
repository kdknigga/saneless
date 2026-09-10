"""
JobStore migration, list, prune, and error category tests.

Covers requirements: UI-05, UI-06, PKG-01, STOR-01, STOR-02, STOR-03, STOR-04,
STOR-05.
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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

PUBLIC_METHOD_FLOOR = 7
"""The number of public JobStore methods that exist today.

A floor, not a roster.  The reflective lock-coverage test asserts the
enumeration found at least this many, so a predicate that silently matches
nothing -- the way a reflective test quietly dies -- cannot pass.
"""

STRESS_ROUNDS = 200
"""Rounds of mixed reads and writes each stress worker runs."""

STRESS_WORKERS = 2
"""Threads the stress test runs concurrently against one shared store."""

BARRIER_TIMEOUT = 30.0
"""Seconds a stress worker waits at the start barrier before giving up.

Inside the project's 60 s per-test timeout, so a worker that never arrives
fails the test with a BrokenBarrierError instead of hanging the suite.
"""

STRESS_STATES = (
    JobState.SCANNING,
    JobState.ASSEMBLING,
    JobState.UPLOADING,
    JobState.DONE,
)
"""The states the stress workers cycle through, one per round."""

PRUNE_MAX_AGE_DAYS = 3650
"""A prune age bound generous enough that the stress rounds delete nothing."""

PRUNE_MAX_ROWS = 1_000_000
"""A prune row bound generous enough that the stress rounds delete nothing."""


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


def _job_store_classdef() -> ast.ClassDef:
    """Return the JobStore class definition parsed out of saneless.job."""
    tree = ast.parse(_job_source())
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "JobStore"
    )


def _public_self_calls(method: ast.FunctionDef) -> list[str]:
    """Return the names of every public ``self.<name>(...)`` call in a method."""
    names: list[str] = []
    for node in ast.walk(method):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "self"
            and not func.attr.startswith("_")
        ):
            names.append(func.attr)
    return names


def _stress_worker(
    store: JobStore,
    barrier: threading.Barrier,
    seen: dict[str, JobState],
    guard: threading.Lock,
) -> None:
    """Run STRESS_ROUNDS of mixed JobStore traffic against a shared store."""
    barrier.wait()
    for index in range(STRESS_ROUNDS):
        job = store.create_job(profile="default", title=f"Stress {index}")
        state = STRESS_STATES[index % len(STRESS_STATES)]
        store.update_state(job.id, state)
        store.get_job(job.id)
        store.list_recent(limit=10)
        store.prune(max_age_days=PRUNE_MAX_AGE_DAYS, max_rows=PRUNE_MAX_ROWS)
        with guard:
            seen[job.id] = state


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


class TestLockDiscipline:
    """Lock coverage, the no-public-self-call rule, and the concurrency claim."""

    def test_locked_coverage_spans_every_public_method(self) -> None:
        """Every public JobStore method carries the lock marker (STOR-01)."""
        # Blind spot, recorded deliberately: inspect.isfunction does not see a
        # public @property.  JobStore has none today; if one is ever added this
        # test will not notice that it is unserialised.
        public = [
            (name, member)
            for name, member in inspect.getmembers(JobStore, inspect.isfunction)
            if not name.startswith("_")
        ]
        assert len(public) >= PUBLIC_METHOD_FLOOR, (
            f"the enumeration found only {len(public)} public methods; a "
            f"reflective test that matches nothing passes vacuously"
        )
        # No opt-out list -- not for close(), not for anything.  A hand-written
        # roster of excused names is where the next one gets quietly added.
        unlocked = sorted(
            name
            for name, member in public
            if getattr(member, job_module._LOCKED_MARKER, False) is not True
        )
        assert unlocked == [], (
            f"public JobStore methods missing the @_locked marker: "
            f"{', '.join(unlocked)}"
        )

    def test_no_public_method_calls_another_public_method(self) -> None:
        """No public JobStore method calls another public method (STOR-01)."""
        # A correctness rule, not style: sqlite3 connection context managers do
        # not nest, so an inner `with conn:` commits the OUTER transaction and
        # half the caller's work lands early.  RLock re-entrancy prevents the
        # deadlock; nothing prevents the incorrectness, and ruff cannot see it.
        store = _job_store_classdef()
        offenders = [
            f"{method.name} -> {callee}"
            for method in store.body
            if isinstance(method, ast.FunctionDef) and not method.name.startswith("_")
            for callee in _public_self_calls(method)
        ]
        assert offenders == [], (
            f"public JobStore methods calling other public methods: "
            f"{', '.join(offenders)}"
        )

    def test_stress_two_threads_survive_two_hundred_rounds(self) -> None:
        """Two threads run 200 rounds of mixed traffic uncorrupted (STOR-01)."""
        store = JobStore()
        seen: dict[str, JobState] = {}
        guard = threading.Lock()
        barrier = threading.Barrier(STRESS_WORKERS, timeout=BARRIER_TIMEOUT)
        try:
            with ThreadPoolExecutor(max_workers=STRESS_WORKERS) as pool:
                futures = [
                    pool.submit(_stress_worker, store, barrier, seen, guard)
                    for _ in range(STRESS_WORKERS)
                ]
                for future in futures:
                    # Future.result() re-raises the worker's exception here.  A
                    # raw threading.Thread swallows it into threading.excepthook
                    # and this test would pass green on a broken store.
                    future.result()

            # 1. Every insert committed exactly once -- none lost, none doubled.
            assert len(seen) == STRESS_WORKERS * STRESS_ROUNDS

            # 2. Each job reads back with the last state its own thread set.  A
            #    half-committed UPDATE surfaces here as a stale state.
            for job_id, expected in seen.items():
                fetched = store.get_job(job_id)
                assert fetched is not None
                assert fetched.state == expected
                # 4. Deserialisation succeeded rather than incidentally.
                assert isinstance(fetched.state, JobState)
                assert isinstance(fetched.tags, list)

            # 3. The row count reconciles with what the workers recorded.
            listed = store.list_recent(limit=STRESS_WORKERS * STRESS_ROUNDS * 2)
            assert len(listed) == len(seen)
            for listed_job in listed:
                assert isinstance(listed_job.state, JobState)
                assert isinstance(listed_job.tags, list)
        finally:
            store.close()
