"""
Scan jobs and their SQLite persistence.

Each job carries a persisted ``state`` column that the worker drives through
the lifecycle, from PENDING through scanning, assembling and uploading to a
terminal state such as DONE or ERROR.  The store records each transition it is
given and does not check that it is a legal one; the order lives in the
worker.  The rows are also the history.  A job survives the process that ran it
only as a row: on startup the web app fails every job still in an active state
through ``fail_active_jobs`` before the worker starts.
"""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import os
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Concatenate, Final

from saneless.exceptions import StorageError, describe
from saneless.vocabulary import (
    ACTIVE_STATES,
    BUSY_STATES,
    RESTART_REASON,
    ErrorCategory,
    JobState,
    ScanOutcome,
    restart_category,
    restart_error,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from collections.abc import Set as AbstractSet
    from pathlib import Path

__all__ = [
    "CLI_JOBS_DEFAULT_LIMIT",
    "REJECTED_HISTORY_ROWS",
    "WEB_HISTORY_LIMIT",
    "ErrorCategory",
    "Job",
    "JobResult",
    "JobState",
    "JobStore",
    "read_recent_jobs",
]

logger = logging.getLogger(__name__)

_LOCKED_MARKER = "__saneless_locked__"
"""The attribute name that marks a callable as serialised on the store's lock.

The decorator that sets it and the reflective test that looks for it both read
this constant, so the marker name cannot drift between source and test.
"""

# How many jobs each history view shows.  A page has room for a longer list than
# a terminal, so the web page and ``saneless jobs`` differ by design.
WEB_HISTORY_LIMIT: Final = 50
CLI_JOBS_DEFAULT_LIMIT: Final = 20

# How many refused submits (``ErrorCategory.REJECTED`` rows) the store keeps.
# Their own cap, outside ``history_max_rows``, means a flood of refusals never
# evicts a real scan; keeping it below WEB_HISTORY_LIMIT means it never fills
# the visible history table either.
REJECTED_HISTORY_ROWS: Final = 20

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
    "pages_removed_at",
)
"""Every column the jobs table carries at the head schema version.

Every ``SELECT`` and ``INSERT`` in this module derives its column list and
placeholders from this tuple, so a column cannot be added to one statement and
forgotten in another.  The statements interpolate only module-level constants:
this column list, runs of ``?`` placeholders and other SQL constants.  Every
value they carry is a bound ``?`` parameter.
"""

_COLUMN_LIST = ", ".join(_COLUMNS)
"""The column list every statement in this module names, written out once."""

_PLACEHOLDERS = ", ".join("?" for _ in _COLUMNS)
"""One bound ``?`` per column, for the ``INSERT``."""

_ACTIVE_STATE_VALUES: tuple[str, ...] = tuple(sorted(s.value for s in ACTIVE_STATES))
"""The stored TEXT value of every job state that counts as still in flight.

Derived from ``ACTIVE_STATES``, so a state added there reaches ``_FAIL_ACTIVE``
without an edit here.  Sorted only because a frozenset's iteration order varies
with ``PYTHONHASHSEED``; sorting keeps a failing test's parameter dump stable.
"""

_ACTIVE_MARKS = ", ".join("?" for _ in _ACTIVE_STATE_VALUES)
"""One bound ``?`` per active state, for ``_FAIL_ACTIVE``'s ``IN`` clause.

Only the length of ``_ACTIVE_STATE_VALUES`` reaches the SQL text; every value is
a bound parameter.
"""

_SELECT_ALL = f"SELECT {_COLUMN_LIST} FROM jobs"
"""Read every column of every job, in ``_COLUMNS`` order."""

_SELECT_BY_ID = f"{_SELECT_ALL} WHERE id = ?"
"""Read a single job by its primary key."""

_SELECT_RECENT = f"{_SELECT_ALL} ORDER BY created_at DESC LIMIT ?"
"""Read the newest jobs first, up to a bound limit."""

_CREATE_CREATED_AT_INDEX = (
    "CREATE INDEX IF NOT EXISTS jobs_created_at ON jobs(created_at)"
)
"""Index the jobs by creation time, so history reads walk it instead of sorting.

Run at every open rather than as a step of ``_MIGRATIONS``: an index changes no
data shape, and keeping it out of the ladder keeps it out of the downgrade
guard, so an earlier release still opens the database.
"""

_LIST_PENDING = f"{_SELECT_ALL} WHERE state = ? ORDER BY created_at ASC"
"""Read the queued jobs oldest-first, the order they will be worked in.

One bound parameter, the state value.  Like ``_PRUNE``'s, the ``ORDER BY`` is
lexicographic over ``created_at``'s ISO-8601 strings and is correct only because
every writer stamps UTC.
"""

_SELECT_LATEST_RUN = (
    f"{_SELECT_ALL} WHERE error_category IS NOT ? ORDER BY created_at DESC LIMIT ?"
)
"""Read the newest jobs that were not rejected at submit, newest first.

Two bound parameters: ``ErrorCategory.REJECTED.value`` and the row limit.
``IS NOT`` rather than ``!=`` because ``NULL != 'X'`` is NULL, and most jobs
have no category, so ``!=`` would hide exactly the jobs this query exists to
return.
"""

