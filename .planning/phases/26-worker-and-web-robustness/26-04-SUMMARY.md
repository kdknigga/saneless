---
phase: 26-worker-and-web-robustness
plan: 04
subsystem: worker
tags: [tdd, worker, threading, queue, shutdown, backpressure, locking, robustness]
requires:
  - 26-01 (SubmitResult, RESTART_REASON)
provides:
  - ScanWorker.stop() -> bool (stop Event + Queue.shutdown(immediate=True) + bounded join)
  - ScanWorker.submit() -> SubmitResult (ACCEPTED | QUEUE_FULL | DOWN; never blocks)
  - STOP_JOIN_SECONDS (public, 5.0) and _IDLE_TICK_SECONDS (5.0) module constants
  - ScanWorker._stopping (threading.Event)
  - ScanWorker.profile_names / has_profile / get_profile / _set_profiles (D-19 lock)
affects:
  - 26-06 (guarded loop, degraded health, idle housekeeping hang off _run and _IDLE_TICK_SECONDS)
  - 26-08 (startup generation replaces _maybe_auto_generate; uses _set_profiles)
  - 26-09 (lifespan consumes stop()'s return value)
  - 26-10 (routes consume SubmitResult and profile_names/has_profile)
tech-stack:
  added: []
  patterns:
    - "queue.Queue.shutdown(immediate=True) as the wake-up for a get(timeout=...) loop; no sentinel"
    - "copy-on-write profile updates: rebind a new dict under a lock, readers take a locked snapshot"
key-files:
  created: []
  modified:
    - src/saneless/worker.py
    - tests/test_worker.py
decisions:
  - "Queue depth is a module constant _QUEUE_DEPTH = 10 (not configurable) rather than a bare literal"
  - "A job ended by shutdown logs at INFO without a traceback: it is the expected shutdown path, not a failure"
  - "_maybe_auto_generate merges {**current, **generated} and rebinds via _set_profiles (only the worker thread writes, so the read-then-rebind needs no single critical section); 26-08 replaces the method"
  - "TestWorkerProfileLock and TestWorkerStopAndSubmit construct workers without starting them where no thread is needed"
metrics:
  duration: 35min
  completed: 2026-09-14
  tasks: 2
  files: 2
---

# Phase 26 Plan 04: Worker stop flag, non-blocking submit and profile lock Summary

`ScanWorker` now stops by setting an Event and shutting its queue down: no sentinel, no blocking `put`, a join capped at `STOP_JOIN_SECONDS` that reports whether the thread actually stopped. `submit()` returns `ACCEPTED`, `QUEUE_FULL` or `DOWN` and never blocks. An open flip wait, or one reached after stopping began, is aborted at once and recorded as `RESTART_REASON`. Every profile read goes through one lock, and updates swap in a new dict instead of editing the old one.

## Tasks

| Task | Name | RED commit | GREEN commit |
| ---- | ---- | ---------- | ------------ |
| 1 | Stop flag, bounded join, non-blocking submit, converted tests | 3abfe19 | e1663e3 |
| 2 | One lock for every profile read and update (D-19) | fbbdec4 | 4ba8193 |

## What changed

**src/saneless/worker.py**
- `stop() -> bool`: sets `_stopping`, calls `_queue.shutdown(immediate=True)`, arms and aborts any live flip coordinator, joins only if the thread is alive, and returns `not is_alive()`. The docstring tells the lifespan to leave the store open when this returns False (D-09).
- `submit() -> SubmitResult`: returns `DOWN` if the thread is not alive or is stopping. `put_nowait` maps `queue.Full` to `QUEUE_FULL` and `queue.ShutDown` to `DOWN`.
- `_run`: `while not _stopping` loops on `get(timeout=_IDLE_TICK_SECONDS)`. `Empty` continues, `ShutDown` breaks, and a job dequeued once stopping has begun is not started.
- `_status_cb`: after `coordinator.arm()` on AWAITING_FLIP, calls `signal_abort()` when stopping (Pitfall 5).
- Failure branch: when stopping, writes `error=RESTART_REASON, error_category=None`. Otherwise it behaves as before.
- `_profiles_lock` plus `profile_names` / `has_profile` / `get_profile` / `_set_profiles`. `_process_job` calls `self.get_profile(job.profile)`, and `_maybe_auto_generate` no longer changes the dict in place.

**tests/test_worker.py**
- Converted to `wait_for_state` before `stop()` (ROBU-03): `test_worker_processes_job`, `test_worker_sets_error_on_failure`, the four `TestScanWorkerManualDuplex` tests that slept (their AWAITING_FLIP poll loops too), both `TestWorkerIntermediateStates`, all five `TestWorkerErrorCategories`, `test_jobs_processed_sequentially`, and `test_worker_status_cb_dispatches_on_pipeline_event`.
- Left as they were, as the plan says: `test_worker_prunes_after_job_completion` and `test_finish_prunes_history_after_a_fallback_outcome` (plan 26-06), and `TestLazyAutoGenerate` (plan 26-08). All still pass because each job finishes well within its sleep.
- New `TestWorkerStopAndSubmit` (7 tests): an idle stop returns in under 0.5 s with the tick at 5 s; stop is safe twice and before start; submit returns DOWN before start and after stop; a full queue gives QUEUE_FULL in under 0.1 s; stop on a full queue returns False in under 1 s with the join shortened, and the thread exits after release; an open flip wait gets RESTART_REASON within 1 s; reaching the flip prompt while stopping aborts well inside the state budget.
- New `TestWorkerProfileLock` (5 tests, `-k profile_lock`): the names list is a copy, `has_profile`, `get_profile`, rebinding leaves the old dict unchanged, and a stress test with 4 readers and 1 writer over 200 rounds.

## Verification

- `uv run pytest tests/test_worker.py -q`: 72 passed. The slowest test takes 1.00 s (`TestLazyAutoGenerate`, which still sleeps); nothing comes near 6 s.
- `uv run pytest tests/test_web.py tests/test_web_state_rendering.py tests/test_outcomes_e2e.py -q`: 106 passed. Routes and app ignore the new return values and still type-check.
- `uv run pytest -m "not browser and not sane_hardware" -q`: 1210 passed
- `uv run ruff check src tests`, `uv run ruff format --check .`, `uv run ty check`, `uv run pyrefly check src tests`: all clean. pyrefly's 4 warnings were already there before this plan, in `pipeline.py:384`, `sane_backend.py:997` and `tests/fake_sane.py:414`.
- Acceptance greps: `def stop(self) -> bool`, `def submit(self, job: Job) -> SubmitResult` and `shutdown(immediate=True)` each match once. There is no `put(None)`, no `.put(job)` and no `Queue[Job | None]`. The 4 profile accessor signatures are present, there is no `self._settings.profiles[`, and `self.get_profile(job.profile)` matches once.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Test read `flip_timeout_seconds` from the wrong model**
- **Found during:** Task 1 GREEN
- **Issue:** The new pass-A stopping test asserted `settings.flip_timeout_seconds`, but the field is on `settings.output`.
- **Fix:** Changed it to `settings.output.flip_timeout_seconds`. The fix went into the GREEN commit because it is a one-line correction to a RED test.
- **Files modified:** tests/test_worker.py
- **Commit:** e1663e3

Everything else in the plan was carried out as written.

## TDD Gate Compliance

- Task 1: `test(26-04)` 3abfe19 comes before `feat(26-04)` e1663e3. At RED, all 7 new tests failed and the 60 converted tests passed.
- Task 2: `test(26-04)` fbbdec4 comes before `feat(26-04)` 4ba8193. At RED, all 5 new tests failed.

## Known Stubs

None.

## Threat Flags

None. The changes stay inside the plan's threat model: T-26-11..T-26-15 are all mitigated and tested.

## Self-Check: PASSED

- FOUND: src/saneless/worker.py, tests/test_worker.py
- FOUND commits: 3abfe19, e1663e3, fbbdec4, 4ba8193
