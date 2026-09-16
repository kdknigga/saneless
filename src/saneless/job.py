"""
Job model with state machine and SQLite persistence.

Tracks scan jobs through their lifecycle (PENDING -> SCANNING ->
ASSEMBLING -> UPLOADING -> DONE) and persists them in SQLite for
history.  A job survives the process that ran it only as a row: on
startup the web app fails every job still in an active state through
``fail_active_jobs`` before the worker starts (ROBU-06).
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Concatenate

from saneless.exceptions import StorageError, describe
from saneless.vocabulary import (
    ACTIVE_STATES,
    BUSY_STATES,
    ErrorCategory,
    JobState,
    ScanOutcome,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Set as AbstractSet

__all__ = ["ErrorCategory", "Job", "JobResult", "JobState", "JobStore"]

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

_ACTIVE_STATE_VALUES: tuple[str, ...] = tuple(sorted(s.value for s in ACTIVE_STATES))
"""The stored TEXT value of every job state that counts as still in flight.

Derived from ``ACTIVE_STATES`` rather than written out, so a state added to that
frozenset in a later phase is picked up by ``_FAIL_ACTIVE`` without anyone
editing this module -- which is the whole point of the vocabulary owning the
partition.  ``ACTIVE_STATES`` and ``TERMINAL_STATES`` partition ``JobState``, so
what this tuple excludes is exactly the completed jobs.

``sorted()`` because ``ACTIVE_STATES`` is a ``frozenset``: its iteration order
varies per process with ``PYTHONHASHSEED``.  The *result* is correct either way,
but an unsorted bind makes a failing test's parameter dump irreproducible across
runs and makes the assembled SQL text vary between processes, which defeats
``sqlite3``'s statement cache.
"""

_ACTIVE_MARKS = ", ".join("?" for _ in _ACTIVE_STATE_VALUES)
"""One bound ``?`` per active state, for ``_FAIL_ACTIVE``'s ``IN`` clause.

Derived from ``_ACTIVE_STATE_VALUES`` for the same reason ``_PLACEHOLDERS`` is
derived from ``_COLUMNS``: the run of placeholders and the tuple bound against it
cannot fall out of step.  Only the *length* of the tuple reaches the SQL text;
every value is a bound parameter.
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

Declared here so every statement in this module is assembled the same way, and
consumed by ``_FAIL_ACTIVE`` below.  Its safety argument is ``_SELECT_JOBS``'s.
"""

_DELETE_JOBS = "DELETE FROM jobs"
"""The ``DELETE`` verb and its target table, held under a name.

Declared for the same reason as ``_UPDATE_JOBS``, and consumed by
``_DELETE_BY_ID`` and ``_PRUNE`` below.  Its safety argument is
``_SELECT_JOBS``'s.
"""

_SELECT_ALL = f"{_SELECT_JOBS} {_COLUMN_LIST} FROM jobs"
"""Read every column of every job, in ``_COLUMNS`` order."""

_SELECT_BY_ID = f"{_SELECT_ALL} WHERE id = ?"
"""Read a single job by its primary key."""

_SELECT_RECENT = f"{_SELECT_ALL} ORDER BY created_at DESC LIMIT ?"
"""Read the newest jobs first, up to a bound limit."""

_LIST_PENDING = f"{_SELECT_ALL} WHERE state = ? ORDER BY created_at ASC"
"""Read the queued jobs oldest-first -- the order they will be worked in.

Ascending, the opposite of ``_SELECT_RECENT``: history reads newest-first, a
queue reads oldest-first.  One bound parameter, the state value, which is
``JobState.PENDING.value`` at the only call site -- the literal string is never
written into the statement text.  Its safety argument is ``_SELECT_JOBS``'s: the
only interpolated value is the module-level ``_SELECT_ALL``.

Like ``_PRUNE``'s, the ``ORDER BY`` is lexicographic over ``created_at``'s
ISO-8601 strings and is correct only because every writer stamps UTC.
"""

_SELECT_LATEST_RUN = (
    f"{_SELECT_ALL} WHERE error_category IS NOT ? ORDER BY created_at DESC LIMIT ?"
)
"""Read the newest jobs that were not rejected at submit, newest first.

