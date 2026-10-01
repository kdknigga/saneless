"""
Scan jobs and their SQLite persistence.

Each job carries a persisted ``state`` column that the worker drives through
the lifecycle, from PENDING through scanning, assembling and uploading to a
terminal state such as DONE or ERROR.  The store records each transition it is
given and does not check that it is a legal one; the order lives in the
worker.  The rows are also the
history.  A job survives the process that ran it only as a row: on startup the
web app fails every job still in an active state through ``fail_active_jobs``
before the worker starts.
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

Spelled in exactly one place.  The decorator that sets it and the reflective
test that looks for it both read this constant, so the marker name cannot drift
between source and test, and ``setattr`` through a constant is also what keeps
ruff's ``B010`` and both type checkers quiet -- a direct
``wrapper.__saneless_locked__ = True`` makes the checkers complain that a
function has no such attribute, and a string literal in ``setattr`` trips
``B010``.
"""

# How many jobs each history view shows.  The web page and ``saneless jobs``
# show different amounts by design -- a page has room for a longer list than a
# terminal -- so they are two constants, not one.  Neither is configurable.
WEB_HISTORY_LIMIT: Final = 50
CLI_JOBS_DEFAULT_LIMIT: Final = 20

# How many refused submits -- ERROR rows with ``ErrorCategory.REJECTED`` -- the
# store keeps, newest first.  They sit under this cap of their own rather than
# under ``history_max_rows``, so a flood of refusals can never evict a real scan.
# Smaller than WEB_HISTORY_LIMIT, so such a flood can never fill the visible
# history table on its own either.  Not configurable.
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
frozenset later is picked up by ``_FAIL_ACTIVE`` without anyone
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
consumed by ``_FAIL_ACTIVE``, ``_FAIL_UPLOADING`` and ``_FAIL_RECOVERED`` below.
Its safety argument is ``_SELECT_JOBS``'s.
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

_CREATE_CREATED_AT_INDEX = (
    "CREATE INDEX IF NOT EXISTS jobs_created_at ON jobs(created_at)"
)
"""Index the jobs by creation time, so history reads walk it instead of sorting.

Without it every ``_SELECT_RECENT`` sorts the whole table in a temporary
B-tree while the store lock is held.  It is run at every open rather than as a
step of ``_MIGRATIONS``: an index changes no data shape, so it needs no schema
version, and keeping it out of the ladder keeps it out of the downgrade guard
that reads the ladder's length.  An earlier release therefore still opens the
database -- SQLite maintains an index whether or not the code knows about it.
"""

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
integer ``latest_run_job`` derives from how many ids it excludes.
Neither is ever written into the statement text.
``IS NOT`` rather than ``!=`` because it is NULL-safe in SQLite: ``NULL != 'X'``
is NULL and would drop the row, while ``NULL IS NOT 'X'`` is true.  Every job
that has not failed, and every job ``fail_active_jobs`` failed on restart before
its upload, has no category, so ``!=`` would silently hide exactly the jobs this
query exists to return.  Its safety argument is ``_SELECT_JOBS``'s: the only interpolated value
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

_FAIL_UPLOADING = (
    f"{_UPDATE_JOBS} SET state = ?, error = ?, error_category = ? WHERE state = ?"
)
"""Move every job still uploading to a failed state, with a text and a category.

Run before ``_FAIL_ACTIVE``, because an uploading job may already be in
paperless-ngx and so gets a text and a category of its own.  Bound parameters,
in this order: the target state value, the error text, the category value, then
``JobState.UPLOADING.value``.  Its safety argument is ``_FAIL_ACTIVE``'s, and
simpler: the only interpolated value is the module-level ``_UPDATE_JOBS``
literal.  Every runtime value is a bound parameter.
"""

_SELECT_STATE_BY_ID = f"{_SELECT_JOBS} state FROM jobs WHERE id = ?"
"""Read one job's state by its primary key, for ``fail_recovered_jobs``.

One bound parameter, the job id.  Its safety argument is ``_SELECT_JOBS``'s:
the only interpolated value is that module-level literal.
"""