_COUNT_JOBS = "SELECT COUNT(*) FROM jobs"
"""Count every job, the read half of ``JobStore.probe``."""

_INSERT = f"INSERT INTO jobs ({_COLUMN_LIST}) VALUES ({_PLACEHOLDERS})"
"""Write one job, naming every column so physical column order never matters."""

_FAIL_ACTIVE = f"UPDATE jobs SET state = ?, error = ? WHERE state IN ({_ACTIVE_MARKS})"
"""Move every still-in-flight job to a failed state with a given error text.

Bound parameters, in this order: the target state value, the error text, then one
per entry of ``_ACTIVE_STATE_VALUES``.  Not ``json_each(?)``: JSON1 was a
compile-time option before SQLite 3.38.
"""

_FAIL_UPLOADING = (
    "UPDATE jobs SET state = ?, error = ?, error_category = ? WHERE state = ?"
)
"""Move every job still uploading to a failed state, with a text and a category.

Run before ``_FAIL_ACTIVE``, because an uploading job may already be in
paperless-ngx and so gets a text and a category of its own.  Bound parameters,
in this order: the target state value, the error text, the category value, then
``JobState.UPLOADING.value``.
"""

_SELECT_STATE_BY_ID = "SELECT state FROM jobs WHERE id = ?"
"""Read one job's state by its primary key, for ``fail_recovered_jobs``."""

_FAIL_RECOVERED = (
    "UPDATE jobs SET state = ?, error = ?, error_category = ? "
    "WHERE id = ? AND state = ?"
)
"""Move one named job to a failed state with its own text and category.

Bound parameters, in this order: the target state value, the error text, the
category value (or NULL), the job id, then the state the row was read in.
Matching that state means the write lands only on the row the text was composed
for.
"""

_NEWEST_RUN_IDS = (
    "SELECT id FROM jobs WHERE error_category IS NOT ? ORDER BY created_at DESC LIMIT ?"
)
"""The ids of the newest jobs that were not refused at submit, up to a limit.

``_PRUNE``'s run-row subquery.  Two bound parameters, in this order:
``ErrorCategory.REJECTED.value`` and the row cap.  ``IS NOT`` for the
NULL-safety reason ``_SELECT_LATEST_RUN`` gives.
"""

_NEWEST_REJECTED_IDS = (
    "SELECT id FROM jobs WHERE error_category = ? ORDER BY rowid DESC LIMIT ?"
)
"""The ids of the most recently written refused submits, up to a limit.

The subquery ``_PRUNE`` and ``_TRIM_REJECTED`` share.  Two bound parameters, in
this order: ``ErrorCategory.REJECTED.value`` and ``REJECTED_HISTORY_ROWS``.

Ordered by ``rowid`` (insertion order), not ``created_at``: on an appliance
whose clock is still behind at boot, an order by time would delete the refused
row just written, which the response names.
"""

_TRIM_REJECTED = (
    f"DELETE FROM jobs WHERE error_category = ? AND id NOT IN ({_NEWEST_REJECTED_IDS})"
)
"""Delete every refused submit outside the newest ``REJECTED_HISTORY_ROWS``.

Three bound parameters, in this order: ``ErrorCategory.REJECTED.value``, then
``ErrorCategory.REJECTED.value`` and ``REJECTED_HISTORY_ROWS`` for the
subquery.  It runs in the same transaction as the write that recorded a
refusal, so a committed table never holds more refused rows than the cap.
"""

_PRUNE = (
    "DELETE FROM jobs WHERE created_at < ? "
    f"OR (error_category IS NOT ? AND id NOT IN ({_NEWEST_RUN_IDS})) "
    f"OR (error_category = ? AND id NOT IN ({_NEWEST_REJECTED_IDS}))"
)
"""Delete every job past the age cutoff or outside its partition's row cap.

Runs (every job not refused at submit, including those with no category) are
capped at the caller's ``max_rows``; refused submits are capped at
``REJECTED_HISTORY_ROWS`` and do not count toward it.  ``IS NOT`` and ``=``
split the table exactly in two.

Seven bound parameters, in this order: the ISO-8601 cutoff; for the run half,
``ErrorCategory.REJECTED.value`` twice and the run-row cap; for the refused
half, ``ErrorCategory.REJECTED.value`` twice and ``REJECTED_HISTORY_ROWS``.

**Every comparison is lexicographic over strings.**  The ``<`` cutoff and the
``ORDER BY`` are correct only because every writer stamps
``datetime.now(tz=UTC).isoformat()``; one non-UTC timestamp in this column makes
both silently wrong.

SQLite evaluates each ``IN (SELECT ... LIMIT ?)`` into a list before the outer
scan, so the delete never sees its own partial deletions.  SQLite does not
document that, so the shuffled-insert-order tests in ``tests/test_job.py`` pin
it.
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

A frozen shape that migration step 2 requires before it upgrades a database.  It
must never grow when a later step adds a column, or step 2 would reject the
databases it had already upgraded.
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

Every one is nullable and carries no ``DEFAULT``: ``NULL`` means "never
recorded", and ``NOT NULL DEFAULT 0`` would backfill history with a measured
zero that never happened.
"""

