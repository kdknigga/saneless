---
phase: 26-worker-and-web-robustness
plan: 06
subsystem: worker
tags: [tdd, worker, robustness, health, sqlite, threading]
requires:
  - 26-01 (WorkerHealth, SubmitResult.DEGRADED, RESTART_REASON, classify_error, JobStore.probe, fail_active_jobs)
  - 26-04 (_stopping Event, _IDLE_TICK_SECONDS get loop, SubmitResult-returning submit)
provides:
  - ScanWorker.health -> WorkerHealth (HEALTHY | DEGRADED | DOWN)
  - ScanWorker.mark_recovery_pending() -> None
  - submit() returns SubmitResult.DEGRADED while degraded
  - guarded _run (loop never ends on an exception), _best_effort_fail, _idle_housekeeping, _try_recover
  - _DEGRADED_AFTER = 3 and _PRUNE_INTERVAL_SECONDS = 3600.0 module constants
affects:
  - 26-09 (lifespan calls mark_recovery_pending() when startup fail_active_jobs raises; owns the startup prune)
  - 26-10 (/health maps health to 200/503; routes map SubmitResult.DEGRADED)
tech-stack:
  added: []
  patterns:
    - "Job-vs-loop failure split: only run_pipeline sits in the job try; the loop's own store writes escape to a guard in _run"
    - "Degraded latch as a threading.Event written by the worker thread, read by request threads"
    - "Idle-tick housekeeping: store probe while degraded, rate-limited prune"
key-files:
  created: []
  modified:
    - src/saneless/worker.py
    - tests/test_worker.py
decisions:
  - "The success-path finish_job sits after the pipeline try (not inside it), so a failed terminal write on either outcome is a loop-level failure, as research Pattern 1 describes for D-10"
  - "_process_job became a try/finally around a new _scan_job, so a raise from the SCANNING write still clears _current_job_id and _flip_coordinator"
  - "Recovery writes the guard's unrecorded failures before running pending restart recovery, so those rows keep their own error text instead of passing through RESTART_REASON"
  - "A job failure is logged after its ERROR write; if that write raises, logger.exception in the guard carries the pipeline exception as the chained context"
  - "Tests use a worker_for factory fixture (scanner, Paperless and settings mocks) to stay within ruff's PLR0913 argument limit"
metrics:
  duration: 40min
  completed: 2026-09-14
  tasks: 2
  files: 2
---

# Phase 26 Plan 06: Guarded worker loop, degraded health and idle recovery Summary

An exception from a job store write or from prune can no longer end the worker thread. `_run` logs the exception with the job id and counts it as a loop-level failure. It then makes one best-effort ERROR write so the row does not stay active. After three such failures in a row the worker reports `DEGRADED` and rejects scans. Each idle tick probes the store, and the first successful probe ends the rows that could not be written and restores `HEALTHY`. Prune no longer runs after each job; it runs hourly on an idle tick.

## Tasks

| Task | Name | RED commit | GREEN commit |
| ---- | ---- | ---------- | ------------ |
| 1 | Guarded loop, job-vs-loop failure split, prune out of the job path | 2be0903 | 36e6729 |
| 2 | Degraded health, the idle probe, and recovery reconciliation | fb04a38 | 199a831 |

## What changed

**src/saneless/worker.py**
- `_run`: on `queue.Empty` it calls `_idle_housekeeping()`. It wraps `_process_job` in `try/except Exception`. That handler calls `logger.exception("Worker loop failed while handling job %s", job.id)`, then `_record_loop_failure()`, then `_best_effort_fail(job, exc)`. The `else` branch resets the consecutive counter but does not clear degraded.
- `_process_job` is now a `try/finally` around `_scan_job` and no longer calls `prune`. Only `run_pipeline` sits inside the job `try`. A pipeline exception is recorded as ERROR through `finish_job`, and a raise from that write escapes. The SCANNING write and the success `finish_job` are also outside the catch, so a failure in any of them is a loop-level failure (D-10).
- `_failure_record(exc)`: returns `RESTART_REASON`/`None` while stopping, otherwise `str(exc)`/`classify_error(exc)`. The job path and the guard both use it.
- `_best_effort_fail`: makes one `finish_job(job.id, ERROR, error=..., error_category=...)` call. If that raises, it logs a WARNING with `exc_info` and saves the failure in `_unrecorded_failures`.
- `_record_loop_failure`: increments the counter. At `_DEGRADED_AFTER` it sets `_degraded` and logs "Scan worker degraded after %d consecutive job store failures; rejecting scans until the store recovers".
- `_idle_housekeeping`: runs `_try_recover()` first if degraded, then prunes when `_PRUNE_INTERVAL_SECONDS` has elapsed. A prune failure is logged with `logger.exception("Idle history prune failed")` and counted.
- `_try_recover`: calls `probe()`, and a failure there is logged at DEBUG and not counted. On success it writes each unrecorded failure, then runs `fail_active_jobs(RESTART_REASON)` if a restart recovery is pending. It then resets the counter, clears degraded and logs "Scan worker recovered: the job store accepted a write". If any of those writes raises, the worker stays degraded and tries again on the next tick.
- `health` property (DOWN / DEGRADED / HEALTHY), `mark_recovery_pending()`, and a DEGRADED check in `submit()` between the DOWN check and `put_nowait`.

