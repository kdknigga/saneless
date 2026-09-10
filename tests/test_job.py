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
from saneless.vocabulary import ACTIVE_STATES, TERMINAL_STATES, ScanOutcome

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

SHUFFLE_ROWS = 60
"""Jobs the shuffled-order prune tests insert."""

SHUFFLE_KEEP = 30
"""Jobs the shuffled-order prune tests ask prune() to retain."""

SHUFFLE_EXPIRED = 20
"""Rows the second shuffled-order case backdates past its age cutoff."""

SHUFFLE_STRIDE = 37
"""The step of the fixed permutation the shuffled-order tests apply.

Coprime with SHUFFLE_ROWS, so ``(index * SHUFFLE_STRIDE) % SHUFFLE_ROWS`` walks
every rank exactly once and no two rows share a created_at.  Written out rather
than drawn from ``random``: an ordering test that shuffles differently on every
run cannot be debugged when it fails, and a flaky ordering test is worse than
no ordering test at all.
"""

SHUFFLE_AGE_DAYS = 7
"""The age cutoff the second shuffled-order case prunes against."""

SHUFFLE_OLD_DAYS = 30
"""How far back the backdated rows are placed -- well past SHUFFLE_AGE_DAYS."""

SHUFFLE_RECENT_HOURS = 2
"""How far back the non-expired rows start, before their per-rank minute offsets."""

SAFE_AGE_DAYS = 365
"""An age bound generous enough that a prune under it deletes nothing by age."""

CONCURRENT_ROWS = 20
"""Jobs the concurrent prune tests insert before racing a create_job against prune."""

CONCURRENT_MAX_ROWS = 5
"""The row cap the concurrent prune tests prune against -- below CONCURRENT_ROWS."""

RACERS = 2
"""Threads the concurrent prune test starts on its barrier: one prune, one insert."""

TRANSACTION_VERBS = frozenset(
    {"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"}
)
"""Leading SQL verbs that open or close a transaction rather than touch a row.

``Connection.set_trace_callback`` reports the implicit BEGIN and COMMIT that
``with conn:`` issues alongside the statements the method itself runs.  These
are what the single-statement assertion filters out.
"""

RESTART_REASON = "Interrupted by restart"
"""The reason ``fail_active_jobs()`` records when the caller names none.

Spelled out here rather than imported from ``job.py``, so a change to that
default surfaces as a visible test failure instead of a constant that silently
agrees with whatever the source now says.
"""

CUSTOM_REASON = "server restarted"
"""The caller-supplied reason the custom-reason case passes.

STOR-05's own prose asks for "a 'server restarted' reason" while M-03's
prescription is the ``Interrupted by restart`` default; a defaulted parameter
satisfies both, and this constant is what proves the parameter is honoured.
"""

QUEUE_ROWS = 6
"""Jobs the ``list_pending()`` ordering case creates."""

QUEUE_MOVED = (1, 3)
"""Insertion indices the ordering case moves out of PENDING.

One goes to an active non-PENDING state and one to a terminal state, so the
case rules out both "every active job" and "every job" as the predicate.
"""

QUEUE_BASE_HOURS = 1
"""How far back the ordering case's oldest queued job is backdated."""


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


def _shuffled_stamps(count: int, expired: int) -> list[datetime]:
    """
    Build count ascending UTC timestamps, the oldest ``expired`` of them long past.

    Every value is UTC, so its ISO-8601 rendering ends ``+00:00``.  Both the
    prune cutoff comparison and its ORDER BY are lexicographic over these
    strings and are correct only under that uniformity.

    Args:
        count: How many timestamps to build.
        expired: How many of the oldest sit beyond SHUFFLE_AGE_DAYS.

    Returns:
        The timestamps in ascending order, index 0 being the oldest.

    """
    now = datetime.now(tz=UTC)
    old_base = now - timedelta(days=SHUFFLE_OLD_DAYS)
    recent_base = now - timedelta(hours=SHUFFLE_RECENT_HOURS)
    return [
        (old_base if rank < expired else recent_base) + timedelta(minutes=rank)
        for rank in range(count)
    ]


def _insert_shuffled(store: JobStore, count: int, expired: int) -> list[str]:
    """
    Insert count jobs whose created_at order disagrees with their insertion order.

    Rows are written in rowid order and then backdated by raw SQL to the rank a
    fixed permutation assigns them, so a scan in table order visits them in an
    order that has nothing to do with created_at.

    Args:
        store: The store to write to.
        count: How many jobs to insert.
        expired: How many of them are backdated past SHUFFLE_AGE_DAYS.

    Returns:
        The job titles in ascending created_at order, index 0 being the oldest.

    """
    stamps = _shuffled_stamps(count, expired)
    ranked = [""] * count
    for index in range(count):
        title = f"Job {index:02d}"
        job = store.create_job(profile="default", title=title)
        rank = (index * SHUFFLE_STRIDE) % count
        ranked[rank] = title
        store._conn.execute(
            "UPDATE jobs SET created_at = ? WHERE id = ?",
            (stamps[rank].isoformat(), job.id),
        )
    store._conn.commit()
    return ranked


