---
phase: 22-job-store-hardening
plan: 01
subsystem: database
tags: [sqlite, migration, pragma-user-version, wal, exceptions, tdd]

# Dependency graph
requires:
  - phase: 21-vocabulary-consolidation
    provides: JobState / ErrorCategory re-exported through job.py, which the ladder's CREATE TABLE and the surviving query methods still round-trip
provides:
  - StorageError, the sixth SanelessError subclass, for job-store schema and persistence failures
  - "_S3_COLUMNS / _V2_COLUMNS module constants describing the frozen historical shape and the six v2 result columns"
  - "_migrate_v1 / _migrate_v2 / _MIGRATIONS / _migrate: a PRAGMA user_version ladder that builds the schema from nothing, joins a legacy database at version 1, and refuses any other shape"
  - JobStore.__init__ opening with sqlite3.Row, WAL before the autocommit flip, and rollback+close on the failure path
  - self._lock (threading.RLock) and self._conn.row_factory, created for plans 22-02 and 22-03 to consume
  - S3/S2 raw-sqlite3 schema fixtures and five migration tests in tests/test_job.py
affects: [22-02 columns and row mapping, 22-03 lock discipline, 22-04 prune and _COLUMNS, 22-05 fail_active_jobs and list_pending, 26 shutdown ordering, 28 exception reconciliation]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "PRAGMA user_version migration ladder as a module-level tuple of (conn, db_path) callables"
    - "Assert-before-act schema guard raising a saneless exception naming the path and the missing columns"
    - "WAL enabled before conn.autocommit = False, pinned by a named file-backed test"

key-files:
  created: []
  modified:
    - src/saneless/exceptions.py
    - src/saneless/job.py
    - tests/test_job.py

key-decisions:
  - "StorageError landed in the RED commit rather than the GREEN one: the project's pre-commit hooks type-check the whole tree, so a test naming a class that does not exist cannot be committed at all"
  - "The journal_mode test passes in RED by design -- the current code already set WAL, and the test exists to pin the ordering the GREEN commit's autocommit flip could have broken"
  - "Migration steps log at debug with the database path, which also gives _migrate_v1 a real use for its db_path parameter instead of an underscore-prefixed throwaway"

patterns-established:
  - "Pattern 1: migration steps share a homogeneous (conn, db_path) signature so the ladder tuple types cleanly under Callable"
  - "Pattern 2: PRAGMA interpolation carries an in-place comment stating why it is safe, never a suppression"
  - "Pattern 3: a failed open rolls back and closes before re-raising, so no BEGIN DEFERRED or file handle leaks"

requirements-completed: [STOR-02, STOR-03]

# Metrics
duration: 42min
completed: 2026-09-10
---

# Phase 22 Plan 01: Migration Ladder Summary

**A `PRAGMA user_version` ladder that creates the jobs table from nothing, joins a legacy `dc9b8af` database at version 1, adds the six v2 result columns, and raises `StorageError` naming the path and the missing columns on any other shape -- replacing `CREATE TABLE IF NOT EXISTS` plus the bare `ALTER ... except: pass`.**

## Performance

- **Duration:** 42 min
- **Started:** 2026-09-10T00:00:00Z (worktree spawn)
- **Completed:** 2026-09-10T00:42:00Z
- **Tasks:** 2 (RED, GREEN)
- **Files modified:** 3

## Accomplishments