_V3_COLUMNS: tuple[tuple[str, str], ...] = (("pages_removed_at", "TEXT"),)
"""The one ``(name, SQL type)`` pair migration step 3 adds to the jobs table.

``pages_removed_at`` holds the positions of the pages blank-page detection
removed, as a JSON array of 1-based scanned page numbers in document order.
``NULL`` means "never recorded"; an empty array means "recorded, and none
removed".
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


def _migrate_v3(conn: sqlite3.Connection, db_path: str) -> None:
    """
    Add the removed-page positions column to a jobs table at version 2.

    Args:
        conn: Open connection to the job database.
        db_path: Path the connection was opened on, for logging.

    """
    for name, sqltype in _V3_COLUMNS:
        # Both interpolated values come from _V3_COLUMNS, a module-level
        # literal no caller can reach, and SQL cannot parameterise a column
        # name or a type name.
        conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {sqltype}")
    logger.debug("Added the version 3 removed-pages column to %s", db_path)


_MIGRATIONS: tuple[Callable[[sqlite3.Connection, str], None], ...] = (
    _migrate_v1,
    _migrate_v2,
    _migrate_v3,
)
"""The ordered migration ladder, where ``index + 1`` is the version it produces.

The downgrade guard in ``_open_connection`` reads its length, so a database a
newer step has touched is refused by any release that lacks that step.
"""


def _schema_version(conn: sqlite3.Connection, stamped: int) -> int:
    """
    Return the schema version a job database is really at.

    Args:
        conn: Open connection to the job database.
        stamped: The ``PRAGMA user_version`` the database reports.

    Returns:
        ``stamped``, except that an unstamped database already holding a jobs
        table is at version 1.

    """
    if stamped != 0:
        return stamped
    legacy = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
    ).fetchone()
    return 0 if legacy is None else 1


def _refuse_unsupported_version(db_path: str, version: int) -> None:
    """
    Refuse a schema version no step of this release's ladder can reach.

    The read-write open and the read-only history read both call this, so
    the two refuse the same versions in the same words.

    Args:
        db_path: Path the connection was opened on, named in the message.
        version: The ``PRAGMA user_version`` the database reports.

    Raises:
        StorageError: If ``version`` is newer than this release supports, or
            negative.

    """
    supported = len(_MIGRATIONS)
    if version > supported:
        msg = (
            f"job database at {db_path} is at schema version {version}, "
            f"newer than this release supports ({supported}); "
            "restore a backup or run a newer saneless"
        )
        raise StorageError(msg)
    # user_version is signed, so a foreign tool can stamp a negative value, and
    # the ladder would then index its migrations from the end.
    if version < 0:
        msg = (
            f"job database at {db_path} is at schema version {version}, "
            f"which no saneless release writes (0 to {supported}); "
            "restore a backup"
        )
        raise StorageError(msg)


def _migrate(conn: sqlite3.Connection, db_path: str) -> None:
    """
    Run every outstanding migration step against a job database.

    Args:
        conn: Open connection to the job database.
        db_path: Path the connection was opened on.

    """
    stamped: int = conn.execute("PRAGMA user_version").fetchone()[0]
    version = _schema_version(conn, stamped)
    for index in range(version, len(_MIGRATIONS)):
        _MIGRATIONS[index](conn, db_path)
        # A PRAGMA argument cannot be bound, and index comes from range() over
        # a module-level tuple, so no caller-supplied value reaches this string.
        conn.execute(f"PRAGMA user_version = {index + 1}")
        # Commit per step: a ladder that fails at step N leaves a valid
        # database at version N - 1 rather than a half-applied one.
        conn.commit()
    # The version read opened a read transaction that no step committed on an
    # up-to-date database.  Committing releases its WAL snapshot, so another
    # connection's checkpoint is not held off while this one sits idle.
    conn.commit()


def _open_failure(db_path: str, exc: sqlite3.Error | OSError) -> StorageError:
    """
    Build the StorageError for a job database that cannot be used at open time.

    Args:
        db_path: Path the connection was opened on, named in the message.
        exc: The sqlite3 or OS error that stopped the open.

    Returns:
        A one-line StorageError naming the path and the reason.

    """
    return StorageError(
        f"Could not open the job database at {db_path}: {describe(exc)}"
    )


_AUTO_VACUUM_INCREMENTAL = 2
"""``PRAGMA auto_vacuum``'s answer for INCREMENTAL (0 is NONE, 1 is FULL)."""

_MEMORY_DB = ":memory:"
"""The path SQLite reads as a private in-memory database, always new."""