def _barrier_prune(store: JobStore, barrier: threading.Barrier) -> int:
    """Wait at the barrier, then prune, returning the count prune reported."""
    barrier.wait()
    return store.prune(max_age_days=SAFE_AGE_DAYS, max_rows=CONCURRENT_MAX_ROWS)


def _barrier_create(store: JobStore, barrier: threading.Barrier) -> None:
    """Wait at the barrier, then insert one job into the store prune is pruning."""
    barrier.wait()
    store.create_job(profile="default", title="Racer")


def _row_touching(traced: list[str]) -> list[str]:
    """
    Filter a trace log down to the statements that touch rows.

    Args:
        traced: Every statement the connection's trace callback reported.

    Returns:
        Those whose leading verb is not transaction control.

    """
    return [
        text
        for text in traced
        if (words := text.split()) and words[0].upper() not in TRANSACTION_VERBS
    ]


def _seed_one_per_state(store: JobStore) -> dict[str, JobState]:
    """
    Create one job in every JobState, each carrying an error text of its own.

    Iterates ``JobState`` itself rather than a hand-written roster, so a member
    added in a later phase is covered here without editing this helper.  Every
    job is given a distinct ``error`` before the call under test, which is what
    lets the terminal rows be asserted unchanged rather than merely
    still-terminal.  ``error_category`` is left ``None`` on purpose.

    Args:
        store: The store to write to.

    Returns:
        A mapping from job id to the state that job was moved into.

    """
    seeded: dict[str, JobState] = {}
    for state in JobState:
        job = store.create_job(profile="default", title=f"Job in {state.value}")
        store.update_state(job.id, state, error=f"before {state.value}")
        seeded[job.id] = state
    return seeded


def _seed_queue(store: JobStore) -> list[str]:
    """
    Create QUEUE_ROWS jobs whose created_at order is the reverse of insertion order.

    Backdating by raw SQL is what ``_insert_shuffled`` already does for the prune
    cases.  It keeps the ordering deterministic without sleeping: D-32 leaves the
    two existing sleeps in the prune tests alone, and this phase adds no third.

    Args:
        store: The store to write to.

    Returns:
        The job ids in insertion order -- index 0 is the NEWEST by created_at.

    """
    base = datetime.now(tz=UTC) - timedelta(hours=QUEUE_BASE_HOURS)
    ids: list[str] = []
    for index in range(QUEUE_ROWS):
        job = store.create_job(profile="default", title=f"Queued {index:02d}")
        ids.append(job.id)
        stamp = base + timedelta(minutes=QUEUE_ROWS - 1 - index)
        store._conn.execute(
            "UPDATE jobs SET created_at = ? WHERE id = ?",
            (stamp.isoformat(), job.id),
        )
    store._conn.commit()
    return ids


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


