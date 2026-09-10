---
phase: 22-job-store-hardening
plan: 02
subsystem: database
tags: [sqlite, sqlite3-row, dataclass, strenum, tdd, ruff-s608]

# Dependency graph
requires:
  - phase: 22-job-store-hardening (plan 22-01)
    provides: "the PRAGMA user_version migration ladder, _S3_COLUMNS, _V2_COLUMNS, and the sixteen-column head schema those six ALTERs produce"
  - phase: 21-vocabulary
    provides: "ScanOutcome, and ScanResult's field names, which five of the six new Job fields mirror"
provides:
  - "_COLUMNS: the sixteen live column names, spelled exactly once"
  - "_COLUMN_LIST and _PLACEHOLDERS, derived from _COLUMNS by join"
  - "_SELECT_JOBS / _INSERT_JOBS / _UPDATE_JOBS / _DELETE_JOBS, the S608 name-prefix constants"
  - "_SELECT_ALL, _SELECT_BY_ID, _SELECT_RECENT, _INSERT, all built once at import"
  - "six new Job fields: outcome, pages_scanned, pages_removed, pages_uploaded, warning, owner_token"
  - "JobStore._row_to_job, private and undecorated, the only row-to-Job conversion in the module"
  - "a reconciliation test binding _S3_COLUMNS + _V2_COLUMNS to _COLUMNS by executed object equality"
affects: [22-03 lock discipline, 22-04 prune, 22-05 fail_active_jobs and list_pending, 23-honest-outcomes, 30-appliance-polish]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "derived module constants (join over _COLUMNS) so two lists cannot drift"
    - "SQL verb held under a name so ruff S608 stays clean without a suppression"
    - "single private undecorated row mapper with name-based sqlite3.Row access"
    - "write-then-read-back-in-the-same-transaction so create_job returns the persisted row"

key-files:
  created: []
  modified:
    - src/saneless/job.py
    - tests/test_job.py

key-decisions:
  - "create_job no longer builds a Job by hand: it inserts, reads the row back inside the same transaction, and converts it through _row_to_job, so JobStore constructs a Job in exactly one place"
  - "the RED commit carries the bare _COLUMNS tuple and the six Job fields, because the project-wide ty pre-commit gate refuses to commit a test naming a symbol that does not exist (Wave 1 precedent for StorageError)"
  - "source assertions read job.py through inspect.getsource rather than pathlib.Path(module.__file__).read_text(), because module.__file__ is typed str | None and both checkers reject it without a suppression"
  - "the ladder/column-list reconciliation is an executed object-equality assertion against the imported module objects, not a source-text check"

patterns-established:
  - "Pattern: every SELECT and INSERT in job.py derives its column list and placeholders from _COLUMNS; adding a column means editing one tuple and one migration step"
  - "Pattern: statement constants carry a docstring stating why the interpolation is safe, rather than leaving a reader to infer it from the naming"
  - "Pattern: a round-trip test asserts the Python type as well as the value, because sqlite3.Row.__getitem__ is typed Any and neither checker can see a wrong-typed column"

requirements-completed: [STOR-03, STOR-04]

# Metrics
duration: 34min
completed: 2026-09-10
---

# Phase 22 Plan 02: Single Column List and Single Row Mapping Summary

**`job.py` now derives every `SELECT` and `INSERT` from one `_COLUMNS` tuple and converts every row through one `_row_to_job`, with six new nullable result columns that round-trip as `ScanOutcome`, `int` and `str`.**

## Performance

- **Duration:** ~34 min
- **Tasks:** 2 (RED, GREEN)
- **Files modified:** 2
- **Completed:** 2026-09-10

## Accomplishments

