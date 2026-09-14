---
phase: 26-worker-and-web-robustness
plan: 01
subsystem: vocabulary, job-store
tags: [tdd, vocabulary, sqlite, contracts, robustness]
requires: []
provides:
  - ErrorCategory.REJECTED
  - WorkerHealth + worker_health_detail
  - SubmitResult
  - RequestRejection + rejection_message + rejection_status_code
  - TITLE_MAX_LENGTH, QUEUE_FULL_JOB_ERROR, WORKER_DOWN_JOB_ERROR, WORKER_DEGRADED_JOB_ERROR, RESTART_REASON
  - JobStore.latest_run_job()
  - JobStore.probe()
affects: [26-04..26-13 (worker, routes, lifespan, templates import these names)]
tech-stack:
  added: []
  patterns: [total match + assert_never label functions, NULL-safe IS NOT bound predicate, same-value PRAGMA write as a write-path probe]
key-files:
  created: []
  modified:
    - src/saneless/vocabulary.py
    - src/saneless/job.py
    - tests/test_vocabulary.py
    - tests/test_job.py
decisions:
  - "D-06 resolved with ErrorCategory.REJECTED + `error_category IS NOT ?` (NULL-safe, so uncategorised and restart-failed rows stay visible); no schema migration"
  - "rejection_message stays an 11-arm match with single assignment per arm; ruff raised no PLR0911/PLR0912, so the Mapping fallback was not needed"
  - "rejection_status_code groups same-status members with `|` patterns (503 pair, 422 triple)"
metrics:
  duration: 25min
  completed: 2026-09-14
  tasks: 2
  files: 4
---

# Phase 26 Plan 01: Vocabulary contracts and store methods Summary

The phase's interface contracts: every S3 message, status code and job-row text lives once in `vocabulary.py` behind total `match` functions, and `JobStore` gains `latest_run_job()` (newest non-REJECTED job, NULL-safe and durable) and `probe()` (count plus a same-value `user_version` write in one transaction).

## Tasks

| Task | Name | RED commit | GREEN commit |
| ---- | ---- | ---------- | ------------ |
| 1 | Vocabulary contracts and the S3 copy | 9c5e752 | 6f862a2 |
| 2 | JobStore.latest_run_job() and JobStore.probe() | a04632b | 36ce2ad |

## What was built

- `ErrorCategory.REJECTED` and its `error_message` arm. The docstring says why `latest_run_job` skips it.
- `WorkerHealth` (HEALTHY/DEGRADED/DOWN) with `worker_health_detail` ("ok" / "job store failing" / "worker thread is down").
- `SubmitResult` (ACCEPTED/QUEUE_FULL/DOWN/DEGRADED).
- `RequestRejection` (11 members) with `rejection_message`, which returns the S3 copy word for word. The TITLE_TOO_LONG message is built from `TITLE_MAX_LENGTH`. `rejection_status_code` returns 429/503/503/422/422/422/403/404/405/500/400.
- `TITLE_MAX_LENGTH: Final = 256`, three rejected job-row texts and `RESTART_REASON`. The row texts have no trailing period, matching `job.error`.
- `_SELECT_LATEST_RUN`, `_COUNT_JOBS`, and the `@_locked` methods `latest_run_job()` and `probe()`. Both pass `TestLockDiscipline`.

## Verification

- `uv run pytest -m "not browser and not sane_hardware" -q`: 1172 passed
- `uv run ruff check .`, `ruff format --check .`, `ty check`, `pyrefly check src tests`: clean. pyrefly shows 4 warnings that were already there, in pipeline.py, sane_backend.py and fake_sane.py.
- `uv run prek run --stage pre-push --all-files`: passed
- `-k "latest_run or probe or LockDiscipline"` selects 11 tests; all pass.
- A throwaway check against a file-backed store showed `probe()` grows the `-wal` file (24752 -> 28872 bytes), so it really does write. With `PRAGMA query_only=ON` it raises `OperationalError: attempt to write a readonly database`, so a store that cannot write keeps the worker degraded (D-12).

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Docstring accuracy] ConnectionStatus docstring said the module "knows no status codes"**
- **Found during:** Task 1
- **Issue:** The new `rejection_status_code` made that sentence false.
- **Fix:** It now says the module imports no HTTP client and never maps a response status to a connection outcome.
- **Files modified:** src/saneless/vocabulary.py
- **Commit:** 6f862a2

**2. [Test naming] `RESTART_REASON` name collision in tests/test_job.py**
- `tests/test_job.py` already has a local `RESTART_REASON = "Interrupted by restart"`, the default for `fail_active_jobs`. The vocabulary constant is imported as `SERVER_RESTART_REASON` instead, so the existing constant is untouched.

**3. [Test determinism] Rows for `latest_run_job` are ordered with raw-SQL `created_at` stamps**
- A new `_create_in_order` helper stamps `created_at` the way `_seed_queue` does. This avoids sleeps and avoids equal timestamps. The restart-failed test makes the interrupted job the newest row, so a NULL-unsafe `!=` predicate would fail the test.

## TDD Gate Compliance

Both tasks have a `test(26-01)` RED commit before the `feat(26-01)` GREEN commit: 9c5e752 before 6f862a2, and a04632b before 36ce2ad. RED failed as expected: an ImportError for Task 1 and an AttributeError for Task 2. No refactor commits were needed.

## Known Stubs

None. `rejection_message`, `worker_health_detail`, `latest_run_job` and `probe` have no production caller yet. That is intended: plans 26-04 through 26-13 wire them in.

## Threat Flags

None. T-26-01 through T-26-04 are mitigated as planned: every message is a constant pinned by literal tests, the category is bound, the PRAGMA value is an int read back from the same database, and a test asserts `list_recent` still shows the REJECTED row first.

## Self-Check: PASSED

- FOUND: src/saneless/vocabulary.py, src/saneless/job.py, tests/test_vocabulary.py, tests/test_job.py
- FOUND commits: 9c5e752, 6f862a2, a04632b, 36ce2ad
