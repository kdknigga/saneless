---
phase: 22-job-store-hardening
verified: 2026-09-10T00:00:00Z
status: passed
score: 5/5 must-haves verified
overrides_applied: 0
---

# Phase 22: Job Store Hardening Verification Report

**Phase Goal:** The job store is safe under concurrency and can evolve its schema honestly — an `RLock` around every public method, a `PRAGMA user_version` migration ladder that opens a v1.0 database cleanly, and every result column this milestone will ever need added in one migration — with the storage docs updated in-phase.

**Verified:** 2026-09-10
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths (ROADMAP § Phase 22 Success Criteria)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Two threads calling any mix of `JobStore` methods for 200 rounds complete with zero exceptions and no interleaved-transaction corruption | ✓ VERIFIED | `tests/test_job.py::TestLockDiscipline::test_stress_two_threads_survive_two_hundred_rounds` (`ThreadPoolExecutor` + `Future.result()`, 2×200 rounds) passed 3/3 independent re-runs in this session (~0.13s each). Every public method carries `@_locked` (`src/saneless/job.py:415-446`, applied to all 7 public methods). I independently removed `@_locked` from `close()` and reran `test_locked_coverage_spans_every_public_method` — it failed exactly as designed, then restored the file and reconfirmed 39/39 green. |
| 2 | A database file written by v1.0 opens, migrates through the ladder, and reports the current `user_version`; the bare `ALTER TABLE ... except: pass` is gone | ✓ VERIFIED | `_migrate_v1`/`_migrate_v2`/`_MIGRATIONS`/`_migrate` (`src/saneless/job.py:266-359`) implement a `PRAGMA user_version` ladder. `TestMigrationLadder` (5 tests) covers fresh-store, legacy-S3-migrates, S2-guard-rejects, idempotent-reopen, and WAL-on-file-db. `grep -n "except.*:.*pass\|except:" src/saneless/job.py` → no matches; the module has no bare `except: pass` left. |
| 3 | The job table carries `outcome`, `pages_scanned`, `pages_removed`, `pages_uploaded`, `warning`, and `owner_token` after a single migration step, even though most stay unused until later phases | ✓ VERIFIED (with a documentation-wording caveat, see below) | `_V2_COLUMNS` (`src/saneless/job.py:249-256`) adds all six columns in `_migrate_v2`, a single migration step. `TestResultColumns` proves round-trip type-fidelity for all six and that they default to `None`. **Caveat:** ROADMAP's "most stay unused" is inaccurate — all six are written by nothing but `create_job` (which writes `None` for every one) and read back `None` everywhere; verified `grep -n "outcome\|pages_scanned\|pages_removed\|pages_uploaded\|warning\|owner_token" src/saneless/job.py` shows only the `Job` dataclass fields, `_row_to_job`, and the `create_job` `None` writes — no other writer exists anywhere in `src/`. This is a ROADMAP.md wording defect, not an implementation gap: the code and docs (`docs/explanation/architecture.md:65`) are honest that *all six* are unused, and STOR-03 itself (`REQUIREMENTS.md`) doesn't claim otherwise. |
| 4 | `fail_active_jobs()` marks every non-terminal job FAILED with a "server restarted" reason, and `list_pending()` returns queued jobs in creation order | ✓ VERIFIED (with two documentation-wording caveats, see below) | `fail_active_jobs()` (`src/saneless/job.py:686-734`) moves every state in `ACTIVE_STATES` to `JobState.ERROR` (default reason `"Interrupted by restart"`), derived from the `ACTIVE_STATES`/`TERMINAL_STATES` partition rather than hand-listed. `list_pending()` (`src/saneless/job.py:659-683`) returns `PENDING` jobs ordered `created_at ASC`. `TestQueryMethods` (9 tests) covers moving every active state, leaving terminal states/history untouched, a custom-reason override, `error_category` left unset, `list_pending` ordering under shuffled insertion, empty-store, and state-filtering. **Caveats:** (a) there is no `JobState.FAILED` — `test_job_state_has_no_failed_member` pins this deliberately (D-19, confirmed in `22-CONTEXT.md:128`); "FAILED" in the ROADMAP/REQUIREMENTS prose means the pre-existing `JobState.ERROR`, a documented reading, not an implementation gap. (b) the implemented default reason is `"Interrupted by restart"`, not `"server restarted"` — but the parameter is honored (`test_fail_active_jobs_honours_a_custom_reason` passes `CUSTOM_REASON = "server restarted"` and asserts it round-trips), so a caller can produce the literal ROADMAP string; the default itself is the more precise M-03-sourced text. Both are ROADMAP.md/REQUIREMENTS.md wording defects, correctly identified as such by the wave-6 executor, not implementation shortfalls. |
| 5 | The row-to-`Job` mapping and its column list appear exactly once, and `prune()` reports its count from one statement | ✓ VERIFIED | `_row_to_job` (`src/saneless/job.py:479-521`) is the sole row→`Job` conversion (`test_single_mapping_defines_exactly_one_row_to_job`, `test_single_mapping_constructs_a_job_in_one_place` via `ast` walk). `_COLUMNS` (`src/saneless/job.py:49-66`) is the sole column-list literal; `_COLUMN_LIST`/`_PLACEHOLDERS` are derived from it and every `SELECT`/`INSERT` derives from those. `prune()` (`src/saneless/job.py:736-769`) executes exactly one `DELETE` and reports `cursor.rowcount`. I independently reverted `prune()` to a two-`SELECT COUNT(*)`-plus-`DELETE` shape and reran `test_prune_concurrent_window_between_count_and_delete_is_closed` — it failed with "prune() ran 3 row-touching statements", then restored the file and reconfirmed 39/39 green. |