- **The column list exists once.** `_COLUMNS` (sixteen names, schema order) drives `_COLUMN_LIST` and `_PLACEHOLDERS` by join, and those drive `_SELECT_ALL`, `_SELECT_BY_ID`, `_SELECT_RECENT` and `_INSERT`, all built once at import. The three hand-written column lists that existed at `job.py:165-168`, `:241-244` and inside `create_job` are gone.
- **The row mapping exists once.** `JobStore._row_to_job` — private, undecorated, name-based `sqlite3.Row` access throughout — is now the only place a database row becomes a `Job`, and the only place `Job(` is called inside `JobStore` at all.
- **Six result columns are readable.** `outcome` (`ScanOutcome | None`), `pages_scanned` / `pages_removed` / `pages_uploaded` (`int | None`), `warning` and `owner_token` (`str | None`), every one defaulting to `None`. `outcome` gets the same truthiness-guarded round trip `error_category` already had. Nothing writes any of them; that is Phase 23's and Phase 30's work.
- **The ladder and the live column list are bound together by test.** `_S3_COLUMNS | {name for name, _ in _V2_COLUMNS} == set(_COLUMNS)` is an executed equality against the imported module objects, so `_S3_COLUMNS` cannot quietly grow and `_COLUMNS` cannot quietly diverge from the migration that produces it.
- **`S608` resolved with zero suppressions.** The verbs live under names (`_SELECT_JOBS`, `_INSERT_JOBS`, `_UPDATE_JOBS`, `_DELETE_JOBS`), each documenting why its interpolation is safe. `ruff check .` reports nothing; the tree's suppression count is still 7, all pre-dating this phase.

## Task Commits

1. **Task 1 (RED): column round-trip and single-mapping tests** — `f9420b9` (test)
2. **Task 2 (GREEN): `_COLUMNS`, six `Job` fields, and one `_row_to_job`** — `75b1e4e` (feat)

No REFACTOR commit: the GREEN implementation needed no cleanup pass, and `ruff format --check` was clean on first write.

## TDD Gate Compliance

- **RED gate:** `f9420b9`, `test(22-02)`. All ten new tests failed on a tree with no implementation (`AttributeError: 'Job' object has no attribute 'outcome'`, `Module saneless.job has no member _COLUMNS`).
- **GREEN gate:** `75b1e4e`, `feat(22-02)`, after RED. All ten pass; suite went 511 → 521.
- **Ordering verified:** `e331b7d` → `f9420b9` (test) → `75b1e4e` (feat).

**The behavioural RED/GREEN split is intact, with one documented compromise.** The project's `.pre-commit-config.yaml` runs `uv run ty check` project-wide with `always_run: true`, and `ty` rejected the RED test file with 32 diagnostics (`unresolved-attribute` on `Job.outcome` and friends, and on `saneless.job._COLUMNS`). Per the Wave 1 precedent set for `StorageError`, the RED commit introduces the **bare symbols only** — the six `Job` field declarations with their `Attributes:` docstring lines, and the `_COLUMNS` tuple with its docstring. No behaviour: no derived constants, no statement constants, no `_row_to_job`, and the three methods still carried their hand-written SQL.

Measured effect on RED: **5 of 10 tests still failed** after the bare symbols landed — the three `outcome_roundtrip` tests (the behavioural core) and three of the `single_mapping` structural tests. The four that flipped to green are the ones whose entire content *is* the bare symbol (`Job` defaults are `None`; `_COLUMNS` matches the schema; `_COLUMNS` reconciles with the ladder). One further test, `test_outcome_roundtrip_of_a_null_column_is_none`, passes trivially in RED because everything is `None` before the mapping exists; it earns its keep in GREEN, where it is the guard against `ScanOutcome("")`.

## Files Created/Modified

- `src/saneless/job.py` — `_COLUMNS` plus its derived and statement constants; `ScanOutcome` added to the `saneless.vocabulary` import; six new `Job` fields and their `Attributes:` lines; new `JobStore._row_to_job`; `create_job`, `get_job` and `list_recent` rewritten onto the constants.
- `tests/test_job.py` — new `class TestResultColumns` (10 tests), the `_job_source()` and `_count_job_constructions()` helpers, and imports for `ast`, `inspect`, `saneless.job` as a module object and `ScanOutcome`.

## Decisions Made

