---
phase: 26-worker-and-web-robustness
plan: 17
subsystem: worker, web
tags: [gap-closure, WR-01, robustness, concurrency, tdd]
requires:
  - 26-15 (ScanWorker._flush_unrecorded_failures on every idle tick)
  - 26-16 (routes._reject_created_job, the post-row rejection write)
provides:
  - "ScanWorker.owe_rejection(job_id, error): an owed ERROR/REJECTED write drained by the idle flush"
  - "_unrecorded_lock guarding every access to _unrecorded_failures"
  - "_reject_created_job falls back to worker.owe_rejection when its write raises"
affects:
  - src/saneless/worker.py
  - src/saneless/web/routes.py
  - docs/explanation/architecture.md
tech-stack:
  added: []
  patterns:
    - "Snapshot shared state under a lock, do store I/O outside it, delete only if the entry is unchanged"
key-files:
  created: []
  modified:
    - src/saneless/worker.py
    - src/saneless/web/routes.py
    - tests/test_worker.py
    - tests/test_web_errors.py
    - docs/explanation/architecture.md
decisions:
  - "WR-01 post-row half: a refused submit's REJECTED write that fails in the request is owed to the worker via owe_rejection, not dropped"
  - "_unrecorded_failures is shared by request and worker threads under _unrecorded_lock; the flush never holds the lock across a store write"
requirements: [ROBU-01, ROBU-02]
metrics:
  duration: ~20 min
  completed: 2026-09-14
  tasks: 2
  files: 5
---

# Phase 26 Plan 17: Rejected-submit writes owed to the worker (WR-01) Summary

When `submit()` refuses a job after its row was created (queue full, down or degraded) and the `finish_job(ERROR, REJECTED)` write then raises, the route now hands that write to the worker with `worker.owe_rejection(job.id, error)`. The worker writes it on its next idle tick, so the row reaches ERROR/REJECTED and the Scan button is enabled again without a restart. Together with 26-16, this closes WR-01.

## What changed

- **`ScanWorker.owe_rejection(job_id, error)`** (src/saneless/worker.py): under `_unrecorded_lock` it sets `_unrecorded_failures[job_id] = (error, ErrorCategory.REJECTED)`. Its docstring covers four points: the route calls it when the write fails (WR-01); the id was never enqueued, so no job can be running under it; the write lands on the next idle tick, or through the recovery path while degraded (CR-01, D-12); and when the worker is DOWN, `/health` reports 503 and the next startup's `fail_active_jobs(RESTART_REASON)` ends the row (D-13).
- **`_unrecorded_lock`**: a new lock. It is taken in `owe_rejection`, in `_best_effort_fail`'s assignment, around the flush snapshot, and around each delete in the flush (4 sites). `_flush_unrecorded_failures` runs `finish_job` outside the lock, and drops an entry only if it still equals the snapshot's tuple. An entry owed after the snapshot waits for the next tick.
- `ErrorCategory` is now a runtime import from `.vocabulary`, no longer TYPE_CHECKING-only.
- **`_reject_created_job(worker, job_store, job_id, *, error)`** (src/saneless/web/routes.py): if the write raises, it logs a WARNING ("...; the worker will record it", with exc_info), calls `worker.owe_rejection(job_id, error)` and returns False. With False, the error response does not reload Job History. `start_scan` passes `state.worker`. `_record_refused_submit` is unchanged.
- **docs/explanation/architecture.md** "Failure handling.": the idle-retry sentence now also covers a refused scan's rejection that the web request could not record.

## Tasks

| Task | Name | Commit | Files |
| ---- | ---- | ------ | ----- |
| 1 | RED: route and worker tests for rejections owed to the worker | f91910b | tests/test_web_errors.py, tests/test_worker.py |
| 2 | GREEN: owe the failed rejection write to the worker under a lock | 40403f0 | src/saneless/worker.py, src/saneless/web/routes.py, docs/explanation/architecture.md |

No REFACTOR commit was needed.

## Tests

- `test_a_refused_submit_whose_rejection_write_fails_is_recorded_by_the_worker[queue_full|down|degraded]` (tests/test_web_errors.py) uses the new `fast_tick_client` fixture, which patches `_IDLE_TICK_SECONDS` to 0.02 before the lifespan starts. `finish_job` raises on its first call only. The test checks:
  - the expected 429 or 503, with no history loader in the response
  - the row reaches ERROR within 2 s, `_assert_rejected_row` holds, and `latest_run_job() is None`
  - the status poll shows "Ready to scan." and a `#scan-btn` without `disabled`