_FAIL_RECOVERED = (
    f"{_UPDATE_JOBS} SET state = ?, error = ?, error_category = ? "
    "WHERE id = ? AND state = ?"
)
"""Move one named job to a failed state with its own text and category.

The sibling of ``_FAIL_UPLOADING`` for one named row.  Bound parameters, in
this order: the target state value, the error text, the category value (or
NULL), the job id, then the state the row was read in.  Matching that state
means the write lands only on the row the text was composed for.  Its safety
argument is ``_FAIL_UPLOADING``'s: the only interpolated value is the
module-level ``_UPDATE_JOBS`` literal.  The id and the text -- which quotes a
title read back from a workspace on disk -- are bound, never written into the
statement.
"""

_DELETE_BY_ID = f"{_DELETE_JOBS} WHERE id = ?"
"""Delete a single job by its primary key.

One bound parameter, the job id.  Its safety argument is ``_SELECT_JOBS``'s: the
only interpolated value is the module-level ``_DELETE_JOBS`` literal.
"""

_NEWEST_RUN_IDS = (
    f"{_SELECT_JOBS} id FROM jobs WHERE error_category IS NOT ? "
    "ORDER BY created_at DESC LIMIT ?"
)
"""The ids of the newest jobs that were not refused at submit, up to a limit.

``_PRUNE``'s run-row subquery.  Two bound parameters, in this order:
``ErrorCategory.REJECTED.value`` and the row cap.  ``IS NOT`` rather than
``!=`` for the reason ``_SELECT_LATEST_RUN`` gives: ``NULL != 'X'`` is NULL, so
``!=`` would leave out every job with no category -- nearly every run -- and
those jobs would then never count toward the cap.

Held apart from ``_PRUNE`` for the same reason ``_SELECT_JOBS`` holds the verb:
``S608`` matches ``select ... from`` anywhere in an interpolated string's
literal text, not only at its start, so a nested ``SELECT`` written inline would
trip it -- and a lint suppression is not available to silence it.  Behind a
name, both halves are ordinary constants and the rule has nothing to flag.
"""

_NEWEST_REJECTED_IDS = (
    f"{_SELECT_JOBS} id FROM jobs WHERE error_category = ? ORDER BY rowid DESC LIMIT ?"
)
"""The ids of the most recently written refused submits, up to a limit.

The subquery ``_PRUNE`` and ``_TRIM_REJECTED`` share.  Two bound parameters, in
this order: ``ErrorCategory.REJECTED.value`` and ``REJECTED_HISTORY_ROWS``.
Plain ``=`` is right here, because it is meant to leave out every NULL
category.  Named apart for the ``S608`` reason ``_NEWEST_RUN_IDS`` gives.

Ordered by ``rowid``, which is insertion order, and not by ``created_at``.
``_TRIM_REJECTED`` runs in the transaction that wrote a refused row, and the
response names that row.  On an appliance whose clock is still behind at
boot, before NTP, the new row's ``created_at`` sorts below the rows already
kept, and an order by time would delete the row just written.
"""

_TRIM_REJECTED = (
    f"{_DELETE_JOBS} WHERE error_category = ? AND id NOT IN ({_NEWEST_REJECTED_IDS})"
)
"""Delete every refused submit outside the newest ``REJECTED_HISTORY_ROWS``.

Three bound parameters, in this order: ``ErrorCategory.REJECTED.value``, then
``ErrorCategory.REJECTED.value`` and ``REJECTED_HISTORY_ROWS`` for the
subquery.  Its safety argument is ``_SELECT_JOBS``'s: the only interpolated
values are the module-level ``_DELETE_JOBS`` literal and the module-level
subquery, and every runtime value is bound.  It runs in the same transaction
as the write that recorded a refusal, so once that write commits the table
never holds more refused rows than the cap.
"""