- **`create_job` reads its row back rather than hand-building a `Job`.** The plan's acceptance criterion is explicit that `Job(` may appear as a constructor call in exactly one place inside `JobStore`, inside `_row_to_job` — while the action prose only names the two ten-field positional constructions in `get_job` and `list_recent` for deletion. `create_job`'s keyword construction was the third. Resolved in favour of the testable criterion: `create_job` now builds the value tuple in `_COLUMNS` order, executes `_INSERT`, reads the row back via `_SELECT_BY_ID` **inside the same transaction**, commits, and converts through `_row_to_job`. Three consequences worth recording:
  - The returned `Job` is provably the persisted row, not a parallel copy that could drift from it.
  - The read happens *before* the commit deliberately. Executing the `SELECT` after the commit would leave a dangling read transaction open under `autocommit = False`; doing it inside the insert's transaction leaves the connection with no open transaction, exactly as before.
  - The cost is one extra primary-key lookup per job creation — one per scan, on an indexed column. `INSERT ... RETURNING` would have avoided it, but it needs SQLite 3.35+ and D-30 records this machine at libsqlite 3.34.1.
- **`inspect.getsource(job_module)` instead of `pathlib.Path(saneless.job.__file__).read_text()`.** The plan suggested the `Path` form. `ModuleType.__file__` is typed `str | None` in typeshed, so `Path(...)` on it fails both checkers, and CLAUDE.md forbids a suppression to paper over it. `inspect.getsource` returns `str`, needs no `pathlib` import at runtime scope, and reads the same source text.
- **`_UPDATE_JOBS` and `_DELETE_JOBS` are declared and unconsumed**, as the plan's `<interfaces>` block specifies. Their docstrings say which plan consumes each, so they do not read as dead constants at review time.
- **`list_recent` got a round-trip test of its own** (`test_outcome_roundtrip_survives_list_recent`). The plan's behaviour block only described the `get_job` path; asserting the list path returns the same typed values is what actually demonstrates that *one* mapping serves both, rather than that one of the two was updated.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] `ty` pre-commit gate blocks a symbol-free RED commit**

- **Found during:** Task 1 (RED)
- **Issue:** `uv run ty check` runs project-wide with `always_run: true`, so the RED test file could not be committed at all: 32 `unresolved-attribute` diagnostics for `Job.outcome`, `Job.pages_scanned`, …, and `saneless.job._COLUMNS`.
- **Fix:** Introduced the bare symbols (six `Job` fields, `_COLUMNS` tuple) in the RED commit, with no behaviour attached — the resolution the executor prompt names as accepted, and the one Wave 1 used for `StorageError`.
- **Files modified:** `src/saneless/job.py`
- **Verification:** `uv run ty check` and `uv run pyrefly check src tests` clean at the RED commit; 5 of the 10 tests, including all three behavioural `outcome_roundtrip` tests, still RED.
- **Committed in:** `f9420b9`

**2. [Rule 3 - Blocking] `Path(module.__file__)` is not type-clean**

- **Found during:** Task 1 (RED)
- **Issue:** The plan's suggested source-assertion idiom, `pathlib.Path(saneless.job.__file__).read_text()`, cannot pass `ty` or `pyrefly`: `__file__` is `str | None`. CLAUDE.md forbids the suppression that would silence it.
- **Fix:** Used `inspect.getsource(job_module)` behind a `_job_source()` helper.
- **Files modified:** `tests/test_job.py`
- **Verification:** All four static gates clean; the source assertions behave identically (they failed in RED, pass in GREEN).
- **Committed in:** `f9420b9`

---

**Total deviations:** 2 auto-fixed (both Rule 3 — blocking).
**Impact on plan:** Neither changes what shipped. Deviation 1 is a documented, precedented compromise on *where* two declarations land in the two-commit split; deviation 2 is a mechanical substitution of an equivalent, type-clean source read. No scope creep, nothing added beyond the plan's `<interfaces>` block.

## Issues Encountered

- **The plan contained one internal tension**, between `<behavior>`'s "one `Job(` inside `JobStore`" and `<action>`'s instruction to delete only the two positional constructions. Resolved in favour of the mechanically-verifiable acceptance criterion; rationale recorded under Decisions Made above.
- **`pyrefly` cannot run bare inside this worktree** (Claude Code worktrees live under `.claude/worktrees/`, which `.gitignore:314` excludes, so pyrefly checks nothing and its exit code is meaningless). Verified with explicit paths instead: `uv run pyrefly check src tests` → `0 errors`. Committed with `SKIP=pyrefly-checker`; every other hook, including project-wide `ty`, ran and passed. The orchestrator's authoritative bare `pyrefly check` on the main checkout covers this.