class TestPruneSingleStatement:
    """Prune's single-statement shape: shuffled ordering and the count's provenance."""

    def test_prune_shuffled_order_retains_the_newest_rows(self) -> None:
        """Prune keeps exactly the newest max_rows under shuffled insertion (STOR-04)."""
        # A pin, not a discriminator.  The single statement is correct only
        # because SQLite materialises the IN (SELECT ... ORDER BY ... LIMIT ?)
        # right-hand side as a LIST SUBQUERY before the outer scan begins, and
        # sqlite.org/isolation.html explicitly declines to guarantee that.  This
        # machine ships libsqlite 3.34.1 while CI and Docker ship newer, so this
        # test is what makes a future plan change loud instead of silent.
        store = JobStore()
        try:
            ranked = _insert_shuffled(store, SHUFFLE_ROWS, expired=0)

            deleted = store.prune(max_age_days=SAFE_AGE_DAYS, max_rows=SHUFFLE_KEEP)

            assert deleted == SHUFFLE_ROWS - SHUFFLE_KEEP
            survivors = {job.title for job in store.list_recent(limit=SHUFFLE_ROWS)}
            # The identity of the survivors, not merely how many there are: a
            # re-evaluating plan could keep the wrong rows and still land on the
            # right count.
            assert survivors == set(ranked[SHUFFLE_ROWS - SHUFFLE_KEEP :])
        finally:
            store.close()

    def test_prune_shuffled_order_with_an_age_cutoff_active(self) -> None:
        """Prune unions the age and row-cap predicates correctly (STOR-04)."""
        store = JobStore()
        try:
            ranked = _insert_shuffled(store, SHUFFLE_ROWS, expired=SHUFFLE_EXPIRED)

            deleted = store.prune(max_age_days=SHUFFLE_AGE_DAYS, max_rows=SHUFFLE_KEEP)

            assert deleted == SHUFFLE_ROWS - SHUFFLE_KEEP
            # Both predicates bit: more rows died than the age cutoff alone
            # accounts for.  The equivalence argument turns on the age-expired
            # set always being a prefix of the created_at ordering, so this is
            # the case that exercises the union rather than either half.
            assert deleted > SHUFFLE_EXPIRED
            survivors = {job.title for job in store.list_recent(limit=SHUFFLE_ROWS)}
            assert survivors == set(ranked[SHUFFLE_ROWS - SHUFFLE_KEEP :])
        finally:
            store.close()

    def test_prune_concurrent_insert_cannot_corrupt_the_count(self) -> None:
        """A create_job racing prune cannot make the returned count wrong (STOR-04)."""
        store = JobStore()
        try:
            for index in range(CONCURRENT_ROWS):
                store.create_job(profile="default", title=f"Row {index:02d}")
            before = len(store.list_recent(limit=CONCURRENT_ROWS * 2))
            barrier = threading.Barrier(RACERS, timeout=BARRIER_TIMEOUT)

            with ThreadPoolExecutor(max_workers=RACERS) as pool:
                pruner = pool.submit(_barrier_prune, store, barrier)
                creator = pool.submit(_barrier_create, store, barrier)
                # Future.result() re-raises the worker's exception here; a raw
                # threading.Thread would bury it in threading.excepthook and
                # this test would pass green on a broken store.
                deleted = pruner.result()
                creator.result()

            after = len(store.list_recent(limit=CONCURRENT_ROWS * 2))
            # The old before-minus-after arithmetic could be forced to -1.
            assert deleted >= 0
            assert deleted <= before
            # Exactly one insert and one prune ran, in one order or the other,
            # so this reconciliation holds under both interleavings.
            assert after == before + 1 - deleted
        finally:
            store.close()

    def test_prune_concurrent_window_between_count_and_delete_is_closed(self) -> None:
        """Prune runs one row-touching statement, leaving no race window (STOR-04)."""
        store = JobStore()
        try:
            for index in range(CONCURRENT_ROWS):
                store.create_job(profile="default", title=f"Row {index:02d}")
            traced: list[str] = []
            store._conn.set_trace_callback(traced.append)

            deleted = store.prune(
                max_age_days=SAFE_AGE_DAYS, max_rows=CONCURRENT_MAX_ROWS
            )

            statements = _row_touching(traced)
            assert deleted == CONCURRENT_ROWS - CONCURRENT_MAX_ROWS
            # The race the review recorded needed a gap between two counting
            # reads for a concurrent insert to land in.  One statement is not a
            # smaller gap; it is no gap, which is why the count is exact rather
            # than merely serialised behind the store's lock.
            assert len(statements) == 1, (
                f"prune() ran {len(statements)} row-touching statements: "
                f"{'; '.join(statements)}"
            )
            assert statements[0].lstrip().upper().startswith("DELETE")
            assert "COUNT(" not in statements[0].upper()
        finally:
            store._conn.set_trace_callback(None)
            store.close()