_PRUNE = (
    f"{_DELETE_JOBS} WHERE created_at < ? "
    f"OR (error_category IS NOT ? AND id NOT IN ({_NEWEST_RUN_IDS})) "
    f"OR (error_category = ? AND id NOT IN ({_NEWEST_REJECTED_IDS}))"
)
"""Delete every job past the age cutoff or outside its partition's row cap.

The table has two partitions.  Runs -- every job not refused at submit,
including every job with no category -- are capped at the caller's
``max_rows``.  Refused submits are capped at ``REJECTED_HISTORY_ROWS`` and do
not count toward ``max_rows``, so a flood of refusals can never push a real
scan out of history.  ``IS NOT`` and ``=`` split the table exactly in two, for
the NULL-safety reason ``_NEWEST_RUN_IDS`` gives.

Seven bound parameters, in this order: the ISO-8601 cutoff; for the run half,
``ErrorCategory.REJECTED.value`` twice and the run-row cap; for the refused
half, ``ErrorCategory.REJECTED.value`` twice and ``REJECTED_HISTORY_ROWS``.
Its safety argument is ``_SELECT_JOBS``'s -- the only interpolated values are
the module-level ``_DELETE_JOBS`` literal and the two module-level subqueries,
and every runtime value is a bound ``?``.

**Every comparison is lexicographic over strings.**  ``created_at`` is written
as ``datetime.now(tz=UTC).isoformat()``, so every value ends ``+00:00``, and the
``<`` cutoff and the ``ORDER BY`` are correct *only* because of that uniformity.
A single non-UTC timestamp reaching this column -- a ``-05:00`` offset, say --
would make both the cutoff and the ordering silently wrong, and nothing here
would raise.  Anyone adding a writer to this column meets this note first.

The predicates are a *union*, not a sequence, and within the run partition that
is equivalent to deleting by age and then trimming to a row cap: both order by
``created_at``, so the partition's age-expired rows are always a prefix of its
oldest and the union of the two sets is exactly what the sequential form
produced.  The refused partition keeps its newest rows by insertion order, for
the reason ``_NEWEST_REJECTED_IDS`` gives, which matches ``created_at`` order
unless the clock stepped back between two refusals.  The equivalence rests in turn on SQLite evaluating each
``IN (SELECT ... ORDER BY ... LIMIT ?)`` right-hand side into a ``LIST
SUBQUERY`` before the outer scan begins, so it never observes its own partial
deletions.  SQLite does not document that as a guarantee, so it is pinned by the
shuffled-insert-order tests in ``tests/test_job.py`` rather than by contract: if
a future libsqlite changes the plan, those go red instead of this statement
quietly under-deleting.
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

_V3_COLUMNS: tuple[tuple[str, str], ...] = (("pages_removed_at", "TEXT"),)
"""The one ``(name, SQL type)`` pair migration step 3 adds to the jobs table.

``pages_removed_at`` holds the positions of the pages blank-page detection
removed, as a JSON array of 1-based scanned page numbers in document order.
It is nullable and carries no ``DEFAULT``: ``NULL`` means "never recorded",
which is true of every row written before this migration and of every job
that recorded no result.  An empty array means "recorded, and none removed".
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

    Every existing row reads ``NULL`` afterwards, which is the truth: none of
    them recorded which pages were removed.

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

Every step takes the database path as well as the connection so the tuple
stays homogeneous, even though only step 2 has anything to name in a failure.
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
        table is at version 1: nothing ever stamped user_version, so an
        existing jobs table is at version 1 rather than at nothing.

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
    # user_version is a signed 32-bit field, so a foreign tool can stamp a
    # negative value. The ladder would index its migrations from the end
    # for one, so it is refused here with the too-new case.
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
        # A PRAGMA argument cannot be bound -- "PRAGMA user_version = ?" is a
        # syntax error -- and index comes from range() over a module-level
        # tuple, so no caller-supplied value reaches this string.
        conn.execute(f"PRAGMA user_version = {index + 1}")
        # Commit per step: a ladder that fails at step N leaves a valid
        # database at version N - 1 rather than a half-applied one.
        conn.commit()
    # The version read above opened a deferred read transaction, and on an
    # up-to-date database no step committed it.  Committing here releases its
    # WAL snapshot, so another connection's checkpoint is not held off for as
    # long as this one sits idle.
    conn.commit()