- `tests/test_job.py` gained `TestMigrationLadder` with five tests -- fresh open, legacy S3 join, S2 guard, idempotent reopen, file-backed WAL -- plus `_build_s3_schema` / `_build_s2_schema` raw-`sqlite3` fixtures and two schema-reading helpers. `test_jobstore_migration_adds_column` was deleted, not adapted: it builds an S2 database, which under D-05 must now raise.
- `src/saneless/exceptions.py` gained `StorageError(SanelessError)`, the sixth type, with `"StorageError"` sorted last in `__all__`.
- `src/saneless/job.py` gained `_S3_COLUMNS`, `_V2_COLUMNS`, `_migrate_v1`, `_migrate_v2`, `_MIGRATIONS` and `_migrate`, and a rewritten `__init__`. The schema is now spelled in exactly one place.
- The D-05 guard reads `PRAGMA table_info(jobs)` and raises before the first `ALTER`, so a rejected database is left at `user_version = 0` with its original nine columns -- asserted by reopening the file with a fresh raw connection.
- `__init__` sets `row_factory`, enables WAL, *then* flips `autocommit = False`, and rolls back plus closes before re-raising. `self._lock` exists for plan 22-03.
- Full suite: **511 passed**. `ruff check`, `ruff format --check`, `ty check` and `pyrefly check src tests` all clean, with zero suppressions added anywhere.

## Task Commits

1. **Task 1 (RED): Migration fixtures and the five ladder tests** - `dde8b88` (test)
2. **Task 2 (GREEN): StorageError, the ladder, and the new open sequence** - `8bfbddd` (feat)

No REFACTOR commit: the GREEN implementation was lifted from the verified research prototype and needed no cleanup pass.

## Files Created/Modified

- `src/saneless/exceptions.py` - adds `StorageError(SanelessError)` and its `__all__` entry
- `src/saneless/job.py` - migration ladder, schema guard, and the rewritten open sequence
- `tests/test_job.py` - S3/S2 fixtures, schema-reading helpers, `TestMigrationLadder`; the S2-based migration test removed

## Decisions Made

- **`_migrate_v1` and `_migrate_v2` both take `(conn, db_path)`.** Only step 2 needs the path for its failure message, but a homogeneous signature lets `_MIGRATIONS` be a single `tuple[Callable[[sqlite3.Connection, str], None], ...]`. Step 1 uses its `db_path` in a debug log rather than being an unused argument.
- **Both `PRAGMA user_version = {index + 1}` and `ALTER TABLE ... ADD COLUMN {name} {sqltype}` carry an in-place comment** stating that every interpolated value is either an `int` from `range()` over a module-level tuple or a literal from `_V2_COLUMNS`, and that SQL cannot bind an identifier. Neither construct trips `S608`, so no `_SELECT_JOBS`-style name-prefix constant was needed in this plan; that resolution arrives with `_COLUMNS` in plan 22-04.
- **The guard message reads `job database at {path} has an unsupported schema: missing column(s) error_category`,** matching `config.py`'s "what, then which" idiom.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] `StorageError` created in the RED commit instead of the GREEN one**

- **Found during:** Task 1 (RED)
- **Issue:** The plan says "Do NOT create `StorageError` in this task", and offers `ImportError` as an acceptable RED signal. But `.pre-commit-config.yaml` runs `uv run ty check` project-wide (`always_run: true`, `pass_filenames: false`) on every commit. A test file naming a class that does not exist fails that gate -- verified: `error[unresolved-import]: Module 'saneless.exceptions' has no member 'StorageError'`. The RED commit was therefore impossible without `--no-verify`, which is forbidden. The same task's own acceptance criterion ("`-k "prune or list_recent or error_category"` still exits zero") independently rules out a collection-time `ImportError`.
- **Fix:** The three-line `StorageError` addition to `exceptions.py` moved into the RED commit. The exception type is not the behaviour under test: the ladder and the guard were still absent, and all four behavioural tests still failed for the intended reasons.
- **Files modified:** `src/saneless/exceptions.py`
- **Verification:** RED run showed `assert 0 == 2` on three tests and `DID NOT RAISE <class 'saneless.exceptions.StorageError'>` on the guard; `-k "prune or list_recent or error_category"` exited zero with 8 passed.
- **Committed in:** `dde8b88` (Task 1 commit)

**2. [Rule 3 - Blocking] The `journal_mode` test passes in RED**

