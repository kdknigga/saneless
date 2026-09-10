"""
Job model with state machine and SQLite persistence.

Tracks scan jobs through their lifecycle (PENDING -> SCANNING ->
ASSEMBLING -> UPLOADING -> DONE) with SQLite-backed persistence
for crash recovery and history.
"""

from __future__ import annotations

import functools
import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Concatenate

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

_LOCKED_MARKER = "__saneless_locked__"
"""The attribute name that marks a callable as serialised on the store's lock.

Spelled in exactly one place.  The decorator that sets it and the reflective
test that looks for it both read this constant, so the marker name cannot drift
between source and test, and ``setattr`` through a constant is also what keeps
ruff's ``B010`` and both type checkers quiet -- a direct
``wrapper.__saneless_locked__ = True`` makes the checkers complain that a
function has no such attribute, and a string literal in ``setattr`` trips
``B010``.
"""

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

_COLUMN_LIST = ", ".join(_COLUMNS)
"""The column list every statement in this module names, written out once.

Derived from ``_COLUMNS`` so the two can never drift apart.
"""

_PLACEHOLDERS = ", ".join("?" for _ in _COLUMNS)
"""One bound ``?`` per column, for the ``INSERT``.

Derived from ``_COLUMNS`` for the same reason ``_COLUMN_LIST`` is: a column
added to the tuple brings its placeholder along with it, so the two lists
cannot fall out of step.
"""

_SELECT_JOBS = "SELECT"
"""The ``SELECT`` verb, held under a name rather than written into a statement.

Interpolating a column list is unavoidable -- SQL cannot parameterise an
identifier -- and here it is safe: the only interpolated values are
``_COLUMN_LIST`` and ``_PLACEHOLDERS``, both derived at import from the
module-level ``_COLUMNS`` tuple literal, which no caller-supplied value can
reach.  Every runtime value is a bound ``?`` parameter.  Holding the verb
under a name is also what lets the statements below be built once at import
instead of rebuilt on every call.
"""

_INSERT_JOBS = "INSERT INTO jobs"
"""The ``INSERT`` verb and its target table, held under a name.

The safety argument is ``_SELECT_JOBS``'s: the only interpolated values are
``_COLUMN_LIST`` and ``_PLACEHOLDERS``, both module-level derivations that no
caller can influence.
"""

_UPDATE_JOBS = "UPDATE jobs"
"""The ``UPDATE`` verb and its target table, held under a name.

Declared here so every statement in this module is assembled the same way.
Nothing is built from it yet -- the ``UPDATE`` statements that will use it
belong to the lock-and-transaction work, not to this change.  Its safety
argument is ``_SELECT_JOBS``'s.
"""

_DELETE_JOBS = "DELETE FROM jobs"
"""The ``DELETE`` verb and its target table, held under a name.

Declared for the same reason as ``_UPDATE_JOBS``, and consumed by ``_PRUNE``
below.  Its safety argument is ``_SELECT_JOBS``'s.
"""

_SELECT_ALL = f"{_SELECT_JOBS} {_COLUMN_LIST} FROM jobs"
"""Read every column of every job, in ``_COLUMNS`` order."""

_SELECT_BY_ID = f"{_SELECT_ALL} WHERE id = ?"
"""Read a single job by its primary key."""

_SELECT_RECENT = f"{_SELECT_ALL} ORDER BY created_at DESC LIMIT ?"
"""Read the newest jobs first, up to a bound limit."""

_INSERT = f"{_INSERT_JOBS} ({_COLUMN_LIST}) VALUES ({_PLACEHOLDERS})"
"""Write one job, naming every column so physical column order never matters."""

_NEWEST_IDS = f"{_SELECT_JOBS} id FROM jobs ORDER BY created_at DESC LIMIT ?"
"""The ids of the newest jobs, up to a bound limit -- ``_PRUNE``'s subquery.

Held apart from ``_PRUNE`` for the same reason ``_SELECT_JOBS`` holds the verb:
``S608`` matches ``select ... from`` anywhere in an interpolated string's
literal text, not only at its start, so a nested ``SELECT`` written inline would
trip it -- and a lint suppression is not available to silence it.  Behind a
name, both halves are ordinary constants and the rule has nothing to flag.
"""