**Score:** 5/5 truths verified (2 with wording caveats attributable to ROADMAP.md/REQUIREMENTS.md prose, not code)

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/saneless/job.py` | `RLock`, migration ladder, `_COLUMNS`/`_row_to_job` single-source, `fail_active_jobs`/`list_pending`, single-statement `prune()` | ✓ VERIFIED | All present, substantive, and exercised by tests (see truths 1-5 above) |
| `src/saneless/exceptions.py` | `StorageError` | ✓ VERIFIED | `class StorageError(SanelessError)` present (`src/saneless/exceptions.py:38`), raised by `_migrate_v2` on schema mismatch, tested by `test_migration_guard_rejects_an_s2_database` |
| `tests/test_job.py` | Full coverage of all five criteria plus structural guards | ✓ VERIFIED | 39 tests in this file, all passing; includes the three structural guards (reflective lock coverage, `ast` self-call walk, single-statement `prune()`), all three confirmed independently to fail on injected regressions |
| `docs/explanation/architecture.md` § Job Storage | Documents RLock, migration ladder, unused columns | ✓ VERIFIED | Section exists (`docs/explanation/architecture.md:53-65`), accurately describes the lock, the ladder, and that all six new columns are unwritten |
| `docs/reference/configuration.md` | Retention/prune description corrected | ✓ VERIFIED (spot-checked) | `history_retention_days` row present and consistent with `prune()`'s behavior |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `JobStore.__init__` | `_migrate` | direct call | ✓ WIRED | `src/saneless/job.py:471` calls `_migrate(self._conn, db_path)` inside `__init__`, wrapped in try/except that rolls back and closes on failure |
| Every public `JobStore` method | `_locked` decorator | `@_locked` | ✓ WIRED | Verified via reflective test and independent regression injection (removed decorator from `close`, guard fired) |
| `_S3_COLUMNS` | migration guard only | `_migrate_v2` set-difference check | ✓ WIRED, NOT used for SQL construction | `grep -n "_S3_COLUMNS" src/saneless/job.py` shows only the definition (line 227) and the guard's set-difference at line 307 — never interpolated into any SQL string. `_COLUMNS` (not `_S3_COLUMNS`) is the sole source for `_COLUMN_LIST`/`_PLACEHOLDERS`, which build every `SELECT`/`INSERT`. The reconciliation test `test_single_mapping_ladder_reconciles_with_the_column_list` performs a real executed `frozenset`/`set` equality assertion (`_S3_COLUMNS | {V2 names} == set(_COLUMNS)`), not a static/lint check — confirmed by reading the test body directly. |
| `fail_active_jobs`/`list_pending` | production callers | grep across `worker.py`, `web/`, `cli.py` | NOT WIRED (expected — out of scope this phase) | Zero production call sites, exactly as the phase declares (`fail_active_jobs` startup call is Phase 26; `list_pending` UI consumer is Phase 30/APPL-08) |

### Data-Flow Trace (Level 4)

Not applicable in the UI-rendering sense — this phase is a persistence-layer hardening phase with no rendering surface. The relevant "data flow" check is the round-trip fidelity of the new columns, covered under Truth 3/5 above (`test_outcome_roundtrip_preserves_value_and_python_type`, `test_outcome_roundtrip_survives_list_recent`) — both are real executed assertions with type checks, not static.

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Full `test_job.py` suite passes | `uv run pytest tests/test_job.py -q` | 39 passed | ✓ PASS |
| Lock-coverage guard actually fails on regression | Removed `@_locked` from `close()`, reran `-k locked_coverage` | Failed: "public JobStore methods missing the @_locked marker: close" | ✓ PASS |
| No-public-self-call guard actually fails on regression | Injected `self.get_job(...)` into `close()`, reran `-k no_public_method_calls` | Failed: "close -> get_job" | ✓ PASS |
| Prune single-statement guard actually fails on regression | Reverted `prune()` to two-`COUNT(*)`-plus-`DELETE` shape, reran `-k prune_concurrent_window` | Failed: "prune() ran 3 row-touching statements" | ✓ PASS |
| Stress test is not flaky | 3 independent re-runs of `-k stress` | 3/3 passed, ~0.13s each | ✓ PASS |
| No bare `except: pass` survives | `grep -n "except.*:.*pass\|except:" src/saneless/job.py` | No matches | ✓ PASS |
| Ruff/ty clean | `uv run ruff check .`, `uv run ty check` | Both clean | ✓ PASS |
| File restored to pre-verification state after each injection | `git status --porcelain` after each restore | Clean | ✓ PASS |

### Probe Execution

No `scripts/*/tests/probe-*.sh` conventions apply to this phase (pure Python library hardening, no CLI/migration tooling probes declared in PLAN/SUMMARY). Skipped.

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|--------------|------------|-------------|--------|----------|
| STOR-01 | 22-03 | Two threads can call any mix of `JobStore` methods concurrently for 200 rounds without exception; `RLock` around every public method; public methods never call other public methods | ✓ SATISFIED | `@_locked` on all 7 public methods; reflective + `ast` guards confirmed to fail on regression; stress test passes reproducibly |
| STOR-02 | 22-01 | `PRAGMA user_version` migration ladder; v1.0 database opens/migrates cleanly; bare `ALTER TABLE ... except: pass` gone | ✓ SATISFIED | `_migrate`/`_MIGRATIONS` ladder; no bare `except` anywhere in `job.py`; `TestMigrationLadder` covers fresh/legacy/guard/idempotent/WAL cases |
| STOR-03 | 22-02 | Job table carries `outcome`, `pages_scanned`, `pages_removed`, `pages_uploaded`, `warning`, `owner_token`, added in one migration | ✓ SATISFIED | `_V2_COLUMNS` added in `_migrate_v2` (single step); `TestResultColumns` round-trip tests |
| STOR-04 | 22-02, 22-04 | Row-to-`Job` mapping and column list exist once; `prune()` reports count from a single statement | ✓ SATISFIED | `_row_to_job`/`_COLUMNS` singularity proven by `ast`-walk tests; `prune()` single-DELETE + `cursor.rowcount`, guard confirmed to fail on regression |
| STOR-05 | 22-05 | `JobStore` exposes `fail_active_jobs()` and `list_pending()` | ✓ SATISFIED | Both methods implemented and tested (9 tests in `TestQueryMethods`); no `JobState.FAILED` added (D-19, deliberately) |

No orphaned requirements found for this phase (`REQUIREMENTS.md` maps STOR-01..05 only, all claimed by plans 22-01/02/03/04/05).

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| — | — | none | — | `grep -n -E "TBD|FIXME|XXX|TODO|HACK|PLACEHOLDER"` across `job.py`, `exceptions.py`, `test_job.py` returns zero matches (the only `PLACEHOLDER`-substring hits are the legitimate `_PLACEHOLDERS` identifier). No `noqa`/`type: ignore` in `job.py`. Suppressions tree-wide: 7, all pre-dating the phase (confirmed independently via the same grep pattern used by wave summaries). |

### Human Verification Required

None. This phase touches no browser surface, no external service, and no hardware. All five success criteria are reachable and were reached via automated tests plus independent regression-injection checks performed in this verification.

### Deviation-Report Honesty (specifically requested check)

- Confirmed no commit message across the phase's 10 commits (`dde8b88`..`13cc9a5`) references `no-verify`.
- Every wave's self-reported deviation ("bare symbol in RED commit to satisfy the project-wide `ty` hook", "`SKIP=pyrefly-checker` because the worktree's bare `pyrefly check` checks nothing under `.gitignore`") is internally consistent with `.pre-commit-config.yaml`'s `ty-checker`/`pyrefly-checker` hooks (`always_run: true` confirmed by direct read).
- Wave 3's claim of independently proving the `ast` no-public-self-call guard fires (by temporarily inserting a violation and reverting) was re-performed independently in this verification session with the same result, confirming the wave's report was accurate and not merely narrated.
- Wave 6's three explicit "ROADMAP overstates/misnames" claims (JobState.FAILED vs ERROR; "server restarted" vs "Interrupted by restart"; "most" vs "all six" columns unused) were independently checked against the code and are all accurate characterizations of documentation wording, not code gaps.

### Scope Discipline (specifically requested check)

- `fail_active_jobs()`: zero production call sites (`grep -rn "fail_active_jobs" --include="*.py" .` outside `job.py`/`test_job.py` returns nothing) — not called at startup, correctly deferred to Phase 26.
- `list_pending()`: zero production call sites, correctly deferred to Phase 30.
- No write path for any of the six new columns exists outside `create_job`'s `None` placeholders — Phases 23/30 territory untouched.
- `tmp_dir / "saneless.db"` path still computed only in `cli.py:227` (single occurrence found), not deduplicated or moved — D-18 correctly left to Phase 23.
- `close()` body is `@_locked` and a bare `self._conn.close()` — no `_closed` flag, no new exception type, no hardening beyond the lock — D-15/T-22-13 correctly left to Phase 26.
- `JobState` has exactly 7 members (`PENDING`, `SCANNING`, `AWAITING_FLIP`, `ASSEMBLING`, `UPLOADING`, `DONE`, `ERROR`); no `FAILED` member added — D-19 respected.

### Gaps Summary

No gaps found. All five ROADMAP success criteria are genuinely implemented and defended by tests that were independently confirmed (not merely trusted) to detect their own regressions. The three ROADMAP-wording concerns raised by the wave-6 executor are real but are documentation defects in `ROADMAP.md`/`REQUIREMENTS.md` prose (the "FAILED" state name, the "server restarted" default reason string, and "most" vs. "all six" unused columns) — the underlying implementation decisions (D-19's `JobState.ERROR` rather than a new `FAILED` member, the `"Interrupted by restart"` default with an honored override parameter, and honest "all six unused" documentation in `architecture.md`) are all correct and deliberate. These do not block the phase goal and do not affect the dependency spine Phases 23-30 build on, since those phases consume the actual code (`JobState.ERROR`, the six nullable columns, `_COLUMNS`) rather than the ROADMAP prose. No override entries were needed since none of the five truths failed outright — the wording mismatches are sub-clauses within otherwise-satisfied criteria, not independent must-haves.

---

*Verified: 2026-09-10*
*Verifier: Claude (gsd-verifier)*
