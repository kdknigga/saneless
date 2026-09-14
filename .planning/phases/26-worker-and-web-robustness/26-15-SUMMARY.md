---
phase: 26-worker-and-web-robustness
plan: 15
subsystem: worker
tags: [robustness, job-store, idle-housekeeping, gap-closure, tdd]
requires:
  - 26-06 (loop guard, _unrecorded_failures, degraded/probe recovery)
provides:
  - ScanWorker._flush_unrecorded_failures shared by the degraded and non-degraded idle paths
  - owed ERROR writes retried on every idle tick regardless of degraded state
affects:
  - src/saneless/worker.py
  - docs/explanation/architecture.md
tech-stack:
  added: []
  patterns:
    - owed writes retried on idle ticks, uncounted towards degraded
key-files:
  created: []
  modified:
    - src/saneless/worker.py
    - tests/test_worker.py
    - tests/test_web_state_rendering.py
    - docs/explanation/architecture.md
decisions:
  - "CR-01: owed job-failure writes are retried on every idle tick, degraded or not; the store probe and degraded clear stay degraded-only (D-12)"
  - "A failed owed-write retry logs DEBUG and is not counted towards degraded; the guard already counted the failure that created the debt (D-10)"
requirements: [ROBU-01]
metrics:
  duration: ~25 min
  completed: 2026-09-14
  tasks: 2
  files: 4
---

# Phase 26 Plan 15: Owed job-store writes retried below the degraded threshold Summary

A job whose SCANNING write and the loop guard's best-effort ERROR write both failed now reaches ERROR on the next idle tick after the store heals, even when the worker never became degraded. The fix is a shared `_flush_unrecorded_failures()` helper that `_idle_housekeeping` calls on every non-degraded tick and `_try_recover` calls after a successful probe. This closes CR-01.

## What changed

- **`ScanWorker._flush_unrecorded_failures()`** (src/saneless/worker.py): writes each owed `finish_job(..., JobState.ERROR, ...)`. It removes an entry only after its write returns and logs INFO "Recorded job %s as failed now that the job store accepts writes". If nothing is owed, it returns without touching the store. If a write raises, the exception propagates: rows already written stay written and the rest wait for the next tick. Its docstring notes it only runs on an Empty tick, so an owed id can never be the job in flight (T-26-62).
- **`_idle_housekeeping()`**: if degraded, it calls `_try_recover()` as before. Otherwise it calls `_flush_unrecorded_failures()` and logs any failure at DEBUG. That failure is not passed to `_record_loop_failure()` (T-26-61). The prune block is unchanged.
- **`_try_recover()`**: the inline loop is replaced by the helper, which still runs before `fail_active_jobs(RESTART_REASON)`. The probe, counter reset, degraded clear and INFO log are unchanged. `self._job_store.probe()` still appears only here.
- Docstrings and comments that said only the recovery probe writes owed rows now say idle ticks retry them (`_run`, `_best_effort_fail`, the `_unrecorded_failures` comment).
- **docs/explanation/architecture.md** "Failure handling." paragraph: when the worker has no job, every 5 seconds it retries failure records it could not write, degraded or not. While degraded it also probes and clears degraded on the first probe that succeeds.

## Tasks

| Task | Name | Commit | Files |
| ---- | ---- | ------ | ----- |
| 1 | RED: fix the locked-in test, add tests showing a stranded row is never reconciled below the degraded threshold | 95de2b4 | tests/test_worker.py, tests/test_web_state_rendering.py |
| 2 | GREEN: retry owed writes on every idle tick, probe only while degraded | 22e1eba | src/saneless/worker.py, docs/explanation/architecture.md |

No REFACTOR commit was needed.

## Tests