_PRUNE = f"{_DELETE_JOBS} WHERE created_at < ? OR id NOT IN ({_NEWEST_IDS})"
"""Delete every job past the age cutoff or outside the newest ``max_rows``.

Two bound parameters, in this order: the ISO-8601 cutoff, then the row cap.  Its
safety argument is ``_SELECT_JOBS``'s -- the only interpolated value is the
module-level ``_DELETE_JOBS`` literal, and both runtime values are bound ``?``.

**Both comparisons are lexicographic over strings.**  ``created_at`` is written
as ``datetime.now(tz=UTC).isoformat()``, so every value ends ``+00:00``, and the
``<`` cutoff and the ``ORDER BY`` are correct *only* because of that uniformity.
A single non-UTC timestamp reaching this column -- a ``-05:00`` offset, say --
would make both the cutoff and the ordering silently wrong, and nothing here
would raise.  Anyone adding a writer to this column meets this note first.

The two predicates are a *union*, not a sequence, and that is equivalent to
deleting by age and then trimming to a row cap: both order by ``created_at``, so
the age-expired rows are always a prefix of the oldest and the union of the two
sets is exactly what the sequential form produced.  The equivalence rests in
turn on SQLite evaluating the ``IN (SELECT ... ORDER BY ... LIMIT ?)`` right-hand
side into a ``LIST SUBQUERY`` before the outer scan begins, so it never observes
its own partial deletions.  SQLite does not document that as a guarantee, so it
is pinned by the shuffled-insert-order tests in ``tests/test_job.py`` rather than
by contract: if a future libsqlite changes the plan, those go red instead of
this statement quietly under-deleting.
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


def _locked[**P, R](
    method: Callable[Concatenate[JobStore, P], R],
) -> Callable[Concatenate[JobStore, P], R]:
    """
    Serialise a JobStore method on the store's re-entrant lock.

    Takes ``self._lock`` and nothing else.  It deliberately does *not* also
    open the connection's transaction context: ``Connection.__exit__`` runs
    after the body and must commit or roll back, so ``with conn: conn.close()``
    raises ``ProgrammingError: Cannot operate on a closed database`` and
    ``close()`` could never be decorated.  Even a hand-rolled context manager
    fails, because ``in_transaction`` itself raises on a closed connection.
    Each method that executes SQL therefore opens its own ``with self._conn:``
    in its body, where the transaction boundary is also more legible.

    Args:
        method: An unbound ``JobStore`` method to serialise.

    Returns:
        The method, wrapped to hold the store's lock for its whole duration and
        carrying the ``_LOCKED_MARKER`` attribute the reflective coverage test
        looks for.

    """

    @functools.wraps(method)
    def wrapper(self: JobStore, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._lock:
            return method(self, *args, **kwargs)

    setattr(wrapper, _LOCKED_MARKER, True)
    return wrapper


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

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        """
        Convert one jobs row into a Job.

        The only row-to-``Job`` conversion in this module.  It is private and
        undecorated deliberately: ``sqlite3`` connection context managers do
        not nest, so an inner ``with conn:`` commits the outer transaction.
        Shared logic therefore has to live in a helper a public method can
        call without going through another public method.

        Every access is by name.  A database migrated from the pre-``thumbnail``
        shape by the old bare ``ALTER`` carries ``error_category`` last while a
        freshly created one carries it sixth, so physical column order is not a
        contract and no ordinal may be used here.  (``sqlite3.Row`` keys are
        case-insensitive; nothing here relies on that, and nothing should.)

        Args:
            row: A jobs row fetched over this store's own connection.

        Returns:
            The job the row records.

        """
        return Job(
            id=row["id"],
            profile=row["profile"],
            title=row["title"],
            state=JobState(row["state"]),
            error=row["error"],
            error_category=(
                ErrorCategory(row["error_category"]) if row["error_category"] else None
            ),
            tags=json.loads(row["tags"]),
            correspondent=row["correspondent"],
            thumbnail=row["thumbnail"],
            created_at=datetime.fromisoformat(row["created_at"]),
            outcome=ScanOutcome(row["outcome"]) if row["outcome"] else None,
            pages_scanned=row["pages_scanned"],
            pages_removed=row["pages_removed"],
            pages_uploaded=row["pages_uploaded"],
            warning=row["warning"],
            owner_token=row["owner_token"],
        )

    @_locked
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
        job_id = str(uuid.uuid4())
        with self._conn:
            self._conn.execute(
                _INSERT,
                (
                    job_id,
                    profile,
                    title,
                    JobState.PENDING.value,
                    None,  # error
                    None,  # error_category
                    json.dumps(tags or []),
                    correspondent,
                    thumbnail,
                    datetime.now(tz=UTC).isoformat(),
                    # The six result columns are written by nothing in this
                    # phase.
                    None,  # outcome
                    None,  # pages_scanned
                    None,  # pages_removed
                    None,  # pages_uploaded
                    None,  # warning
                    None,  # owner_token
                ),
            )
            # Read the row back inside the same transaction, before the commit:
            # the caller then gets the job the database holds rather than a
            # second, hand-built copy of it, which is what keeps the row
            # mapping in exactly one place.
            row = self._conn.execute(_SELECT_BY_ID, (job_id,)).fetchone()
        job = self._row_to_job(row)
        logger.debug("Created job %s: %s", job.id, job.title)
        return job

    @_locked
    def get_job(self, job_id: str) -> Job | None:
        """
        Fetch a job by ID.

        Args:
            job_id: The UUID string of the job.

        Returns:
            The Job if found, None otherwise.

        """
        with self._conn:
            row = self._conn.execute(_SELECT_BY_ID, (job_id,)).fetchone()
        return None if row is None else self._row_to_job(row)

    @_locked
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
        with self._conn:
            self._conn.execute(
                "UPDATE jobs SET state = ?, error = ?, error_category = ? WHERE id = ?",
                (
                    state.value,
                    error,
                    error_category.value if error_category else None,
                    job_id,
                ),
            )
        logger.debug("Job %s -> %s", job_id, state.value)

    @_locked
    def update_thumbnail(self, job_id: str, thumbnail: str) -> None:
        """
        Update the thumbnail of a job.

        Args:
            job_id: The UUID string of the job.
            thumbnail: Base64-encoded JPEG thumbnail string.

        """
        with self._conn:
            self._conn.execute(
                "UPDATE jobs SET thumbnail = ? WHERE id = ?",
                (thumbnail, job_id),
            )

    @_locked
    def list_recent(self, limit: int = 50) -> list[Job]:
        """
        Fetch the most recent jobs ordered newest-first.

        Args:
            limit: Maximum number of jobs to return.

        Returns:
            List of Job instances ordered by creation time descending.

        """
        with self._conn:
            rows = self._conn.execute(_SELECT_RECENT, (limit,)).fetchall()
        return [self._row_to_job(row) for row in rows]

    # The two declarations below carry no behaviour yet.  They exist in this
    # commit because the project-wide `ty` hook resolves attributes across the
    # whole tree, so a test naming a method that does not exist blocks the
    # commit outright.  The RED/GREEN split that matters is the behavioural
    # one, and it is intact: both raise, and every test of them is red.

    @_locked
    def list_pending(self) -> list[Job]:
        """Raise until the GREEN commit gives this method its behaviour."""
        raise NotImplementedError

    @_locked
    def fail_active_jobs(self, reason: str = "Interrupted by restart") -> int:
        """Raise until the GREEN commit gives this method its behaviour."""
        raise NotImplementedError

    @_locked
    def prune(self, max_age_days: int = 7, max_rows: int = 500) -> int:
        """
        Remove old jobs by age and count limits.

        One ``DELETE`` removes every job older than ``max_age_days`` together
        with every job outside the newest ``max_rows``, and reports how many
        rows it removed.  The two predicates are a union rather than a sequence;
        ``_PRUNE`` carries the argument for why that is the same set the old
        delete-by-age-then-trim composition produced, and the UTC assumption
        both halves rest on.

        The count comes from the statement itself.  The two ``SELECT COUNT(*)``
        reads this used to subtract straddled the deletes, so an insert landing
        between them made the answer wrong -- a scratch test drove it to ``-1``.
        A single statement leaves no gap for an insert to land in, which is a
        stronger guarantee than serialising the method behind the store's lock:
        it holds against writers on other connections too.

        Args:
            max_age_days: Maximum age in days before a job is pruned.
            max_rows: Maximum number of jobs to retain.

        Returns:
            Total number of jobs deleted.

        """
        cutoff = (datetime.now(tz=UTC) - timedelta(days=max_age_days)).isoformat()
        with self._conn:
            deleted = self._conn.execute(_PRUNE, (cutoff, max_rows)).rowcount

        if deleted > 0:
            logger.debug("Pruned %d old jobs", deleted)
        return deleted

    @_locked
    def close(self) -> None:
        """Close the database connection."""
        # No `with self._conn:` here, deliberately.  Connection.__exit__ runs
        # after the body and must commit or roll back a connection the body has
        # already closed, which raises "Cannot operate on a closed database".
        self._conn.close()