Two bound parameters: ``ErrorCategory.REJECTED.value`` and the row limit, an
integer ``latest_run_job`` derives from how many ids it excludes (IN-08).
Neither is ever written into the statement text.
``IS NOT`` rather than ``!=`` because it is NULL-safe in SQLite: ``NULL != 'X'``
is NULL and would drop the row, while ``NULL IS NOT 'X'`` is true.  Every job
that has not failed, and every job ``fail_active_jobs`` failed on restart, has
no category, so ``!=`` would silently hide exactly the jobs this query exists to
return.  Its safety argument is ``_SELECT_JOBS``'s: the only interpolated value
is the module-level ``_SELECT_ALL``, and the category is bound.

Like ``_SELECT_RECENT``'s, the ``ORDER BY`` is lexicographic over
``created_at``'s ISO-8601 strings and is correct only because every writer
stamps UTC.
"""

_COUNT_JOBS = f"{_SELECT_JOBS} COUNT(*) FROM jobs"
"""Count every job -- the read half of ``JobStore.probe``.

No parameters.  Its safety argument is ``_SELECT_JOBS``'s: the only interpolated
value is that module-level literal.
"""

_INSERT = f"{_INSERT_JOBS} ({_COLUMN_LIST}) VALUES ({_PLACEHOLDERS})"
"""Write one job, naming every column so physical column order never matters."""

_FAIL_ACTIVE = (
    f"{_UPDATE_JOBS} SET state = ?, error = ? WHERE state IN ({_ACTIVE_MARKS})"
)
"""Move every still-in-flight job to a failed state with a given error text.

Bound parameters, in this order: the target state value, the error text, then one
per entry of ``_ACTIVE_STATE_VALUES``.  Its safety argument is
``_SELECT_JOBS``'s, extended one step: the only interpolated values are the
module-level ``_UPDATE_JOBS`` literal and a run of ``?`` characters whose LENGTH
comes from a module-level tuple.  Every runtime value -- the target state, the
caller-supplied reason, each active-state value -- is a bound parameter.

``json_each(?)`` would make the statement fully static and was verified to work
here, but JSON1 was a compile-time option before SQLite 3.38, so it would add a
soft dependency on an extension this module otherwise does not need.
"""

_DELETE_BY_ID = f"{_DELETE_JOBS} WHERE id = ?"
"""Delete a single job by its primary key.