## Verification

| Gate | Result |
|---|---|
| `uv run pytest -m "not browser" -q` | **521 passed** (baseline entering the wave: 511) |
| `uv run pytest tests/test_job.py -k "columns or outcome_roundtrip or single_mapping" -q` | 10 passed |
| `uv run ruff check .` | No issues found — in particular zero `S608` |
| `uv run ruff format --check .` | 40 files already formatted |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |
| suppression count in `src/` and `tests/` | 7, unchanged, all pre-dating this phase |
| `grep -v '^ *#' src/saneless/job.py \| grep -c 'SELECT id, profile'` | 0 |
| `grep -c 'def _row_to_job' src/saneless/job.py` | 1 |

## Threat Model Coverage

- **T-22-06 (Tampering / SQL injection) — mitigated as specified.** The only interpolated values in `_SELECT_ALL`, `_SELECT_BY_ID`, `_SELECT_RECENT` and `_INSERT` are `_COLUMN_LIST` and `_PLACEHOLDERS`, both derived at import from the `_COLUMNS` tuple literal. Every runtime value is a bound `?`. The statements are built once at import, so nothing is reassembled from data at call time. Each verb constant carries a docstring stating this. The `S608` resolution is a naming form, not a suppression: ruff reports zero findings and there is no `# noqa` in the file.
- **T-22-07 (Information Disclosure / `owner_token`) — accepted as specified.** `owner_token` lands unwritten and unread. Nothing in this change logs it, renders it, or treats it as a credential; its only appearances are the column name, the dataclass field, a `None` in the insert tuple, and a passthrough in `_row_to_job`.
- **T-22-12 (Tampering / enum reconstruction) — mitigated as specified.** `ScanOutcome(row["outcome"]) if row["outcome"] else None` raises on a stored value the enum does not know, identically to `JobState` and `ErrorCategory`. Because `sqlite3.Row.__getitem__` is typed `Any`, `test_outcome_roundtrip_preserves_value_and_python_type` asserts the Python type alongside the value for all six columns — 13 `isinstance` assertions in the file. That test is the whole control.
- **T-22-SC (package installs) — accepted.** No packages installed; `pyproject.toml` and `uv.lock` are untouched.

## Known Stubs

`outcome`, `pages_scanned`, `pages_removed`, `pages_uploaded`, `warning` and `owner_token` read back as `None` for every job, because `create_job` writes `None` into all six and no other code path writes them. This is **intentional and locked by D-07**: the columns and their fields ship in this phase so that the row mapping is complete once; the writers belong to Phase 23 (`outcome`, `warning`) and Phase 30 (the page counts, `owner_token`). `test_created_job_result_columns_stay_none` pins the current state so a future writer cannot land silently.

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

Ready for the rest of Wave 2 and beyond:

- **22-03 (lock discipline)** gets `_row_to_job` already private and undecorated, which is the shape D-24 requires before `@_locked` goes on the public methods. Note for that plan: `create_job` calls `_row_to_job` and executes a `SELECT` between its `INSERT` and its `commit()`; that whole body needs to sit inside one `with self._conn:` block.
- **22-04 (`prune`)** gets `_DELETE_JOBS` already declared.
- **22-05 (`fail_active_jobs` / `list_pending`)** gets `_UPDATE_JOBS` and `_SELECT_ALL` already declared; `_SELECT_ALL` composes for a `WHERE state = ?` variant the same way `_SELECT_BY_ID` does.
- **Phase 23 and Phase 30** can write the six columns without touching the row mapping: an `UPDATE` and a value are all that is needed.

No blockers. One thing a reviewer should look at rather than take on trust: `create_job`'s extra `SELECT`, and whether the "one `Job(` construction" criterion is worth that lookup. The alternative — leaving `create_job` to build its own `Job` — was rejected because it puts the sixteen-field construction back in two places, which is the drift STOR-04 exists to prevent.

---
*Phase: 22-job-store-hardening*
*Completed: 2026-09-10*

## Self-Check: PASSED

Both modified files exist on disk and both recorded commit hashes resolve in this worktree (`f9420b9e72a0fa`, `75b1e4e337a477`).