def _create_private(db_path: str) -> bool:
    """
    Create a new job database file, mode 0600, and report whether this call did.

    SQLite would otherwise create it with the umask's mode, and it holds every
    job title and thumbnail.  Its ``-wal`` and ``-shm`` files take the same
    mode; an existing file keeps its own, since this never chmods.

    Raises:
        StorageError: If the file could not be created for a reason other than
            already existing.

    """
    try:
        os.close(os.open(db_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        return False
    except OSError as exc:
        raise _open_failure(db_path, exc) from exc
    return True


def _open_connection(db_path: str) -> sqlite3.Connection:
    """
    Open a job database connection with WAL and explicit transaction control.

    A missing database file is first created owner-only by
    :func:`_create_private`, and a database this call created gets
    ``auto_vacuum = INCREMENTAL`` before anything is written to it.  A database
    stamped with a schema version newer than this release's migration ladder,
    or negative, is refused before any statement that could write to it.  Only
    this open path is translated: the runtime store methods keep their raw
    sqlite3 errors, which the web worker's degraded-health handling depends on.

    Args:
        db_path: Path to the SQLite database file, or ":memory:".

    Returns:
        The open connection under explicit transaction control.  With
        ``autocommit`` off a deferred transaction is always open, but it
        holds no snapshot until its first read.

    Raises:
        StorageError: If the database cannot be opened or read as SQLite, or
            if its schema version is newer than this release supports or
            negative, in which case the file is left exactly as it was
            found.  The connection, when one was opened, is closed first.

    """
    created = db_path == _MEMORY_DB or _create_private(db_path)
    try:
        conn = sqlite3.connect(db_path, check_same_thread=False)
    except sqlite3.Error as exc:
        raise _open_failure(db_path, exc) from exc
    try:
        conn.row_factory = sqlite3.Row
        # The version check has to come before the WAL pragma: switching a
        # rollback-journal file to WAL rewrites its header, so a refusal made
        # after it would no longer leave the file exactly as it was found.
        version: int = conn.execute("PRAGMA user_version").fetchone()[0]
        _refuse_unsupported_version(db_path, version)
        if created:
            # auto_vacuum can only be switched on for free while the file
            # holds no table yet, so it runs before the WAL header rewrite and
            # any migration.  An existing database is converted separately by
            # enable_incremental_auto_vacuum, since that takes a VACUUM.
            conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
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


def _positions_json(positions: tuple[int, ...] | None) -> str | None:
    """
    Encode removed-page positions for the ``pages_removed_at`` column.

    Args:
        positions: The 1-based scanned page numbers, or None when not recorded.

    Returns:
        A JSON array such as ``"[2, 4]"``, or None so the column stays NULL.

    """
    if positions is None:
        return None
    return json.dumps(list(positions))


def _parse_positions(raw: str | None, job_id: str) -> tuple[int, ...] | None:
    """
    Decode the ``pages_removed_at`` column, refusing anything but page numbers.

    The database is a file on disk, so a value that is not a JSON array of
    integers of at least 1 (``true`` included) is logged and treated as never
    recorded rather than allowed to break every history read.

    Args:
        raw: The stored text, or None for NULL.
        job_id: The row's id, named in the warning.

    Returns:
        The positions as a tuple of ints, or None when NULL or unreadable.

    """
    if raw is None:
        return None
    try:
        decoded = json.loads(raw)
    except ValueError:
        decoded = None
    if isinstance(decoded, list) and all(
        type(item) is int and item >= 1 for item in decoded
    ):
        return tuple(decoded)
    # %r: the stored text came off disk, and a newline in it would otherwise
    # start what reads as a second log line.
    logger.warning(
        "Job %s has an unreadable removed-pages value %r; ignoring it",
        job_id,
        raw,
    )
    return None


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
        removed_positions: The 1-based scanned page numbers blank-page detection
            removed, in document order, once a scan has recorded them.  An
            informational note, never a warning.
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
    removed_positions: tuple[int, ...] | None = None
    warning: str | None = None
    owner_token: str | None = None

    @property
    def is_active(self) -> bool:
        """Whether this job is still in flight (not in a terminal state)."""
        return self.state in ACTIVE_STATES

    @property
    def is_busy(self) -> bool:
        """Whether the machine is working (active, but not waiting for a human)."""
        return self.state in BUSY_STATES


@dataclass(frozen=True)
class JobResult:
    """
    Everything a finished scan recorded, bundled into one argument.

    :meth:`JobStore.finish_job` takes it.  The fields mirror
    :class:`saneless.pipeline.ScanResult` without importing it, so neither
    module imports the other; the worker copies the fields across.

    Attributes:
        outcome: How the scan resolved.
        warning: A note about something odd that did not fail the scan.
        pages_scanned: Pages the scanner produced.
        pages_removed: Pages discarded as blank.
        pages_uploaded: Pages sent to paperless-ngx.
        removed_positions: The 1-based scanned page numbers removed as blank,
            in document order, or None when the run did not record them.

    """

    outcome: ScanOutcome | None
    warning: str | None
    pages_scanned: int | None
    pages_removed: int | None
    pages_uploaded: int | None
    removed_positions: tuple[int, ...] | None = None


def _locked[**P, R](
    method: Callable[Concatenate[JobStore, P], R],
) -> Callable[Concatenate[JobStore, P], R]:
    """
    Serialise a JobStore method on the store's lock and mark it ``_LOCKED_MARKER``.

    It does not also open the connection's transaction context, because
    ``Connection.__exit__`` raises on a closed database and ``close()`` could
    then never be decorated.  Each method that executes SQL opens its own
    ``with self._conn:``.
    """

    @functools.wraps(method)
    def wrapper(self: JobStore, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._lock:
            return method(self, *args, **kwargs)

    setattr(wrapper, _LOCKED_MARKER, True)
    return wrapper


def _job_from_row(row: sqlite3.Row) -> Job:
    """
    Convert one jobs row into a Job.

    A plain function, holding no lock, so the read-only history read maps rows
    exactly as :meth:`JobStore.list_recent` does.  Every access is by name:
    physical column order differs between migrated and fresh databases.

    Args:
        row: A jobs row, fetched with ``sqlite3.Row`` as the row factory.

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
        removed_positions=_parse_positions(row["pages_removed_at"], row["id"]),
        warning=row["warning"],
        owner_token=row["owner_token"],
    )


def _open_uri(uri: str) -> tuple[sqlite3.Connection, int]:
    """
    Connect to a ``file:`` URI and read the database's schema version.

    Args:
        uri: The ``file:`` URI to connect to.

    Returns:
        The open connection, with ``sqlite3.Row`` rows, and the database's
        ``PRAGMA user_version``.

    """
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        version: int = conn.execute("PRAGMA user_version").fetchone()[0]
    except BaseException:
        conn.close()
        raise
    return conn, version


def _has_side_files(db_path: Path) -> bool:
    """
    Report whether a job database has a ``-wal`` or ``-shm`` file beside it.

    Args:
        db_path: Path of the job database file.

    Returns:
        True when either side file exists.

    """
    return any(
        db_path.with_name(db_path.name + suffix).exists() for suffix in ("-wal", "-shm")
    )


def _immutable_read_is_safe(db_path: Path) -> bool:
    """
    Report whether a job database can be read with ``immutable=1``.

    Args:
        db_path: Path of the job database file.

    Returns:
        True only when the database's directory is not writable and neither
        its ``-wal`` nor its ``-shm`` file exists.

    """
    return not os.access(db_path.parent, os.W_OK) and not _has_side_files(db_path)


def _connect_read_only(db_path: Path, owner: int) -> tuple[sqlite3.Connection, int]:
    """
    Open a job database read-only and read its schema version.

    Args:
        db_path: Path of an existing job database file.
        owner: The user id that owns the file.

    Returns:
        The open connection and the database's ``PRAGMA user_version``.

    """
    # as_uri() percent-encodes the path, so a '?', '#' or space in it stays
    # part of the file name rather than starting the URI's query or fragment.
    uri = db_path.resolve().as_uri() + "?mode=ro"
    # A read-only connection to a WAL database leaves -wal and -shm files it
    # cannot remove, and if root left them the server could no longer write its
    # own database.  So a reader who does not own the file, finding no side
    # files, reads it immutable and creates none; a checkpoint landing mid-read
    # can at worst spoil this one read, never the file.
    if owner != os.geteuid() and not _has_side_files(db_path):
        return _open_uri(uri + "&immutable=1")
    try:
        return _open_uri(uri)
    except sqlite3.OperationalError:
        if not _immutable_read_is_safe(db_path):
            raise
    # A read-only connection still creates -shm, so in an unwritable directory
    # it fails.  There, with no side files, immutable=1 is safe: no writer can
    # create a -wal, and without one no committed page lives outside the file.
    return _open_uri(uri + "&immutable=1")


def read_recent_jobs(db_path: Path, limit: int) -> list[Job]:
    """
    Read the most recent jobs, newest first, without ever writing.

    The listing can run beside a server that has the same database open, so
    it must never change the file under it: it opens the database read-only,
    and never creates the file, switches its journal mode, sets
    ``auto_vacuum`` or runs a migration step.  What it may leave behind is
    stated here, because SQLite decides it: read by the user who owns the
    database, with no server holding it, a WAL database gains the empty
    ``-wal`` and the ``-shm`` index a read-only connection creates and cannot
    remove, owned by that same user, and the next writer reuses both.  Any
    other user, root included, reads without creating either, so it never
    leaves a file the server cannot write in the server's folder.  Upgrading
    a database belongs to the server that writes it, so one at an older
    schema is refused rather than migrated.  A missing file, or one with no
    jobs table yet, is an empty history; a file that cannot be looked up for
    any other reason is an error, never an empty history.

    Args:
        db_path: Path of the job database file.
        limit: Maximum number of jobs to return.

    Returns:
        The jobs, newest first, mapped exactly as
        :meth:`JobStore.list_recent` maps them.

    Raises:
        StorageError: If the database cannot be looked up, opened or read, or
            its schema version is older than this release reads, newer than
            it supports, or negative.  Every message names ``db_path``.  The
            database file itself is left exactly as it was found.

    """
    name = str(db_path)
    # Only a file that is not there is an empty history.  Path.exists() also
    # answers False for a lookup that failed, which would print an empty table.
    try:
        owner = db_path.stat().st_uid
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise _open_failure(name, exc) from exc
    try:
        conn, stamped = _connect_read_only(db_path, owner)
    except sqlite3.Error as exc:
        raise _open_failure(name, exc) from exc
    try:
        _refuse_unsupported_version(name, stamped)
        version = _schema_version(conn, stamped)
        if version == 0:
            return []
        supported = len(_MIGRATIONS)
        if version < supported:
            msg = (
                f"job database at {name} is at schema version {version}, "
                f"older than this saneless reads ({supported}); "
                "only saneless serve upgrades a job database: start it once "
                "with the same config, stop it, then run saneless jobs again"
            )
            raise StorageError(msg)
        rows = conn.execute(_SELECT_RECENT, (limit,)).fetchall()
        return [_job_from_row(row) for row in rows]
    except sqlite3.Error as exc:
        raise _open_failure(name, exc) from exc
    finally:
        conn.close()


class JobStore:
    """
    SQLite-backed persistence for scan jobs.

    Args:
        db_path: Path to SQLite database file, or ":memory:" for
            in-memory storage (default).

    """

    def __init__(self, db_path: Path | str = ":memory:") -> None:
        """
        Open the job store, enable WAL, run the ladder, and index the history.

        Raises:
            StorageError: If the database cannot be opened or read as SQLite,
                its schema version is newer than this release supports or
                negative (the file is then left untouched), or its jobs table
                has an unsupported shape.  Every message names ``db_path``.

        """
        db_path = os.fspath(db_path)
        self._lock = threading.RLock()
        self._conn = _open_connection(db_path)
        try:
            _migrate(self._conn, db_path)
            self._conn.execute(_CREATE_CREATED_AT_INDEX)
            self._conn.commit()
        except Exception as exc:
            # Release the transaction and file handle before the caller sees
            # the failure.  A rollback that fails too must not replace the
            # open's StorageError with a raw sqlite3 error, which the CLI maps
            # to ExitCode.UNEXPECTED rather than ExitCode.CONFIG.
            with contextlib.suppress(sqlite3.Error):
                self._conn.rollback()
            self._conn.close()
            if isinstance(exc, sqlite3.Error):
                raise _open_failure(db_path, exc) from exc
            raise

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        """
        Convert one jobs row into a Job through the module's one row mapper.

        Args:
            row: A jobs row fetched over this store's own connection.

        Returns:
            The job the row records.

        """
        return _job_from_row(row)

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

        A new job has no thumbnail: :meth:`update_thumbnail` writes it once
        the worker has a scanned image.

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
                    None,  # thumbnail -- update_thumbnail writes it later
                    datetime.now(tz=UTC).isoformat(),
                    # NULL means "never recorded", not a measured zero;
                    # finish_job writes these once the run ends.
                    None,  # outcome
                    None,  # pages_scanned
                    None,  # pages_removed
                    None,  # pages_uploaded
                    None,  # warning
                    # owner_token's only writer.  NULL means unowned: the flip
                    # prompt renders for every browser.  A footgun guard, not
                    # a login.  See
                    # docs/explanation/decisions/0014-owner-token-not-a-login.md.
                    owner_token,
                    None,  # pages_removed_at -- finish_job's to write
                ),
            )
            # Read back inside the same transaction, so the row mapping stays
            # in _row_to_job alone.
            row = self._conn.execute(_SELECT_BY_ID, (job_id,)).fetchone()
        job = self._row_to_job(row)
        # %r, not %s: a newline in the untrusted title would forge a second
        # log line.
        logger.debug("Created job %s: %r", job.id, job.title)
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

        The row is written already terminal, ``ERROR`` with
        ``ErrorCategory.REJECTED``, which :meth:`latest_run_job` skips so the
        rejection never replaces the job that just ended in the status area.

        One ``INSERT``, not :meth:`create_job` then :meth:`finish_job`: if the
        second transaction raised, the first would leave a ``PENDING`` row no
        worker ever runs, disabling the Scan button for good.  The same
        transaction trims refused rows to ``REJECTED_HISTORY_ROWS``.

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
                    None,  # outcome
                    None,  # pages_scanned
                    None,  # pages_removed
                    None,  # pages_uploaded
                    None,  # warning
                    None,  # owner_token
                    None,  # pages_removed_at
                ),
            )
            # Read back before the trim, so the read can never miss the row it
            # just wrote.
            row = self._conn.execute(_SELECT_BY_ID, (job_id,)).fetchone()
            self._trim_rejected()
        job = self._row_to_job(row)
        # %r for the same reason as create_job's.
        logger.debug("Created rejected job %s: %r", job.id, job.title)
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

        The only writer of the result columns: naming them in
        :meth:`update_state`'s unconditional ``SET`` would blank them on every
        in-flight transition.

        Everything lands in one ``UPDATE``, because the web request thread
        reads this row while the worker writes it and must never see a job
        that finished without recording how.  Recording
        ``ErrorCategory.REJECTED`` also trims the refused rows to their cap, in
        the same transaction.

        Omitting ``result`` leaves the result columns NULL ("never recorded")
        rather than zero ("counted, and there were none").

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
                "pages_removed_at = ?, error = ?, error_category = ? WHERE id = ?",
                (
                    state.value,
                    result.outcome.value if result and result.outcome else None,
                    result.warning if result else None,
                    result.pages_scanned if result else None,
                    result.pages_removed if result else None,
                    result.pages_uploaded if result else None,
                    _positions_json(result.removed_positions if result else None),
                    error,
                    error_category.value if error_category else None,
                    job_id,
                ),
            )
            if error_category is ErrorCategory.REJECTED:
                # A job refused after its row existed joins the refused rows
                # and their cap.
                self._trim_rejected()
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
    def list_recent(self, limit: int = WEB_HISTORY_LIMIT) -> list[Job]:
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

    def _trim_rejected(self) -> None:
        """
        Delete refused rows past ``REJECTED_HISTORY_ROWS``, in the open transaction.

        Unlocked and without a ``with self._conn:`` of its own: its callers
        already hold the lock and a transaction, and the trim must commit with
        the write that recorded the refusal.
        """
        rejected = ErrorCategory.REJECTED.value
        trimmed = self._conn.execute(
            _TRIM_REJECTED, (rejected, rejected, REJECTED_HISTORY_ROWS)
        ).rowcount
        if trimmed > 0:
            # fetchall() steps the pragma to completion; see prune.
            self._conn.execute("PRAGMA incremental_vacuum").fetchall()

    def _pending_jobs(self) -> list[Job]:
        """
        Read the queue oldest-first, without taking the lock.

        The shared body of :meth:`list_pending` and :meth:`queue_position`, so
        a job's position and the list it is a position into cannot disagree.
        A public method may not call another public one: ``sqlite3`` connection
        context managers do not nest, so an inner ``with`` commits early.
        """
        with self._conn:
            rows = self._conn.execute(
                _LIST_PENDING, (JobState.PENDING.value,)
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    @_locked
    def list_pending(self) -> list[Job]:
        """
        Fetch the queued jobs oldest-first.

        The oldest entry is the next one to be worked.  :meth:`queue_position`
        reads the same ordering.

        Returns:
            List of Job instances awaiting a scanner, ordered by creation time
            ascending.

        """
        return self._pending_jobs()

    @_locked
    def queue_position(self, job_id: str, *, running: str | None = None) -> int | None:
        """
        Count the queued jobs ahead of one job.

        Zero-based: the job at the head of the queue answers ``0``.  A job that
        has left ``PENDING`` and an id no row carries both answer ``None``.

        Walks :meth:`list_pending`'s ordering rather than asking the database
        for a rank, so there is one definition of "ahead".

        The job the worker has taken can still read ``PENDING`` until it holds
        the scanner gate.  The caller names it in ``running``; it is never
        counted ahead of anyone, and asked about itself it answers ``None``.

        Args:
            job_id: The job to locate in the queue.
            running: The job the worker is running, or None when it is idle.

        Returns:
            The zero-based number of pending jobs ahead of job_id, or None when
            that job is not waiting.

        """
        queue = (job for job in self._pending_jobs() if job.id != running)
        return next(
            (position for position, job in enumerate(queue) if job.id == job_id),
            None,
        )

    @_locked
    def latest_run_job(self, exclude_ids: AbstractSet[str] = frozenset()) -> Job | None:
        """
        Fetch the newest job that was not rejected at submit.

        The status area falls back to this once no job is active.  Not
        ``list_recent(1)``: a submit refused while a job runs writes a newer
        ``ErrorCategory.REJECTED`` row, which would replace the job that just
        ended.

        Excluded ids are filtered in Python, never interpolated into the SQL.
        Reading ``len(exclude_ids) + 1`` rows is enough: at most that many
        newer rows can be skipped.

        Args:
            exclude_ids: Ids to treat as never run, such as refused submits
                whose REJECTED write is still owed to the worker.  A set, not
                any collection, because a bare id string would exclude its
                single characters.

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
        The write appends a WAL frame, so a full disk or a file gone read-only
        fails the probe; nothing observable changes on a healthy store.  A
        clean return clears the worker's degraded health.

        Raises:
            sqlite3.Error: Whatever sqlite raises when the store cannot be read
                or written, including ``sqlite3.ProgrammingError`` once the
                store is closed.

        """
        with self._conn:
            self._conn.execute(_COUNT_JOBS).fetchone()
            version: int = self._conn.execute("PRAGMA user_version").fetchone()[0]
            # A PRAGMA argument cannot be bound; version is an int read back
            # from this database, never request input.
            self._conn.execute(f"PRAGMA user_version = {version}")

    @_locked
    def fail_active_jobs(self) -> int:
        """
        Fail every job still in flight, for recovery after an unclean restart.

        A job left mid-scan by a killed process has no worker behind it, so the
        UI would otherwise poll it for ever.  Every row whose state is in
        ``ACTIVE_STATES`` moves to ``JobState.ERROR`` in one transaction,
        worded by ``restart_error`` and ``restart_category`` for the state it
        was left in.  Because ``ACTIVE_STATES`` and ``TERMINAL_STATES``
        partition ``JobState``, a completed job's history is never touched.

        ``error_category`` is written only for the UPLOADING rows: such a job
        may already be in paperless-ngx and needs the amber category's advice,
        while any other row shows its own precise text.

        Returns:
            How many jobs were moved to ERROR.

        """
        category = restart_category(JobState.UPLOADING)
        with self._conn:
            failed = self._conn.execute(
                _FAIL_UPLOADING,
                (
                    JobState.ERROR.value,
                    restart_error(JobState.UPLOADING, None),
                    category.value if category else None,
                    JobState.UPLOADING.value,
                ),
            ).rowcount
            failed += self._conn.execute(
                _FAIL_ACTIVE,
                (JobState.ERROR.value, RESTART_REASON, *_ACTIVE_STATE_VALUES),
            ).rowcount

        if failed > 0:
            logger.debug("Failed %d in-flight job(s) after a restart", failed)
        return failed

    @_locked
    def fail_recovered_jobs(self, kept: Mapping[str, str]) -> int:
        """
        Fail each named job still in flight, each naming where its pages went.

        Startup's workspace recovery calls this before ``fail_active_jobs``, so
        a job whose pages were kept in ``failed/`` says where.  Only a row whose
        id is in ``kept`` *and* whose state is in ``ACTIVE_STATES`` moves to
        ``JobState.ERROR``; an id with no row is ignored.  Each state is read in
        the same transaction it is written in, and the category follows
        ``fail_active_jobs``.

        Args:
            kept: The sentence naming the kept file, by job id.

        Returns:
            How many jobs were moved to ERROR.

        """
        failed = 0
        with self._conn:
            for job_id, sentence in kept.items():
                row = self._conn.execute(_SELECT_STATE_BY_ID, (job_id,)).fetchone()
                if row is None or row[0] not in _ACTIVE_STATE_VALUES:
                    continue
                state = JobState(row[0])
                category = restart_category(state)
                failed += self._conn.execute(
                    _FAIL_RECOVERED,
                    (
                        JobState.ERROR.value,
                        restart_error(state, sentence),
                        category.value if category else None,
                        job_id,
                        state.value,
                    ),
                ).rowcount

        if failed > 0:
            logger.debug("Failed %d in-flight job(s) with recovered pages", failed)
        return failed

    @_locked
    def prune(self, max_age_days: int = 7, max_rows: int = 500) -> int:
        """
        Remove old jobs by age and count limits.

        One ``DELETE`` removes every job older than ``max_age_days`` together
        with every run outside the newest ``max_rows`` and every refused submit
        outside the newest ``REJECTED_HISTORY_ROWS``; ``_PRUNE`` states the UTC
        assumption it rests on.  The count comes from that one statement, so
        no insert, even on another connection, can land between a count and
        the delete.

        When rows went, an incremental vacuum in the same transaction returns
        their pages to the file system.  On a database not yet converted by
        :meth:`enable_incremental_auto_vacuum` the pragma is a no-op.

        Args:
            max_age_days: Maximum age in days before a job is pruned.
            max_rows: Maximum number of runs to retain.  Refused submits are
                kept under ``REJECTED_HISTORY_ROWS`` instead.

        Returns:
            Total number of jobs deleted.

        """
        cutoff = (datetime.now(tz=UTC) - timedelta(days=max_age_days)).isoformat()
        rejected = ErrorCategory.REJECTED.value
        with self._conn:
            deleted = self._conn.execute(
                _PRUNE,
                (
                    cutoff,
                    rejected,
                    rejected,
                    max_rows,
                    rejected,
                    rejected,
                    REJECTED_HISTORY_ROWS,
                ),
            ).rowcount
            if deleted > 0:
                # The fetchall() is load-bearing: incremental_vacuum frees one
                # page per step, and execute() alone takes only the first step.
                self._conn.execute("PRAGMA incremental_vacuum").fetchall()

        if deleted > 0:
            logger.debug("Pruned %d old jobs", deleted)
        return deleted

    @_locked
    def enable_incremental_auto_vacuum(self) -> bool:
        """
        Convert an older job database to ``auto_vacuum = INCREMENTAL``, once.

        A database with ``auto_vacuum = NONE`` keeps the pages its deletes
        free; switching it over takes a full ``VACUUM``, which rewrites it.

        ``VACUUM`` cannot run inside a transaction, so the method commits and
        runs both statements with ``autocommit`` on, restoring explicit
        transaction control afterwards whatever happens.  ``VACUUM`` needs free
        space roughly the database's size, so server callers catch
        ``sqlite3.Error`` and carry on.  This bumps no schema version, so
        older releases still open the file.

        Returns:
            True if the database was converted, False if it already was
            INCREMENTAL and nothing was done.

        """
        mode: int = self._conn.execute("PRAGMA auto_vacuum").fetchone()[0]
        if mode == _AUTO_VACUUM_INCREMENTAL:
            return False
        # Announced before it starts: on an SD card the rewrite can outlast a
        # container health check's start period.
        pages: int = self._conn.execute("PRAGMA page_count").fetchone()[0]
        page_size: int = self._conn.execute("PRAGMA page_size").fetchone()[0]
        logger.info(
            "Converting the job database (%d MiB) to incremental auto-vacuum; "
            "this runs once and rewrites the whole file",
            pages * page_size // 2**20,
        )
        self._conn.commit()
        self._conn.autocommit = True
        try:
            self._conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
            self._conn.execute("VACUUM")
        finally:
            self._conn.autocommit = False
        logger.info("Converted the job database to incremental auto-vacuum")
        return True

    @_locked
    def close(self) -> None:
        """Close the database connection."""
        # No `with self._conn:`: Connection.__exit__ would then raise on the
        # connection the body has already closed.
        self._conn.close()