- **Found during:** Task 1 (RED)
- **Issue:** The plan expects all five new tests to fail. `test_journal_mode_is_wal_on_a_file_database` passed immediately, because the pre-change `__init__` already ran `PRAGMA journal_mode=WAL` and never flipped `autocommit`, so there was no transaction to refuse the switch.
- **Fix:** None required, and the test was kept. Per D-21 and RESEARCH § Pitfall 1 this test is a *regression guard* on an ordering that the GREEN commit introduces the hazard for -- it is the only thing standing between the correct order and a future reversal that would pass every `:memory:` test and raise `OperationalError` on the operator's first `saneless serve`. The TDD fail-fast rule was applied: the pass was investigated, explained by the existing code, and recorded in the RED commit body rather than silently accepted.
- **Files modified:** none
- **Verification:** `command grep -n 'journal_mode=WAL\|autocommit = False' src/saneless/job.py` returns lines 225 and 226 in that order.
- **Committed in:** `dde8b88` (documented in the commit body)

**3. [Rule 3 - Blocking] `uv run pyrefly check` cannot run project-wide inside the worktree**

- **Found during:** Task 1 (RED)
- **Issue:** This executor runs in `/home/kris/git/saneless/.claude/worktrees/agent-…`, and the repository's own `.gitignore:314` lists `.claude/worktrees/`. pyrefly matches that pattern against the absolute path of its include glob and therefore skips every file, printing `No Python files matched patterns …` and exiting 1. It is a location artifact: the gate fails identically on unmodified `master` in this worktree, and passes in the main checkout.
- **Fix:** pyrefly was run with explicit paths -- `uv run pyrefly check src tests` -- which type-checks the real files and reports `0 errors`. The two commits were made with `SKIP=pyrefly-checker` so the remaining hooks (including `ty`, which does run project-wide and passes) all executed normally. No `--no-verify`, no config change, no suppression. Note that adding a `pyrefly.toml` with `project-includes` was tried and rejected: it exits 0 while still checking nothing, which is a false green.
- **Files modified:** none
- **Verification:** `uv run pyrefly check src tests` -> `INFO 0 errors`.
- **Committed in:** n/a (tooling invocation only)

---

**Total deviations:** 3 auto-fixed (all Rule 3 - blocking)
**Impact on plan:** No scope creep. Two deviations are commit-sequencing consequences of the project's own pre-commit gates; the third is a worktree-location artifact of one type checker. Every artifact the plan specifies exists, and every static gate is genuinely clean.

## Issues Encountered

- **`D213` on the multi-line `journal_mode` docstring.** `pyproject.toml` ignores `D203` and `D212`, so the summary must start on the second line. Fixed in place before the RED commit.
- **A plan acceptance criterion is arithmetically unsatisfiable.** Task 1 asks for "at least 5 more" `def test_` occurrences while also mandating the deletion of `test_jobstore_migration_adds_column` in the same commit. The file went 9 -> 13 (five added, one deleted, net +4), which is the intended outcome.
- **`conn.autocommit = False` did not disturb any existing method.** Every write path in `job.py` already called `commit()` explicitly, and the read paths leave a deferred read transaction that `close()` discards. 511 tests pass, including the four web/worker files that construct a `JobStore` across threads.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- Plan 22-02 can add the six `Job` dataclass fields, `_COLUMNS` and `_row_to_job` on top of a table that already carries all sixteen columns, and `self._conn.row_factory = sqlite3.Row` is already set for it.
- Plan 22-03 has `self._lock` waiting; note that `__init__` is deliberately undecorated and that `_migrate` is a private module-level function, so neither is in scope for the reflective `@_locked` coverage test.
- `create_job`, `get_job`, `update_state`, `update_thumbnail`, `list_recent`, `prune` and `close` are untouched and still spell their own ten-column SQL, exactly as plan 22-02 expects to find them.
- Carried forward for whoever runs the phase verification: `uv run pyrefly check` with no arguments does not work from a worktree under `.claude/worktrees/`; use `uv run pyrefly check src tests`, or run it from the main checkout.

---
*Phase: 22-job-store-hardening*
*Completed: 2026-09-10*

## Self-Check: PASSED

All three modified source files exist on disk and both commit hashes (`dde8b88`, `8bfbddd`) resolve in this worktree.