class TestQueryMethods:
    """fail_active_jobs() and list_pending(): the two STOR-05 query methods."""

    def test_fail_active_jobs_moves_every_active_job_to_error(self) -> None:
        """fail_active_jobs fails every active job and reports how many (STOR-05)."""
        store = JobStore()
        try:
            seeded = _seed_one_per_state(store)

            failed = store.fail_active_jobs()

            assert failed == len(ACTIVE_STATES)
            for job_id, original in seeded.items():
                job = store.get_job(job_id)
                assert job is not None
                if original in ACTIVE_STATES:
                    assert job.state == JobState.ERROR
                    assert job.error == RESTART_REASON
                else:
                    # Not merely "still terminal": the same state AND the same
                    # error text it carried before the call.  A predicate that
                    # over-reached would rewrite completed job history, and the
                    # error text is where that would show first.
                    assert original in TERMINAL_STATES
                    assert job.state == original
                    assert job.error == f"before {original.value}"
        finally:
            store.close()

    def test_fail_active_jobs_with_nothing_active_returns_zero(self) -> None:
        """fail_active_jobs on a settled store changes nothing (STOR-05)."""
        store = JobStore()
        try:
            job = store.create_job(profile="default", title="Finished")
            store.update_state(job.id, JobState.DONE)

            failed = store.fail_active_jobs()

            assert failed == 0
            settled = store.get_job(job.id)
            assert settled is not None
            assert settled.state == JobState.DONE
            assert settled.error is None
        finally:
            store.close()

    def test_fail_active_jobs_honours_a_custom_reason(self) -> None:
        """A caller-supplied reason is the text recorded on the failed row (STOR-05)."""
        store = JobStore()
        try:
            job = store.create_job(profile="default", title="In flight")
            store.update_state(job.id, JobState.SCANNING)

            failed = store.fail_active_jobs(reason=CUSTOM_REASON)

            assert failed == 1
            stopped = store.get_job(job.id)
            assert stopped is not None
            assert stopped.state == JobState.ERROR
            assert stopped.error == CUSTOM_REASON
        finally:
            store.close()

    def test_fail_active_jobs_leaves_error_category_unset(self) -> None:
        """fail_active_jobs writes no error_category on the rows it fails (STOR-05)."""
        # A deliberate assertion, not an omission.  N-14 records error_category
        # as written but never read, Phase 21's D-12 left it unwired, and Phase
        # 30 (APPL-04) owns giving it a consumer -- a second unread writer here
        # would work against the milestone that has to justify or delete the
        # field.  ErrorCategory.UNKNOWN would additionally be wrong: an
        # interrupted restart is not an unknown failure.
        store = JobStore()
        try:
            seeded = _seed_one_per_state(store)

            store.fail_active_jobs()

            for job_id in seeded:
                job = store.get_job(job_id)
                assert job is not None
                assert job.error_category is None
        finally:
            store.close()

    def test_fail_active_jobs_transitions_exactly_the_active_states(self) -> None:
        """The states fail_active_jobs moves are exactly ACTIVE_STATES (STOR-05)."""
        store = JobStore()
        try:
            seeded = _seed_one_per_state(store)

            store.fail_active_jobs()

            transitioned = set()
            for job_id, original in seeded.items():
                job = store.get_job(job_id)
                assert job is not None
                if job.state != original:
                    transitioned.add(original)
            # Computed from the real frozenset rather than spelled out.  This is
            # what makes Phase 23's FALLBACK and Phase 25's SCANNING_REVERSE get
            # picked up the moment they join ACTIVE_STATES, with no edit to
            # fail_active_jobs -- and what fails loudly if the predicate is ever
            # hand-written back into a fixed list.
            assert transitioned == ACTIVE_STATES
            # The seeding covered the whole enum, so "exactly ACTIVE_STATES" is
            # a statement about every state and not only about the ones seeded.
            assert set(seeded.values()) == ACTIVE_STATES | TERMINAL_STATES
        finally:
            store.close()

    def test_job_state_has_no_failed_member(self) -> None:
        """FAILED in the STOR-05 prose means the existing JobState.ERROR (STOR-05)."""
        # D-19, pinned as a test because a future reader taking the requirement
        # prose literally could reasonably invent a JobState.FAILED.  The value
        # is persisted as SQLite TEXT and read back through the enum
        # constructor, so adding or renaming a member is a data migration.
        assert "FAILED" not in JobState.__members__
        assert JobState.ERROR.value == "ERROR"

    def test_list_pending_returns_only_pending_jobs_oldest_first(self) -> None:
        """list_pending returns the still-queued jobs in creation order (STOR-05)."""
        store = JobStore()
        try:
            ids = _seed_queue(store)
            store.update_state(ids[QUEUE_MOVED[0]], JobState.SCANNING)
            store.update_state(ids[QUEUE_MOVED[1]], JobState.DONE)

            pending = store.list_pending()

            # created_at descends with the insertion index, so ascending
            # created_at order is the reverse of insertion order.  Spelling the
            # expectation this way makes the test fail under DESC (list_recent's
            # ordering) and under raw insertion order alike.
            expected = [
                f"Queued {index:02d}"
                for index in reversed(range(QUEUE_ROWS))
                if index not in QUEUE_MOVED
            ]
            assert [job.title for job in pending] == expected
        finally:
            store.close()

    def test_list_pending_on_an_empty_store_returns_an_empty_list(self) -> None:
        """list_pending on a store with no jobs returns an empty list (STOR-05)."""
        store = JobStore()
        try:
            assert store.list_pending() == []
        finally:
            store.close()

    def test_list_pending_returns_jobs_in_the_pending_state(self) -> None:
        """Every object list_pending returns is a PENDING Job (STOR-05)."""
        store = JobStore()
        try:
            ids = _seed_queue(store)
            store.update_state(ids[QUEUE_MOVED[0]], JobState.UPLOADING)

            pending = store.list_pending()

            assert len(pending) == QUEUE_ROWS - 1
            for job in pending:
                assert isinstance(job, Job)
                assert job.state == JobState.PENDING
                assert isinstance(job.tags, list)
        finally:
            store.close()