- `TestOwedRejections` (tests/test_worker.py):
  - idle-tick write on a healthy worker, with no probe calls
  - a failing retry (at least 3 `finish_job` calls) leaves the row PENDING, `_consecutive_loop_failures == 0` and the worker HEALTHY; after `heal()` the row reaches ERROR/REJECTED
  - 100 rejections owed from 4 threads started on a Barrier are all written, and `_unrecorded_failures` ends empty

## TDD Gate Compliance

- RED `f91910b test(26-17): ...` touched no file under src/. Against the 26-15/26-16 source, the `-k` selection ran 6 tests and all 6 failed:
  - the 3 route cases failed with `AssertionError: row still PENDING after 2.0s`, which is the real WR-01 behaviour
  - the 3 worker tests failed with `AttributeError: 'ScanWorker' object has no attribute 'owe_rejection'`, the expected contract RED for a new API. The many-thread test collects exceptions from its threads and re-raises them after joining, so it too fails with the AttributeError rather than timing out.
  - there were no import, fixture or name errors, and the other 169 tests in both files passed
- GREEN `40403f0 feat(26-17): ...` comes after RED, and all 6 tests pass.
- Flakiness: `pytest tests/test_worker.py -k TestOwedRejections` passed 5 of 5 runs (about 0.17 s each). The acceptance selection including `below_the_degraded_threshold or degraded` passed 23 tests.

## Verification

- `uv run pytest tests/test_worker.py tests/test_web_errors.py tests/test_web.py tests/test_web_state_rendering.py -q`: 312 passed
- `uv run pytest -m "not browser and not sane_hardware" -q`: 1400 passed
- `uv run ruff check .`, `uv run ruff format --check .` and `uv run ty check` are clean. `uv run pyrefly check src tests` reports 0 errors. `uv run prek run --all-files` passes every hook.
- Grep criteria:
  - `def owe_rejection(self, job_id: str, error: str) -> None` appears once
  - `with self._unrecorded_lock` appears 4 times
  - `worker.owe_rejection(job_id, error)` appears once, in `_reject_created_job`
  - there is no TYPE_CHECKING `ErrorCategory` import
  - "rejection" appears in the Failure handling paragraph

## Deviations from Plan

**1. [Rule 3 - Blocking] The `finish_job` wrapper is a module-level factory, not a closure written inside the test**
- **Found during:** Task 1
- **Issue:** In 26-15, `ty` narrowed a bound store method captured by a local closure to the method's real signature, and rejected forwarding `*args: object`. Suppressions are forbidden.
- **Fix:** `_fail_first_call(original: Callable[..., object]) -> Callable[..., object]` returns a closure with a list call counter. Taking the method as a parameter widens its type. The behaviour matches the plan: the first call raises `sqlite3.OperationalError("disk I/O error")` and later calls delegate.
- **Files modified:** tests/test_web_errors.py
- **Commit:** f91910b

**2. [Rule 1 - Test robustness] The many-thread test also waits for the owed dict to empty**
- **Found during:** Task 1
- **Issue:** The flush deletes an entry just after its write lands, so reading `_unrecorded_failures` the moment the last row is ERROR could race that delete.
- **Fix:** The wait predicate requires all rows ERROR and `not worker._unrecorded_failures`.
- **Commit:** f91910b

## Known Stubs

None.

## Threat Flags

None. There are no new endpoints, auth paths or schema changes. T-26-68 (stranded row), T-26-69 (dict race) and T-26-70 (never-enqueued ids only) are mitigated and tested. T-26-71 and T-26-72 are accepted as planned.

## Self-Check: PASSED

- FOUND: src/saneless/worker.py (`def owe_rejection`, `_unrecorded_lock`)
- FOUND: src/saneless/web/routes.py (`worker.owe_rejection(job_id, error)`)
- FOUND: tests/test_worker.py (`class TestOwedRejections`)
- FOUND: tests/test_web_errors.py (`def fast_tick_client`, `def test_a_refused_submit_whose_rejection_write_fails_is_recorded_by_the_worker`)
- FOUND commits: f91910b (test), 40403f0 (feat)