def _open_failure(db_path: str, exc: sqlite3.Error | OSError) -> StorageError:
    """
    Build the StorageError for a job database that cannot be used at open time.

    Args:
        db_path: Path the connection was opened on, named in the message.
        exc: The sqlite3 error that stopped the open, or the OS error that
            stopped the private pre-create of a new database file or the
            read-only listing's lookup of an existing one.

    Returns:
        A one-line StorageError naming the path and sqlite's reason.
        The CLI guard maps StorageError to exit 2 by type.

    """
    return StorageError(
        f"Could not open the job database at {db_path}: {describe(exc)}"
    )


_AUTO_VACUUM_INCREMENTAL = 2
"""``PRAGMA auto_vacuum``'s answer for INCREMENTAL (0 is NONE, 1 is FULL)."""

_MEMORY_DB = ":memory:"
"""The path SQLite reads as a private in-memory database rather than a file.

Such a database is always new, and there is no file for a mode to apply to.
"""


def _create_private(db_path: str) -> bool:
    """
    Create a new job database file readable by its owner alone.

    SQLite would create a missing file itself, with a mode the umask decides --
    0644 under the usual 022 -- and the database holds every job title and
    thumbnail.  Creating the empty file first, exclusively and with mode 0600,
    closes that: SQLite accepts a zero-byte file as an empty database, and it
    gives the ``-wal`` and ``-shm`` files it later creates the database file's
    own mode.  A mode passed to ``open`` is only ever narrowed by the umask, so
    0600 holds under any of them.

    A file that already exists keeps the mode it has, including a 0644 one an
    earlier release created: this is the create path only, never a chmod.  The
    same answer covers another opener winning a race to create it.

    Args:
        db_path: Path of the database file to create.

    Returns:
        True if this call created the file, False if it already existed.

    Raises:
        StorageError: If the file could not be created for any other reason,
            such as a missing parent directory or a permission refusal.

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
    ``auto_vacuum = INCREMENTAL`` before anything is written to it.  A missing
    parent fails at that pre-create; a directory fails at ``connect``; a file
    that is not SQLite fails at the ``user_version`` read,
    the first statement that reads it.  A database stamped with a schema
    version newer than this release's migration ladder, or negative, is
    refused before any statement that could write to it.  Only this open
    path is translated: the runtime store methods keep their raw sqlite3
    errors, which the web worker's degraded-health handling depends on.

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
        # The except BaseException arm below closes the connection.
        version: int = conn.execute("PRAGMA user_version").fetchone()[0]
        _refuse_unsupported_version(db_path, version)
        if created:
            # auto_vacuum can only be switched on for free while the file
            # holds no table yet, so it runs before the WAL header rewrite and
            # before any migration creates one.  INCREMENTAL lets a delete hand
            # its freed pages back to the file system; an existing database is
            # converted separately by enable_incremental_auto_vacuum, since
            # that takes a VACUUM.
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

    The column is only ever written by :meth:`JobStore.finish_job`, but the
    database is a file on disk, so it is read defensively: a value that is not
    a JSON array of integers, each at least 1, is logged and treated as never
    recorded rather than rendered or allowed to break every history read.  A
    JSON ``true`` is refused too, although Python counts ``bool`` as ``int``.

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
    the ceiling and that ``self`` is not counted; spelling these six facts out
    as individual parameters alongside ``job_id``, ``state``, ``error`` and
    ``error_category`` would make ten.

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
        removed_positions: The 1-based scanned page numbers removed as blank,
            in document order, or None when the run did not record them.
            Defaulted, so a caller with nothing to say about them need not.

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


def _job_from_row(row: sqlite3.Row) -> Job:
    """
    Convert one jobs row into a Job.

    The only row-to-``Job`` conversion in this module.  It is a plain
    function rather than a store method so the read-only history read, which
    has no store, maps rows exactly as :meth:`JobStore.list_recent` does.  It
    holds no lock and opens no transaction, which is what lets a store method
    call it from inside its own ``with conn:``.

    Every access is by name.  A database migrated from the pre-``thumbnail``
    shape by the old bare ``ALTER`` carries ``error_category`` last while a
    freshly created one carries it sixth, so physical column order is not a
    contract and no ordinal may be used here.  (``sqlite3.Row`` keys are
    case-insensitive; nothing here relies on that, and nothing should.)

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

    The version read is the first statement, and so the point where a
    read-only open that cannot work fails.

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
    # A read-only connection to a WAL database creates the -wal and -shm files
    # beside it, and being read-only it cannot remove them when it closes.
    # Left by another user -- root, for a sudo saneless jobs -- they belong to
    # that user, and the server that owns the folder can then no longer write
    # its own database.  So a reader who does not own the file, finding no
    # side files, reads it immutable and creates none.  No side files means no
    # connection holds the database and no committed page lives outside the
    # file.  A server that opens it during the read writes to a new -wal, not
    # to this file, until it checkpoints; a checkpoint landing mid-read can at
    # worst spoil this one read, and never the file, which an immutable
    # connection does not write.
    if owner != os.geteuid() and not _has_side_files(db_path):
        return _open_uri(uri + "&immutable=1")
    try:
        return _open_uri(uri)
    except sqlite3.OperationalError:
        if not _immutable_read_is_safe(db_path):
            raise
    # A read-only connection to a WAL database still creates the -shm file
    # beside it, so in a directory nobody can write to it fails.  There, and
    # with no -wal or -shm present, immutable=1 is safe: no writer can be
    # active in a directory where none could create its -wal, and no -wal
    # means no committed page lives outside the database file, so nothing can
    # change under the read and nothing is missed by skipping the WAL.
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
    leaves a file the server cannot write in the server's folder.  Upgrading a database belongs
    to the server that writes it, so one at an older schema is refused rather
    than migrated.  A missing file, or one with no jobs table yet, is an
    empty history; a file that cannot be looked up for any other reason is
    an error, never an empty history.

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
    # answers False for a lookup that failed -- a folder this user may not
    # enter, a symlink loop -- and a listing that took that for "no jobs"
    # would print an empty table, and [] on the JSON contract.
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
                "start saneless serve once to upgrade it"
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
            # Release the BEGIN DEFERRED the failed ladder or index still
            # holds, and the file handle with it, before the caller sees the
            # failure.  A rollback that fails too -- a disk I/O error on the
            # same broken file -- must not replace the open's own error with a raw
            # sqlite3 one, which would exit 5 instead of 2.
            with contextlib.suppress(sqlite3.Error):
                self._conn.rollback()
            self._conn.close()
            if isinstance(exc, sqlite3.Error):
                raise _open_failure(db_path, exc) from exc
            raise

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        """
        Convert one jobs row into a Job through the module's one row mapper.

        Private and undecorated deliberately: ``sqlite3`` connection context
        managers do not nest, so an inner ``with conn:`` commits the outer
        transaction.  Shared logic therefore has to live in a helper a public
        method can call without going through another public method.

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
                    # is the writer of these, and of pages_removed_at, once the
                    # run ends.
                    None,  # outcome
                    None,  # pages_scanned
                    None,  # pages_removed
                    None,  # pages_uploaded
                    None,  # warning
                    # owner_token's first and only writer.  The value is an
                    # opaque session token minted by the web layer and kept for
                    # exactly one purpose: rendering the flip prompt to the
                    # browser that submitted this job rather than to every
                    # browser watching it.  NULL means the row is unowned and
                    # the prompt is rendered for everyone -- which is every row
                    # written before the column had a writer, including a
                    # manual-duplex job still in flight across an upgrade, so
                    # no migration backfills it and none is needed.  It is a
                    # footgun guard, not an authentication mechanism: the
                    # column already existed unused, and guessing a token
                    # grants nothing a LAN neighbour cannot already do.
                    owner_token,
                    None,  # pages_removed_at -- finish_job's to write
                ),
            )
            # Read the row back inside the same transaction, before the commit:
            # the caller then gets the job the database holds rather than a
            # second, hand-built copy of it, which is what keeps the row
            # mapping in exactly one place.
            row = self._conn.execute(_SELECT_BY_ID, (job_id,)).fetchone()
        job = self._row_to_job(row)
        # %r, not %s: the title is untrusted text, and a newline in it would
        # otherwise start what reads as a second log line.
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

        The refused attempt still gets a row, so history shows the user that
        their scan was not started and why.  The row is written already
        terminal -- ``ERROR`` with ``ErrorCategory.REJECTED`` -- and that marker
        is what :meth:`latest_run_job` skips, so the rejection never replaces
        the job that just ended in the status area.

        One ``INSERT``, not :meth:`create_job` followed by :meth:`finish_job`.
        Those are two transactions: if the second one raised, the first would
        already have committed a ``PENDING`` row with no marker.  No worker ever
        saw that id and restart recovery never runs again, so the row would stay
        active for good, showing "Starting scan..." and disabling the Scan
        button.  With a single statement a failure leaves no row at all.

        The same transaction then trims refused rows to the newest
        ``REJECTED_HISTORY_ROWS``, so a flood of refused submits holds the
        table at a fixed size instead of growing it.  That trim deletes only
        refused rows, so it cannot strand a ``PENDING`` one either.  The row
        stays unowned: ``owner_token`` is NULL.

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
                    None,  # pages_removed_at
                ),
            )
            # Read back inside the same transaction, as create_job does, so the
            # row mapping stays in _row_to_job alone.  Before the trim, so the
            # read can never miss the row it just wrote.
            row = self._conn.execute(_SELECT_BY_ID, (job_id,)).fetchone()
            self._trim_rejected()
        job = self._row_to_job(row)
        # %r for the same reason as create_job's: the title is untrusted text.
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

        The only writer of the six result columns, and the reason
        :meth:`update_state` is not.  ``update_state``'s SQL is an
        unconditional ``SET state, error, error_category``; naming the result
        columns there would blank them on every in-flight SCANNING /
        ASSEMBLING / UPLOADING transition, so the terminal write is a separate
        method rather than an extra argument.

        State, outcome, warning, the three counts, the removed-page positions
        and the error all land in one ``UPDATE``.  Recording ``ErrorCategory.REJECTED`` also trims the refused
        rows to their cap, in the same transaction.  The web request thread reads this row while the worker
        thread writes it, and a two-statement version would let it observe a
        job that had finished but had not yet recorded how.

        Omitting ``result`` leaves ``outcome``, ``warning``, all three page
        counts and the positions NULL rather than zero.  NULL means "never recorded"; ``0`` means
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
                # A job refused after its row existed -- the queue was full, or
                # the worker replays a rejection it owed -- joins the refused
                # rows, so it is trimmed under their cap in this transaction
                # just as create_rejected_job's rows are.
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

        Private, unlocked and without a ``with self._conn:`` of its own for the
        reason :meth:`_pending_jobs` gives: its callers, ``create_rejected_job``
        and ``finish_job``, already hold the lock and a transaction, and the
        trim must commit with the write that recorded the refusal.  It deletes
        only rows marked ``ErrorCategory.REJECTED``, so a flood of refused
        submits can never remove a run.
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

        The shared body of :meth:`list_pending` and :meth:`queue_position`.
        Private and unlocked deliberately: both public callers already carry
        ``@_locked``, and a public method may not call another public one --
        ``sqlite3`` connection context managers do not nest, so an inner ``with
        self._conn:`` commits the outer method's transaction early.  Factoring
        the query here, rather than letting ``queue_position`` write a second
        ``ORDER BY``, is what makes it impossible for a job's position and the
        list it is a position into to disagree.

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
    def list_pending(self) -> list[Job]:
        """
        Fetch the queued jobs oldest-first.

        Ascending ``created_at``, the opposite of ``list_recent``'s newest-first
        history ordering: this is a queue, and the oldest entry is the next one
        to be worked.  ``PENDING`` is the only state a job that has not started
        can be in, so the predicate is a single bound scalar rather than the
        variable-length ``IN`` ``fail_active_jobs`` needs -- but the state value
        is still bound rather than written into the statement text.

        No production caller wants this list *as* a list.  What production
        wants from the ordering is one job's place in it, which
        :meth:`queue_position` reports for the status area's "N ahead of you"
        line.  Both methods read it through ``_pending_jobs``, so
        this method is the ordering's public shape and its tests are the
        ordering's proof.

        Returns:
            List of Job instances awaiting a scanner, ordered by creation time
            ascending.

        """
        return self._pending_jobs()

    @_locked
    def queue_position(self, job_id: str, *, running: str | None = None) -> int | None:
        """
        Count the queued jobs ahead of one job.

        Zero-based: the job at the head of the queue has nothing ahead of it
        and answers ``0``.  The UI adds nothing to the number -- it renders
        ``(next in line)`` for ``0`` rather than ``(0 ahead of you)``, which is
        technically true and reads like a bug -- and ``(N ahead of you)`` for
        anything higher.

        ``None`` means the job is not waiting.  A job that has left ``PENDING``
        and an id no row carries both answer ``None``, because both mean the
        same thing to the status area: there is no queue line to render.

        Walks :meth:`list_pending`'s ordering through the shared
        ``_pending_jobs`` query rather than asking the database for a rank.  A
        second ``ORDER BY`` -- or a ``COUNT(*)`` over a hand-written predicate
        -- would be a second definition of "ahead", and two definitions that
        agree today drift tomorrow.  The queue is bounded by the submission
        cap, so reading it is cheaper than keeping the two in step by hand.

        The job the worker has taken is not in the queue, although its row
        can still read ``PENDING``: the worker persists ``SCANNING`` only once
        it holds the scanner gate, and a running check can hold that gate for
        up to the listing deadline.  The caller names that job in
        ``running``, and it is never counted ahead of anyone; asked about
        itself, it answers ``None``.

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

        This is what the status area falls back to once no job is active, to
        report the job that just ended.  ``list_recent(1)`` is the wrong
        answer there: a submit refused while a job runs -- queue full, worker
        down or degraded -- still writes a row marked
        ``ErrorCategory.REJECTED``, and that row is newer than the running job.
        Reading the newest row would let the rejection replace the job in the
        status area the moment the job ends.  History keeps using
        ``list_recent``, so the rejected row is still listed there.

        Excluded ids are filtered in Python, never interpolated into the SQL.
        Reading ``len(exclude_ids) + 1`` rows is enough: at most that many
        newer rows can be skipped, so the row after them is the answer.

        Args:
            exclude_ids: Ids to treat as never run.  The web layer passes the
                refused submits whose REJECTED write is still owed to the
                worker, so their PENDING rows do not stand in for the job that
                just ended.  A set rather than any collection: a bare id
                string is itself a collection of strings, and would silently
                exclude its single characters instead.

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
        holds -- appends a WAL frame and so exercises the write path.
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
    def fail_active_jobs(self) -> int:
        """
        Fail every job still in flight, for recovery after an unclean restart.

        A job left mid-scan by a killed process has no worker behind it any
        more, so it would otherwise sit in an active state for ever and the UI
        would poll it for ever.  Every row whose state is in ``ACTIVE_STATES``
        moves to ``JobState.ERROR``, worded by the state it was left in:
        ``restart_error`` and ``restart_category`` choose the text and the
        category.  The UPLOADING rows are written first, by ``_FAIL_UPLOADING``,
        and every other active row then gets ``RESTART_REASON``, all in one
        transaction.

        The ``_FAIL_ACTIVE`` predicate is derived from ``ACTIVE_STATES`` rather than listed by
        hand, which buys two things: ``ACTIVE_STATES`` and ``TERMINAL_STATES``
        partition ``JobState``, so it provably cannot reach a completed job's
        recorded history; and a state added to that frozenset later is covered
        here with no edit to this method.

        A job failed here is recorded as ``JobState.ERROR``.  There is no
        ``JobState.FAILED``, and none is added: the value is persisted as
        SQLite TEXT and read back through the enum constructor, so adding or
        renaming a member is a data migration.

        ``error_category`` is written only for the UPLOADING rows.  A row with
        no category shows its text itself in the status area rather than a
        category's generic sentence, which is right for a job that had not
        started its upload: an interrupted restart is a precisely known
        failure, and ``UNKNOWN`` would be the wrong value.  An UPLOADING row
        may already be in paperless-ngx, so it needs the amber category and its
        advice to check the document list before scanning again.

        Its caller is the web lifespan at startup, before the worker thread
        begins; when that call raises, the worker's recovery makes the same
        call once the store accepts writes again.  The returned count is what
        lets the lifespan log how many jobs it failed without a second query.

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

        Startup's workspace recovery calls this before ``fail_active_jobs``:
        a job whose workspace a killed process left behind has had its pages
        kept in ``failed/``, and its row should say where rather than carry
        the bare restart text.  Only a row whose id is in ``kept`` *and*
        whose state is in ``ACTIVE_STATES`` moves, to ``JobState.ERROR``, so a
        finished job's recorded history is never rewritten and an id with no
        row is ignored.  Each row's state is read inside the one transaction
        every row is written in, and its text and category are composed from
        that state by ``restart_error`` and ``restart_category``: the restart
        text for the state, then the kept sentence.

        ``error_category`` is written only for an UPLOADING row, for the
        reason ``fail_active_jobs`` gives: a row with no category shows its
        text, and an UPLOADING row needs the amber category's advice.

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
        outside the newest ``REJECTED_HISTORY_ROWS``, and reports how many rows
        it removed.  Refused submits do not count toward ``max_rows``, so they
        can never push a real scan out of history.  The predicates are a union
        rather than a sequence; ``_PRUNE`` carries the argument for why that is
        the same set the old delete-by-age-then-trim composition produced, and
        the UTC assumption every half rests on.

        The count comes from the statement itself.  The two ``SELECT COUNT(*)``
        reads this used to subtract straddled the deletes, so an insert landing
        between them made the answer wrong -- a scratch test drove it to ``-1``.
        A single statement leaves no gap for an insert to land in, which is a
        stronger guarantee than serialising the method behind the store's lock:
        it holds against writers on other connections too.

        When rows went, an incremental vacuum in the same transaction returns
        their pages to the file system, so the file shrinks back.  That only
        happens on a database with ``auto_vacuum = INCREMENTAL``; on an older
        one the pragma is a no-op until
        :meth:`enable_incremental_auto_vacuum` has converted it.

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
                # The fetchall() is load-bearing.  incremental_vacuum frees one
                # page per step of the statement, and execute() alone takes
                # only the first step, so without it a prune of thousands of
                # pages would hand back exactly one.
                self._conn.execute("PRAGMA incremental_vacuum").fetchall()

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
    def enable_incremental_auto_vacuum(self) -> bool:
        """
        Convert an older job database to ``auto_vacuum = INCREMENTAL``, once.

        A database this release creates already has it, set before its first
        table.  One an earlier release created has ``NONE``, so the pages its
        deletes free stay inside the file for good; switching an existing file
        over takes a full ``VACUUM``, which rewrites it.  After that, the
        incremental vacuums :meth:`prune` and the refused-row trim run can
        shrink it.

        ``VACUUM`` cannot run inside a transaction, and this connection always
        has one open, so the method commits it and runs both statements with
        ``autocommit`` on, restoring explicit transaction control afterwards
        whatever happens.  ``VACUUM`` also needs free disk space of roughly the
        database's size for its copy -- and a database bloated by a flood may
        sit on a nearly full disk -- so callers on the server path catch
        ``sqlite3.Error``, log it and carry on with the database as it is.

        This is not a migration step and bumps no schema version:
        ``auto_vacuum`` is a header setting that older releases open without
        complaint, so a version bump would only make them refuse the file.
        No ``with self._conn:`` here, for the ``VACUUM`` reason above.

        Returns:
            True if the database was converted, False if it already was
            INCREMENTAL and nothing was done.

        """
        mode: int = self._conn.execute("PRAGMA auto_vacuum").fetchone()[0]
        if mode == _AUTO_VACUUM_INCREMENTAL:
            return False
        # Announced before it starts: the rewrite runs before the server
        # binds, and on an SD card it can outlast a container health check's
        # start period, so the log must say what the pause is.
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
        # No `with self._conn:` here, deliberately.  Connection.__exit__ runs
        # after the body and must commit or roll back a connection the body has
        # already closed, which raises "Cannot operate on a closed database".
        self._conn.close()
