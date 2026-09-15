---
phase: 26-worker-and-web-robustness
plan: 18
subsystem: worker, web
tags: [gap-closure, WR-10, WR-11, robustness, tdd]
requires:
  - 26-15 (ScanWorker._flush_unrecorded_failures on every idle tick)
  - 26-17 (ScanWorker.owe_rejection and _unrecorded_lock)
provides:
  - "_OWED_RETRY_DEGRADED_AFTER: Final = 3, read at call time"
  - "ScanWorker._failed_flush_ticks: idle ticks in a row whose owed-write retry raised"
  - "_idle_housekeeping degrades the worker once the streak reaches the limit; a flush that returns resets it"
  - "_try_recover resets the streak with _consecutive_loop_failures before _degraded.clear()"
affects:
  - src/saneless/worker.py
  - tests/test_worker.py
  - tests/test_web_state_rendering.py
  - docs/explanation/architecture.md
  - docs/reference/web-api.md
tech-stack:
  added: []
  patterns:
    - "A separate streak counter for retry failures, kept apart from the loop-level failure count"
    - "Tests that watch a failing window raise a call-time module constant instead of racing the real limit"
key-files:
  created: []
  modified:
    - src/saneless/worker.py
    - tests/test_worker.py
    - tests/test_web_state_rendering.py
    - docs/explanation/architecture.md
    - docs/reference/web-api.md
decisions:
  - "WR-10: three failed owed-write idle ticks in a row degrade the worker, counted in _failed_flush_ticks and never folded into _consecutive_loop_failures (a failed retry is still not a loop-level failure, D-10)"
  - "The first failed retry of a streak logs WARNING with exc_info; later failures in the same streak log DEBUG"
  - "Supersedes 26-15's 'a failed retry is logged at DEBUG and does not count towards degraded' and 26-17's 'never counting a failed retry'"
metrics:
  duration: "7 min"
  completed: "2026-09-15"
  tasks: 3
  files: 5
---

# Phase 26 Plan 18: Owed-Write Retry Streak Degrades the Worker Summary

A job store that keeps refusing owed job-row writes now degrades the worker after three failed idle ticks in a row (about 15 s in production), so `/health` answers 503 "job store failing" instead of staying 200 until an hourly prune fails. The streak lives in its own counter `_failed_flush_ticks`, resets on any flush that returns and on recovery, and the WR-11 read race in the below-threshold heal test is closed.

## What Was Built

**Task 1 (RED, 1abc001).** Added `TestOwedWriteStreak` (between `TestOwedRejections` and `_DEGRADING_JOBS`) and one web test:
- `test_a_persistent_owed_write_fault_degrades_the_worker_in_bounded_idle_ticks[guard|rejection]`: a `finish_job` that never heals degrades the worker. The streak equals the limit. The production bound is at most 30 s. The loop count stays at 1 for a guard debt and 0 for a rejection debt. The row stays active, `submit` returns DEGRADED, there is exactly one WARNING with `exc_info`, and a "degraded" WARNING is logged.
- `test_a_worker_degraded_by_a_failed_retry_streak_recovers_once_the_owed_write_lands`: after healing, the worker is HEALTHY again. The row is ERROR with the guard's text, the owed dict is empty (read under the lock), both counters are 0, and a new scan finishes DONE.
- `test_owed_write_retries_failing_fewer_ticks_than_the_streak_never_degrade`: two separate 2-tick streaks run with the production limit. The worker never probes, stays HEALTHY, and logs exactly two WARNINGs.
- `test_health_reports_the_job_store_failing_while_an_owed_failure_cannot_be_written`: `/health` returns 503 with the exact JSON, a second POST `/api/scan` returns 503, and after healing `/health` is 200, `#scan-btn` is enabled and the row is ERROR.

All 5 failed against unchanged src for the intended reasons: `assert degraded` was False (3 tests), the WARNING count was 0 instead of 2, and the `/health` 503 poll was False. There was no AttributeError, NameError or fixture error. The other 187 tests in both files passed.

**Task 2 (GREEN, 90109b7).** `worker.py`:
- Added `_OWED_RETRY_DEGRADED_AFTER: Final = 3` with its rationale comment.
- Added `_failed_flush_ticks` in `__init__`.
- In the non-degraded branch of `_idle_housekeeping`: on a raise, increment the streak, log WARNING on the first failure and DEBUG after that, and set degraded at the limit with a "degraded" WARNING. In the `else` branch, reset the streak.
- `_try_recover` resets the streak before `_degraded.clear()`.
- Updated docstrings and comments.
- `_record_loop_failure()` is still called from only 2 sites.

Superseded tests:
- Added `_UNREACHABLE_STREAK = 1_000_000`, and both `below_the_degraded_threshold` cases now patch the limit to it.
- WR-11 fix: wait for the owed dict to empty, then copy it under `_unrecorded_lock`.
- Renamed the owe-rejection retry test to `test_a_failed_owed_rejection_retry_is_not_a_loop_level_failure` and gave it the same patch.
- Rewrote the `TestOwedRejections` docstring.
- The web scan-button test now raises the limit too.

Results: 281 tests passed across the worker, web-state, web-errors and app-lifespan files, and the 10-run flake loop passed every run (10/10 tests each).

**Task 3 (docs, cce865f).** In `architecture.md`, the Failure handling paragraph now describes the three-idle-tick streak and says degraded clears once the probe succeeds and owed records are written. In `web-api.md`, the paragraph under the `GET /health` table now lists three degrading conditions and the new clearing wording. The status table is unchanged.

## Verification

- `uv run pytest -m "not browser and not sane_hardware" -q`: 1405 passed
- `uv run pytest -m browser -q`: 48 passed
- `ruff check .`, `ruff format --check .`, `ty check`, `pyrefly check src tests` (0 errors), `prek run --all-files`, and `prek run --stage pre-push --all-files`: all exit 0
- The plan's acceptance greps all match: the constant appears once, `self._failed_flush_ticks = 0` appears 3 times, the `>=` check appears once, `_record_loop_failure()` appears 2 times, the reset comes before `clear()` in `_try_recover`, the limit patch appears 2 times in test_worker and 1 time in test_web_state_rendering, and "three idle ticks in a row" appears once in each doc.

## Deviations from Plan

None in behaviour. Small implementation choices:
- WARNING-vs-DEBUG uses an explicit `if/else` with two logger calls rather than choosing a bound method, which keeps both type checkers simple. The message text is identical.
- The guard-debt `update_state` fault in the parametrized test is installed before `worker.start()`, next to the other store patches, not inside the `try`.
- The recovery and sub-threshold tests capture `drained = _wait_until(...)` and assert it after `finally`, so the worker is always stopped first. The WR-11 fix in the existing test uses the plan's inline `assert _wait_until(...)` form.

Environment note: this worktree started on master (ed2d620), not the expected base, so it was reset to bf69398 before any work, as the worktree branch check instructs.

## TDD Gate Compliance

RED `test(26-18)` 1abc001, then GREEN `feat(26-18)` 90109b7. No refactor commit was needed.

## Known Stubs

None.

## Self-Check: PASSED

- FOUND: src/saneless/worker.py, tests/test_worker.py, tests/test_web_state_rendering.py, docs/explanation/architecture.md, docs/reference/web-api.md
- FOUND commits: 1abc001, 90109b7, cce865f
