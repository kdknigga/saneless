---
phase: 26-worker-and-web-robustness
plan: 19
subsystem: job store, worker, web
tags: [gap-closure, IN-08, D-06, D-17, robustness, tdd]
requires:
  - 26-17 (ScanWorker.owe_rejection and _unrecorded_lock)
  - 26-18 (_OWED_RETRY_DEGRADED_AFTER, test constant _UNREACHABLE_STREAK)
provides:
  - "JobStore.latest_run_job(exclude_ids: Collection[str] = ()) with a bound LIMIT of len(exclude_ids) + 1"
  - "ScanWorker.owed_rejection_ids() -> frozenset[str], only owed REJECTED entries, read under _unrecorded_lock"
  - "_current_or_recent_job passes worker.owed_rejection_ids() as exclude_ids"
affects:
  - src/saneless/job.py
  - src/saneless/worker.py
  - src/saneless/web/routes.py
  - tests/test_job.py
  - tests/test_worker.py
  - tests/test_web_errors.py
tech-stack:
  added: []
  patterns:
    - "Exclusion filtered in Python over a bounded, fully bound-parameter query instead of building an IN list"
    - "Owed state exposed as an immutable snapshot taken under the lock, not the private dict"
key-files:
  created: []
  modified:
    - src/saneless/job.py
    - src/saneless/worker.py
    - src/saneless/web/routes.py
    - tests/test_job.py
    - tests/test_worker.py
    - tests/test_web_errors.py
decisions:
  - "IN-08: the status lookup skips ids whose REJECTED write is still owed to the worker. This applies D-06 to a rejection that is not written yet; D-17 is unchanged"
  - "Only owed REJECTED entries are skipped. A guard-owed failure belongs to a job that ran, so D-17 still reports it"
  - "Accepted residual window: a poll landing during the request's own write attempt, between create_job and the REJECTED write or the owe_rejection call"
metrics:
  duration: "10 min"
  completed: "2026-09-14"
  tasks: 2
  files: 6
---

# Phase 26 Plan 19: Owed Refused Attempts Stay Out of the Status Area Summary

A refused scan whose REJECTED write failed in the request, and is now owed to the worker, no longer shows as "Starting scan..." with a disabled Scan button. The status lookup now calls `latest_run_job(exclude_ids=worker.owed_rejection_ids())`. The owed set holds only REJECTED entries, so a job that actually ran is still reported as the job that just ended.

## What Was Built

**Task 1 (RED, b1b96a7).** Seven tests:
- `tests/test_job.py` (`TestQueryMethods`):
  - `test_latest_run_job_skips_excluded_ids` excludes 1, 2 or all 3 of the newest rows.
  - `test_latest_run_job_exclusions_combine_with_the_rejection_skip` excludes an owed row and skips a written rejection in the same query.
  - `test_latest_run_job_ignores_excluded_ids_that_match_no_row` excludes ids that match no row.
- `tests/test_worker.py` (`TestOwedRejections`): `test_owed_rejection_ids_lists_only_rejections_still_owed`. A guard-owed failure and an owed rejection are pending together. The set is exactly `{refused.id}` and is a `frozenset`. It is empty once the store heals and the row is written.
- `tests/test_web_errors.py`: `test_a_refused_attempt_whose_rejection_is_still_owed_is_not_shown_as_the_live_job[queue_full|down|degraded]`. `finish_job` always fails, and the row is still PENDING while the store alone would pick it. The poll must show "Ready to scan.", must not show "Starting scan...", and must render `#scan-btn` without `disabled`. After the poll checks, the test asserts the id is in `owed_rejection_ids()`.

All 7 failed against unchanged src, each for an acceptable reason:
- The 3 store tests raised `TypeError` naming the unexpected keyword `exclude_ids`.
- The worker test raised `AttributeError` naming `owed_rejection_ids`.
- The 3 web cases failed on `'Ready to scan.' in poll`, because the page rendered "Scanning..." with `disabled`.

The other 241 tests in the three files passed.

**Task 2 (GREEN, a9b4437).**
- `job.py`:
  - `Collection` is now imported under `TYPE_CHECKING`.
  - `_SELECT_LATEST_RUN` ends in `LIMIT ?`, and its docstring now describes two bound parameters.
  - `latest_run_job` builds `skip = frozenset(exclude_ids)`, fetches `len(skip) + 1` rows and returns the first row whose id is not in `skip`.
  - Its docstring gained an `Args:` entry, an explanation of why the LIMIT is enough, and an updated `Returns:`.
- `worker.py`: `owed_rejection_ids()` sits right after `owe_rejection`. It filters `_unrecorded_failures` to `ErrorCategory.REJECTED` under the lock. I confirmed that `classify_error` only returns FEEDER, CONFIG, SCANNER, UPLOAD or UNKNOWN.
- `routes.py`: `_current_or_recent_job` passes the owed ids to the store, and its docstring now describes IN-08 and the accepted residual window.

No refactor commit was needed.

## Verification

- The four named test files: 339 passed.
- `uv run pytest -m "not browser and not sane_hardware" -q`: 1412 passed (1405 before this plan, plus 7).
- `uv run pytest -m browser -q`: 48 passed.
- `ruff check .`, `ruff format --check .`, `ty check` and `pyrefly check src tests` (0 errors) are all clean. `prek run --all-files` and `prek run --stage pre-push --all-files` passed every hook.
- Flake loop: the worker test and the 3 web cases passed 10 runs out of 10.
- Acceptance greps:
  - The signature line matches once, and `len(skip) + 1` matches once.
  - `LIMIT 1` no longer appears in job.py.
  - `latest_run_job(` appears once in routes.py.
  - IN-08 appears in all three src files.

## Deviations from Plan

None in behaviour. Notes:
- The acceptance grep `ORDER BY created_at DESC LIMIT ?` matches 3 lines in job.py, not 1. The other two are the existing `_SELECT_RECENT` and `_NEWEST_IDS` constants. The criterion's intent still holds: `_SELECT_LATEST_RUN` binds its LIMIT and `LIMIT 1` is gone.
- `owed_rejection_ids`'s docstring summary starts with "List ..." to satisfy ruff D401.
- The store test binds each `latest_run_job(...)` result to a local and asserts `is not None` before `.id`, so both type checkers can narrow the Optional. The web test narrows the same way.
- The web test gets its error text from the parametrize tuple and uses it only in the assertion message, so the `error` parameter is not unused.

Environment note: this worktree started on master (ed2d620), so it was reset to 41d8ca0 before any work, as the worktree branch check instructs.

## TDD Gate Compliance

RED `test(26-19)` b1b96a7 came before GREEN `feat(26-19)` a9b4437.

## Known Stubs

None.

## Threat Flags

None. No new endpoints or trust-boundary surface. The SQL change only adds one bound integer parameter (T-26-80).

## Self-Check: PASSED

- FOUND: src/saneless/job.py, src/saneless/worker.py, src/saneless/web/routes.py, tests/test_job.py, tests/test_worker.py, tests/test_web_errors.py
- FOUND commits: b1b96a7, a9b4437
