---
phase: 26-worker-and-web-robustness
plan: 09
subsystem: web
tags: [tdd, lifespan, crash-recovery, shutdown, sqlite, robustness]
requires:
  - 26-01 (RESTART_REASON, WorkerHealth, JobStore.fail_active_jobs)
  - 26-04 (ScanWorker.stop() -> bool, STOP_JOIN_SECONDS, current_job_id)
  - 26-06 (ScanWorker.mark_recovery_pending(), health, idle recovery probe)
  - 26-07 (create_app with install_error_handlers and CrossOriginGuard, left untouched)
provides:
  - lifespan startup order validate -> fail_active_jobs(RESTART_REASON) -> guarded prune -> worker.start()
  - startup recovery failure starts the worker degraded with recovery pending
  - guarded shutdown close gated on worker.stop()
  - tests/test_app_lifespan.py (11 tests)
affects:
  - 26-10 (/health must report the DEGRADED health a failed startup recovery leaves behind)
tech-stack:
  added: []
  patterns:
    - "Lifespan startup writes guarded with try/except: recovery failure degrades, prune failure only warns"
    - "Resource close gated on a confirmed worker stop; early return leaves handles for process exit"
key-files:
  created:
    - tests/test_app_lifespan.py
  modified:
    - src/saneless/web/app.py
    - src/saneless/job.py
decisions:
  - "Crash recovery failure is logged with logger.exception (an ERROR record with exc_info), because ruff G201 rejects logger.error(..., exc_info=True)"
  - "The stuck-worker test spies on finish_job/update_state without forwarding, since any write after stop() is itself the failure; the real idle thread is stopped and the store closed in the test's finally"
  - "The job.py module docstring no longer promises crash recovery from persistence alone; it names fail_active_jobs as the startup recovery the web app runs before the worker starts"
metrics:
  duration: 25min
  completed: 2026-09-14
  tasks: 2
  files: 3
---

# Phase 26 Plan 09: Startup crash recovery and guarded shutdown Summary

At startup, the web lifespan now marks every job a crashed process left active (PENDING, SCANNING, AWAITING_FLIP and the other active states) as ERROR with "The server restarted before this scan finished". This runs before the worker thread starts. At shutdown, the job store and Paperless client are closed only after the worker confirms it has stopped.

## What changed

**Startup (`src/saneless/web/app.py` `lifespan`):** the order is `validate_settings_dirs`, then `job_store.fail_active_jobs(RESTART_REASON)`, then `job_store.prune(...)`, then `worker.start()`.
- If recovery changed any rows, a WARNING gives the count ("Marked 3 interrupted job(s) as failed at startup").
- If recovery raises, the app still starts. It logs an ERROR with the traceback and calls `worker.mark_recovery_pending()`, so the worker starts `DEGRADED`. Its first successful idle probe then runs the recovery (26-06).
- If prune raises, the app logs a WARNING with the traceback and starts anyway.

**Shutdown:** `if not worker.stop():` the lifespan logs a WARNING and returns without closing anything or writing any job state. The WARNING names the running job id and the 5 s join limit. Otherwise it closes Paperless, then the store, and logs "App shutdown complete".

**`src/saneless/job.py` (docstrings only, doc row 33):** the module docstring now says how crash recovery actually works. `fail_active_jobs` no longer claims it has no production caller: its callers are the web lifespan at startup and, after a failed startup recovery, the worker's recovery. The `list_pending` "NO production caller" paragraph is unchanged.

## Tests (`tests/test_app_lifespan.py`, 11 tests)

- A seeded crashed store (DONE, PENDING, AWAITING_FLIP, and SCANNING created last): the three active rows become ERROR with `RESTART_REASON` and the DONE row is unchanged.
- T9: `GET /` renders `&#10007; Error: The server restarted before this scan finished`, and the `#status-area` tag has no `hx-get` or `hx-trigger`.
- Ordering: a wrapped `worker.start` sees the SCANNING row already ERROR. The recorded call order is `fail_active_jobs`, `prune`, `start`.
- The recovery-count WARNING is logged when rows are recovered and not logged on a clean store.
- A prune that raises `sqlite3.OperationalError` still lets `/health` answer 200 and logs a WARNING with exc_info.
- A `fail_active_jobs` that raises leaves `worker.health is WorkerHealth.DEGRADED` and logs an ERROR with exc_info.
- A normal shutdown runs stop, then Paperless close, then store close, then the INFO record.
- When `stop()` returns False, nothing is closed, the store still reads, the WARNING contains `job-xyz` and `5`, and no store write happens.
- A real idle worker's shutdown closes the store: `get_job` then raises `sqlite3.ProgrammingError`.

## Verification

- `uv run pytest tests/test_app_lifespan.py tests/test_job.py -q`: 70 passed
- `uv run pytest -m "not browser and not sane_hardware" -q`: 1329 passed
- `uv run ruff check src tests`, `uv run ruff format --check .`, `uv run ty check`, `uv run pyrefly check src tests`: all clean
- Acceptance greps: `fail_active_jobs(RESTART_REASON)` (line 94) comes before `job_store.prune(` (108), which comes before `worker.start()` (114). `worker.mark_recovery_pending()` appears once. `if not worker.stop():` (122) comes before `paperless.close()` (134) and `job_store.close()` (135). "NO production caller in this phase" now appears only in `list_pending`.

## TDD Gate Compliance

- Task 1: `564a7e9` test(26-09) (RED: 7 of 8 failed; the negative no-warning case passed as expected), then `939ff31` feat(26-09) (GREEN)
- Task 2: `6578647` test(26-09) (RED: the stuck-worker test failed; the normal-close and idle-close tests passed against the old code, which already closed in that order), then `756b6b3` feat(26-09) (GREEN)

## Deviations from Plan

**1. [Rule 3 - Blocking] `logger.exception` instead of `logger.error(..., exc_info=True)`**
- **Found during:** Task 1 GREEN
- **Issue:** ruff G201 rejects `.error(..., exc_info=True)`, and the project forbids suppressing lint rules.
- **Fix:** Used `logger.exception(...)`, which logs the same ERROR record with the traceback. The test assertion is unchanged.
- **Commit:** 939ff31

**2. [Rule 3 - Blocking] Worktree base reset**
- At start, the worktree HEAD (`ed2d620`) did not descend from the expected base `7d13df2`. As the branch check instructs, it was reset to `7d13df2` before any work began.

## Known Stubs

None.

## Self-Check: PASSED

- FOUND: tests/test_app_lifespan.py, src/saneless/web/app.py, src/saneless/job.py
- FOUND commits: 564a7e9, 939ff31, 6578647, 756b6b3