- `test_a_failed_best_effort_write_is_only_logged` is now `test_a_failed_best_effort_write_is_retried_until_it_lands`. It uses a fast tick and a single stranded job, and waits for that row to reach ERROR (with `_DISK_ERROR` and its classified category) before submitting a second job, which reaches DONE. It also asserts HEALTHY and a WARNING carrying `exc_info`. The old "Still active" assertion is gone.
- New `test_owed_failures_below_the_degraded_threshold_are_written_once_the_store_heals[1|2]`: `finish_job` fails until healed and `probe` is spied. Before healing it checks that at least two idle retries happened, every row is still active, health is HEALTHY, `_consecutive_loop_failures == stranded` and there were no probe calls. After `heal()` it checks that every row reaches ERROR with the guard's text and category, `latest_run_job()` is not active, `_unrecorded_failures == {}`, there are still no probe calls, and health is HEALTHY.
- New `test_status_poll_reenables_the_scan_button_once_an_owed_failure_is_written` (web): the store's writes are wrapped in `_BreakableWrite`, which raises while a `broken` Event is set. The test POSTs `/api/scan` and waits for at least 2 `finish_job` calls. At that point `#scan-btn` is `disabled` and `/health` returns 200. It then clears `broken`, polls until the button is enabled, and checks the row is ERROR and `/health` is still 200. The app construction moved into `_make_app(tmp_path)`, which the `client` fixture now uses.

## TDD Gate Compliance

- RED gate `95de2b4 test(26-15): ...` touched no file under src/. Against the unchanged worker.py, exactly the 4 target tests failed and the other 180 in the two files passed:
  - the corrected worker test and both parametrized cases: `RuntimeError: Job ... did not reach DONE, ERROR, FALLBACK within 2.0s (last observed: PENDING)`, raised in `wait_for_state`
  - the web test: `assert False where False = _poll_until(<lambda finishes.calls >= 2>, 2.0)`, because the guard's single `finish_job` call is never retried
  - no import, fixture, name or type errors
- GREEN gate `22e1eba feat(26-15): ...` comes after RED. All 184 tests in the two files pass. The new and degraded tests passed 5 runs in a row.

## Verification

- `uv run pytest -m "not browser and not sane_hardware" -q`: 1389 passed
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, `uv run pyrefly check src tests` (0 errors) and `uv run prek run --all-files` are all clean
- `--durations=5` on tests/test_worker.py: the slowest test takes 0.22 s
- Grep criteria: `self._flush_unrecorded_failures()` appears 2 times, `self._job_store.probe()` 1 time, the `for job_id, (error, category) in list(...)` loop appears once (in the helper), and the architecture doc contains "degraded or not"

## Deviations from Plan

**1. [Rule 3 - Blocking] Web test store wrappers are a small class, not closures**
- **Found during:** Task 1
- **Issue:** `ty` narrowed a `Callable[..., object]`-annotated local to the bound method's real signature and rejected `real_finish(*args, **kwargs)` with `object` arguments (9 invalid-argument-type errors). Suppressions are forbidden.
- **Fix:** Added a module-level `_BreakableWrite(original: Callable[..., object], broken: threading.Event)` that counts calls under a lock. Passing the method as a parameter widens its type, the same way `_StoreFault` does in tests/test_worker.py. The behaviour matches the plan: it raises `sqlite3.OperationalError("disk I/O error")` while `broken` is set, delegates otherwise, and counts calls under a lock. No private helpers are imported from tests/test_worker.py.
- **Files modified:** tests/test_web_state_rendering.py
- **Commit:** 95de2b4

## Threat Flags

None. No new network endpoints, auth paths or schema changes. T-26-60..63 are mitigated as planned.

## Known Stubs

None.

## Self-Check: PASSED

- FOUND: src/saneless/worker.py (`def _flush_unrecorded_failures(self) -> None`)
- FOUND: tests/test_worker.py (`test_owed_failures_below_the_degraded_threshold_are_written_once_the_store_heals`)
- FOUND: tests/test_web_state_rendering.py (`test_status_poll_reenables_the_scan_button_once_an_owed_failure_is_written`, `_make_app`)
- FOUND: commits 95de2b4 (test) and 22e1eba (feat)