One bound parameter, the job id.  Its safety argument is ``_SELECT_JOBS``'s: the
only interpolated value is the module-level ``_DELETE_JOBS`` literal.
"""

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


def _open_failure(db_path: str, exc: sqlite3.Error) -> StorageError:
    """
    Build the StorageError for a job database that cannot be used at open time.

    Args:
        db_path: Path the connection was opened on, named in the message.
        exc: The sqlite3 error that stopped the open.

    Returns:
        A one-line StorageError naming the path and sqlite's reason (D-08).
        The CLI guard maps StorageError to exit 2 by type (D-07 amendment).

    """
    return StorageError(
        f"Could not open the job database at {db_path}: {describe(exc)}"
    )


def _open_connection(db_path: str) -> sqlite3.Connection:
    """
    Open a job database connection with WAL and explicit transaction control.

    A path that cannot be opened (a directory, a missing parent) fails at
    ``connect``; a file that is not SQLite fails at the WAL pragma, the first
    statement that reads it.  Only this open path is translated: the runtime
    store methods keep their raw sqlite3 errors, which the web worker's
    degraded-health handling depends on.

    Args:
        db_path: Path to the SQLite database file, or ":memory:".

    Returns:
        The open connection, outside any transaction.

    Raises:
        StorageError: If the database cannot be opened or read as SQLite.  The
            connection, when one was opened, is closed first.

    """
    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
    except sqlite3.Error as exc:
        raise _open_failure(db_path, exc) from exc
    try:
        conn.row_factory = sqlite3.Row
        # WAL has to be enabled before the connection switches to explicit
        # transaction control: that switch opens a transaction immediately,
        # and SQLite refuses a journal-mode change inside a transaction on a
        # file database while silently reporting "memory" on ":memory:".
        conn.execute("PRAGMA journal_mode=WAL")
        conn.autocommit = False
    except sqlite3.Error as exc:
        conn.close()
        raise _open_failure(db_path, exc) from exc
    except BaseException:
        conn.close()
        raise
    return conn


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


@dataclass(frozen=True)
class JobResult:
    """
    Everything a finished scan recorded, bundled into one argument.

    It exists to keep :meth:`JobStore.finish_job` within ruff's ``PLR0913``
    limit of five non-``self`` parameters.  :meth:`JobStore.create_job` sits
    exactly at that limit and passes, which is the evidence both that five is
    the ceiling and that ``self`` is not counted; spelling these five facts out
    as individual parameters alongside ``job_id``, ``state``, ``error`` and
    ``error_category`` would make nine.

    The fields deliberately mirror :class:`saneless.pipeline.ScanResult`
    *without* importing it.  ``job.py`` importing ``pipeline.py`` would invert
    the dependency direction -- the pipeline is the layer that knows about
    persistence, not the reverse -- so the worker does the field-for-field copy
    at its single call site instead.  Taking a ``ScanResult`` directly, and
    raising the ``PLR0913`` limit, were both weighed and rejected for those two
    reasons respectively.

    Every field is optional, because a caller that has no result to record
    passes no ``JobResult`` at all rather than a zero-filled one.

    Attributes:
        outcome: How the scan resolved.
        warning: A note about something odd that did not fail the scan.
        pages_scanned: Pages the scanner produced.
        pages_removed: Pages discarded as blank.
        pages_uploaded: Pages sent to paperless-ngx.

    """

    outcome: ScanOutcome | None
    warning: str | None
    pages_scanned: int | None
    pages_removed: int | None
    pages_uploaded: int | None


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
        """
        Open the job store, enable WAL, and run the migration ladder.

        Raises:
            StorageError: If the database cannot be opened or read as SQLite,
                or its jobs table has an unsupported shape.  Every message
                names ``db_path``.

        """
        self._lock = threading.RLock()
        self._conn = _open_connection(db_path)
        try:
            _migrate(self._conn, db_path)
        except Exception as exc:
            # Release the BEGIN DEFERRED the failed ladder still holds, and
            # the file handle with it, before the caller sees the failure.  A
            # rollback that fails too -- a disk I/O error on the same broken
            # file -- must not replace the migration's own error with a raw
            # sqlite3 one, which would exit 5 instead of 2 (IN-06).
            with contextlib.suppress(sqlite3.Error):
                self._conn.rollback()
            self._conn.close()
            if isinstance(exc, sqlite3.Error):
                raise _open_failure(db_path, exc) from exc
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
        owner_token: str | None = None,
    ) -> Job:
        """
        Create and persist a new job.

        ``owner_token`` occupies the parameter slot ``thumbnail`` used to hold.
        Nothing ever passed ``thumbnail`` to this method -- verified by grep
        across ``src/`` and ``tests/`` before it was removed -- because
        :meth:`update_thumbnail` is the live writer, called by the worker once
        a scan has produced an image (``worker.py:1288``).  Spending the freed
        slot rather than adding a sixth parameter is deliberate: ruff's
        ``PLR0913`` ceiling is five non-``self`` parameters and it counts
        keyword-only ones too, so a sixth would need either a suppression,
        which this project does not write, or a frozen-dataclass bundle in the
        shape of :class:`JobResult`.  Neither is warranted to make room for a
        parameter that replaces a dead one.

        Args:
            profile: Scan profile name.
            title: Document title.
            tags: Optional list of tag IDs.
            correspondent: Optional correspondent ID.
            owner_token: The submitting browser's opaque session token, or None
                when the submission carried none.

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
                    # thumbnail -- never a submission field.  update_thumbnail
                    # writes it once the worker has an image to write
                    # (worker.py:1288), which is why the parameter that used to
                    # sit in this position could be spent on owner_token.
                    None,
                    datetime.now(tz=UTC).isoformat(),
                    # A new job has recorded nothing yet, so every result
                    # column starts NULL -- deliberately, because NULL means
                    # "never recorded" rather than a measured zero.  finish_job
                    # is the writer of these five, once the run ends.
                    None,  # outcome
                    None,  # pages_scanned
                    None,  # pages_removed
                    None,  # pages_uploaded
                    None,  # warning
                    # owner_token's first and only writer (D-23).  The value is
                    # an opaque session token minted by the web layer and kept
                    # for exactly one purpose: rendering the flip prompt to the
                    # browser that submitted this job rather than to every
                    # browser watching it.  NULL means the row is unowned and
                    # the prompt is rendered for everyone -- which is every row
                    # written before this phase, including a manual-duplex job
                    # still in flight across an upgrade, so no migration
                    # backfills it and none is needed.  It is a footgun guard,
                    # not an authentication mechanism: the column already
                    # existed unused, and guessing a token grants nothing a LAN
                    # neighbour cannot already do.
                    owner_token,
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
    def create_rejected_job(
        self,
        profile: str,
        title: str,
        *,
        error: str,
        tags: list[int] | None = None,
        correspondent: int | None = None,
    ) -> Job:
        """
        Record a submit that was refused before any job row existed.

        The refused attempt still gets a row, so history shows the user that
        their scan was not started and why (D-05).  The row is written already
        terminal -- ``ERROR`` with ``ErrorCategory.REJECTED`` -- and that marker
        is what :meth:`latest_run_job` skips, so the rejection never replaces
        the job that just ended in the status area (D-06).

        One ``INSERT``, not :meth:`create_job` followed by :meth:`finish_job`.
        Those are two transactions: if the second one raised, the first would
        already have committed a ``PENDING`` row with no marker.  No worker ever
        saw that id and restart recovery never runs again, so the row would stay
        active for good, showing "Starting scan..." and disabling the Scan
        button (WR-01).  With a single statement a failure leaves no row at all.

        Args:
            profile: Scan profile name the refused submit named.
            title: Document title the refused submit named.
            error: The job-history error text explaining the refusal.
            tags: Optional list of tag IDs.
            correspondent: Optional correspondent ID.

        Returns:
            The newly created, already-terminal Job instance.

        """
        job_id = str(uuid.uuid4())
        with self._conn:
            self._conn.execute(
                _INSERT,
                (
                    job_id,
                    profile,
                    title,
                    JobState.ERROR.value,
                    error,
                    ErrorCategory.REJECTED.value,
                    json.dumps(tags or []),
                    correspondent,
                    None,  # thumbnail -- a refused submit never scanned
                    datetime.now(tz=UTC).isoformat(),
                    # Nothing ran, so nothing was recorded: every result column
                    # is NULL ("never recorded"), never a measured zero.
                    None,  # outcome
                    None,  # pages_scanned
                    None,  # pages_removed
                    None,  # pages_uploaded
                    None,  # warning
                    None,  # owner_token
                ),
            )
            # Read back inside the same transaction, as create_job does, so the
            # row mapping stays in _row_to_job alone.
            row = self._conn.execute(_SELECT_BY_ID, (job_id,)).fetchone()
        job = self._row_to_job(row)
        logger.debug("Created rejected job %s: %s", job.id, job.title)
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
    def finish_job(
        self,
        job_id: str,
        state: JobState,
        result: JobResult | None = None,
        error: str | None = None,
        error_category: ErrorCategory | None = None,
    ) -> None:
        """
        Record a job's terminal state together with everything the run produced.

        The only writer of the five result columns, and the reason
        :meth:`update_state` is not.  ``update_state``'s SQL is an
        unconditional ``SET state, error, error_category``; naming the result
        columns there would blank them on every in-flight SCANNING /
        ASSEMBLING / UPLOADING transition, so the terminal write is a separate
        method rather than an extra argument.

        State, outcome, warning, the three counts and the error all land in one
        ``UPDATE``.  The web request thread reads this row while the worker
        thread writes it, and a two-statement version would let it observe a
        job that had finished but had not yet recorded how.

        Omitting ``result`` leaves ``outcome``, ``warning`` and all three page
        counts NULL rather than zero.  NULL means "never recorded"; ``0`` means
        "counted, and there were none".  A job that failed before the scanner
        opened has not measured zero pages, and writing ``0`` would make those
        two states indistinguishable for the life of the row.

        Args:
            job_id: The UUID string of the job.
            state: The terminal state to record.
            result: What the run recorded, or None when it recorded nothing.
            error: Optional error message (typically set with ERROR state).
            error_category: Optional error category for programmatic handling.

        """
        with self._conn:
            self._conn.execute(
                "UPDATE jobs SET state = ?, outcome = ?, warning = ?, "
                "pages_scanned = ?, pages_removed = ?, pages_uploaded = ?, "
                "error = ?, error_category = ? WHERE id = ?",
                (
                    state.value,
                    result.outcome.value if result and result.outcome else None,
                    result.warning if result else None,
                    result.pages_scanned if result else None,
                    result.pages_removed if result else None,
                    result.pages_uploaded if result else None,
                    error,
                    error_category.value if error_category else None,
                    job_id,
                ),
            )
        logger.debug("Job %s finished as %s", job_id, state.value)

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

    @_locked
    def list_pending(self) -> list[Job]:
        """
        Fetch the queued jobs oldest-first.

        Ascending ``created_at``, the opposite of ``list_recent``'s newest-first
        history ordering: this is a queue, and the oldest entry is the next one
        to be worked.  ``PENDING`` is the only state a job that has not started
        can be in, so the predicate is a single bound scalar rather than the
        variable-length ``IN`` ``fail_active_jobs`` needs -- but the state value
        is still bound rather than written into the statement text.

        This method has NO production caller in this phase.  Showing a job its
        position in the queue is APPL-08, which belongs to Phase 30; until then
        its tests are its only consumer.

        Returns:
            List of Job instances awaiting a scanner, ordered by creation time
            ascending.

        """
        with self._conn:
            rows = self._conn.execute(
                _LIST_PENDING, (JobState.PENDING.value,)
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    @_locked
    def latest_run_job(self, exclude_ids: AbstractSet[str] = frozenset()) -> Job | None:
        """
        Fetch the newest job that was not rejected at submit.

        This is what the status area falls back to once no job is active, to
        report the job that just ended (D-17).  ``list_recent(1)`` is the wrong
        answer there: a submit refused while a job runs -- queue full, worker
        down or degraded -- still writes a row marked
        ``ErrorCategory.REJECTED``, and that row is newer than the running job.
        Reading the newest row would let the rejection replace the job in the
        status area the moment the job ends (D-06).  History keeps using
        ``list_recent``, so the rejected row is still listed there.

        Excluded ids are filtered in Python, never interpolated into the SQL.
        Reading ``len(exclude_ids) + 1`` rows is enough: at most that many
        newer rows can be skipped, so the row after them is the answer.

        Args:
            exclude_ids: Ids to treat as never run.  The web layer passes the
                refused submits whose REJECTED write is still owed to the
                worker, so their PENDING rows do not stand in for the job that
                just ended (IN-08, D-06).  A set rather than any collection:
                a bare id string is itself a collection of strings, and would
                silently exclude its single characters instead (IN-10).

        Returns:
            The newest job whose error category is not REJECTED and whose id is
            not excluded, or None when the store holds no such job.

        """
        skip = frozenset(exclude_ids)
        with self._conn:
            rows = self._conn.execute(
                _SELECT_LATEST_RUN, (ErrorCategory.REJECTED.value, len(skip) + 1)
            ).fetchall()
        jobs = (self._row_to_job(row) for row in rows)
        return next((job for job in jobs if job.id not in skip), None)

    @_locked
    def probe(self) -> None:
        """
        Prove the database can be read and written, in one transaction.

        A count of the jobs table, then a same-value ``user_version`` write.
        The write is what makes this a real probe: a read-only transaction can
        succeed against a store whose disk is full or whose file has gone
        read-only, but setting ``user_version`` -- even to the value it already
        holds -- appends a WAL frame and so exercises the write path (D-12).
        Nothing observable changes on a healthy store.

        The worker's idle loop calls this while it is degraded; a clean return
        is what clears degraded health, and a raise means "still degraded".

        Raises:
            sqlite3.Error: Whatever sqlite raises when the store cannot be read
                or written, including ``sqlite3.ProgrammingError`` once the
                store is closed.

        """
        with self._conn:
            self._conn.execute(_COUNT_JOBS).fetchone()
            version: int = self._conn.execute("PRAGMA user_version").fetchone()[0]
            # A PRAGMA argument cannot be bound -- "PRAGMA user_version = ?" is
            # a syntax error.  version is an int read back from this database
            # in this same transaction, never request input, so no
            # caller-supplied value reaches this string.
            self._conn.execute(f"PRAGMA user_version = {version}")

    @_locked
    def fail_active_jobs(self, reason: str = "Interrupted by restart") -> int:
        """
        Fail every job still in flight, for recovery after an unclean restart.

        A job left mid-scan by a killed process has no worker behind it any
        more, so it would otherwise sit in an active state for ever and the UI
        would poll it for ever.  Every row whose state is in ``ACTIVE_STATES``
        moves to ``JobState.ERROR`` carrying ``reason``.

        The predicate is derived from ``ACTIVE_STATES`` rather than listed by
        hand, which buys two things: ``ACTIVE_STATES`` and ``TERMINAL_STATES``
        partition ``JobState``, so it provably cannot reach a completed job's
        recorded history; and a state added to that frozenset in a later phase
        is covered here with no edit to this method.

        "FAILED" throughout STOR-05 and the roadmap criteria means this
        ``JobState.ERROR``.  There is no ``JobState.FAILED``, and none is added:
        the value is persisted as SQLite TEXT and read back through the enum
        constructor, so adding or renaming a member is a data migration.

        ``error_category`` is deliberately not written.  It is recorded by
        ``update_state`` today and read by nothing, and the phase that gives it
        a consumer has to decide whether the field earns its place at all -- a
        second unread writer here would work against that.  ``UNKNOWN`` would
        additionally be the wrong value: an interrupted restart is not an
        unknown failure, it is a precisely known one.

        Its caller is the web lifespan at startup, before the worker thread
        begins, passing ``RESTART_REASON`` (ROBU-06); when that call raises,
        the worker's recovery makes the same call once the store accepts
        writes again (26-06).  The returned count is what lets the lifespan
        log how many jobs it failed without a second query.

        Args:
            reason: The error text recorded on every job this fails.

        Returns:
            How many jobs were moved to ERROR.

        """
        with self._conn:
            failed = self._conn.execute(
                _FAIL_ACTIVE,
                (JobState.ERROR.value, reason, *_ACTIVE_STATE_VALUES),
            ).rowcount

        if failed > 0:
            logger.debug("Failed %d in-flight job(s): %s", failed, reason)
        return failed

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
    def delete_job(self, job_id: str) -> bool:
        """
        Remove one job by id.

        :meth:`prune` deletes by age and by row count, which is the wrong shape
        for a caller holding a single id.  Without this, removing one named job
        meant reaching past the store into ``_conn`` and opening a transaction
        on the shared connection while holding none of the store's lock -- the
        exact pattern the ``@_locked`` discipline exists to prevent.

        Args:
            job_id: The UUID string of the job.

        Returns:
            True if a job was removed, False if no job carried that id.

        """
        with self._conn:
            deleted = self._conn.execute(_DELETE_BY_ID, (job_id,)).rowcount

        if deleted > 0:
            logger.debug("Deleted job %s", job_id)
        return deleted > 0

    @_locked
    def close(self) -> None:
        """Close the database connection."""
        # No `with self._conn:` here, deliberately.  Connection.__exit__ runs
        # after the body and must commit or roll back a connection the body has
        # already closed, which raises "Cannot operate on a closed database".
        self._conn.close()