**tests/test_worker.py**
- Added a `_StoreFault` callable. It records every call, raises `sqlite3.OperationalError("disk I/O error")` on the chosen calls until `heal()` is called, and passes the rest through to the real method. Also added `_wait_until`, `_worker_records`, `_submit_jobs`, `_degrade`, and the `worker_for` fixture.
- `TestWorkerGuard` (8 tests): the worker survives `update_state` raising, the failure-path `finish_job` raising, and `prune` raising, and each of these is logged with `exc_info`. Pipeline failures leave the counter at 0 and health HEALTHY. The idle prune uses the configured limits and runs on its interval, and there is no prune inside the first 10 ticks at the default interval. The guard's best-effort write happens exactly once, with the right text and category. A failed best-effort write is only logged.
- `TestWorkerDegradedHealth` (7 tests): DOWN before start and after stop. DEGRADED after three failures, with the thread still alive, a WARNING logged, and `submit` returning DEGRADED. Failures must be consecutive: two failures, a success and two more leave it HEALTHY. While the probe fails the worker stays degraded, and the first success heals it (INFO logged, then a scan is ACCEPTED and reaches DONE). Recovery ends the rows the guard could not write, using the original text and category. `mark_recovery_pending` starts the worker degraded, and recovery then fails the SCANNING row with `RESTART_REASON`. No probe runs while healthy.
- Rewrote the two per-job prune tests. `test_worker_makes_no_prune_call_in_the_job_path` and `test_finish_clears_the_job_without_a_prune_after_a_fallback_outcome` now check that there are zero prune calls.

## Verification

- `uv run pytest tests/test_worker.py -q`: 87 passed in 4.8 s. The slowest test is 1.00 s (`TestLazyAutoGenerate`, which already existed). None come near 6 s.
- `uv run pytest tests/test_worker.py -q -k "TestWorkerGuard or prune"`: 10 passed. `-k degraded`: 7 passed.
- Flakiness check: the 17 new or rewritten tests passed 8 times in a row, and passed again with 6 copies running at once.
- `uv run pytest -m "not browser and not sane_hardware" -q`: 1286 passed.
- `uv run ruff check src tests`, `uv run ruff format --check .`, `uv run ty check`, `uv run pyrefly check src tests`: all clean (pyrefly's 4 warnings were already there).
- Acceptance greps:
  - `_job_store.prune(` appears once, inside `_idle_housekeeping`.
  - The `logger.exception("Worker loop failed while handling job %s"` line appears once.
  - `self._job_store.probe()` appears once.
  - The `health`/`mark_recovery_pending`/`SubmitResult.DEGRADED`/`_DEGRADED_AFTER` grep returns 7 lines.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] A SCANNING-write failure would have left `_current_job_id` set**
- **Found during:** Task 1 GREEN
- **Issue:** The old `finally` covered only the pipeline `try`. Once `update_state(SCANNING)` is allowed to escape, it would skip the cleanup and leave a stale current job and flip coordinator behind.
- **Fix:** Split the method in two. `_process_job` sets the current job and wraps a new `_scan_job` in `try/finally`, which clears both on every exit.
- **Files modified:** src/saneless/worker.py
- **Commit:** 36e6729

**2. [Rule 3 - Blocking] ruff PLR0913 on the guard tests**
- **Found during:** Task 1 RED
- **Issue:** Tests that need scanner, Paperless, settings, monkeypatch, caplog and wait_for_state take 6 fixture arguments, which is over ruff's limit of 5.
- **Fix:** Added a `worker_for` factory fixture that builds a `ScanWorker` for a given store. `pytest` is now imported at runtime in the test module so the fixture decorator is available.
- **Files modified:** tests/test_worker.py
- **Commit:** 2be0903

### Minor structural choices (within the plan's intent)
- The success `finish_job` runs after the pipeline `try` rather than inside it. The plan's action text puts it inside, but its D-10 rule and research Pattern 1 both treat a failed terminal write on either path as a loop-level failure. Inside the `try`, a failed success write would be recorded as a job failure and never counted.
- `_try_recover` writes the unrecorded failures before `fail_active_jobs(RESTART_REASON)`; the plan lists them the other way round. The rows end up identical either way. With this order, a reconciled row never holds the restart text even briefly.
- In the "failure-path finish_job raising" test, the guard's retry records the store exception's text, not the pipeline's. The plan's error-text rule produces this. The test checks only that the row is ERROR with an error, so it does not lock the text in.

## TDD Gate Compliance

- Task 1: `test(26-06)` 2be0903 comes before `feat(26-06)` 36e6729. At RED, all 10 selected tests failed: the thread died, the prune constant was missing, or prune still ran after each job.
- Task 2: `test(26-06)` fb04a38 comes before `feat(26-06)` 199a831. At RED, all 7 degraded tests and the extended pipeline-failure test failed because `health` did not exist yet.

## Known Stubs

None.

## Threat Flags

None. All the changes fall within threats T-26-21..T-26-25, and each is mitigated and tested. No new endpoints, file access or schema changes.

## Self-Check: PASSED

- FOUND: src/saneless/worker.py, tests/test_worker.py, .planning/phases/26-worker-and-web-robustness/26-06-SUMMARY.md
- FOUND commits: 2be0903, 36e6729, fb04a38, 199a831
