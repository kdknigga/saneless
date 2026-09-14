---
phase: 26-worker-and-web-robustness
plan: 16
subsystem: web, job-store
tags: [gap-closure, WR-01, robustness, sqlite, tdd]
requires: []
provides:
  - "JobStore.create_rejected_job: locked single-INSERT terminal ERROR/REJECTED row"
  - "routes._record_refused_submit (pre-row, one statement)"
  - "routes._reject_created_job (post-row finish_job; 26-17 adds its owed-write fallback here)"
affects: [26-17]
tech-stack:
  added: []
  patterns:
    - "Record a refusal that has no row yet as one INSERT of the terminal row, never create-then-finish"
    - "Trace-callback test pins the statement shape (exactly one INSERT, no UPDATE)"
key-files:
  created: []
  modified:
    - src/saneless/job.py
    - tests/test_job.py
    - src/saneless/web/routes.py
    - tests/test_web_errors.py
decisions:
  - "WR-01 pre-row half: a submit refused at the health check is written by JobStore.create_rejected_job in one INSERT; a store failure leaves no row rather than a PENDING row without the REJECTED marker"
  - "_record_rejected_submit split into _record_refused_submit (pre-row) and _reject_created_job (post-row) so 26-17 can add the post-submit fallback without an unused parameter on the pre-row path"
metrics:
  duration: "~25 min"
  completed: 2026-09-14
  tasks: 2
  files: 4
requirements: [ROBU-01, ROBU-02]
---

# Phase 26 Plan 16: Single-statement record of refused-before-row submits (WR-01) Summary

A submit refused while the worker is DOWN or DEGRADED is now written by `JobStore.create_rejected_job`, one locked INSERT of a row that is already `ERROR`/`REJECTED`. A store failure can no longer leave a PENDING row that shows "Starting scan..." forever and keeps the Scan button disabled.

## What was built

### Task 1: `JobStore.create_rejected_job`
- `@_locked` method placed directly after `create_job`, with keyword-only `error`, `tags`, `correspondent`. It runs one `with self._conn:` block containing `_INSERT` (state `ERROR`, category `REJECTED`, thumbnail and all result/owner columns NULL) and the `_SELECT_BY_ID` read-back. It calls no other public method, so `TestLockDiscipline` passes.
- The docstring covers D-05 (the attempt shows in history), D-06 (the marker keeps it out of the status area) and WR-01 (one statement).
- `TestCreateRejectedJob` in `tests/test_job.py` has three tests:
  - row shape and `get_job` read-back
  - a trace-callback test: exactly one INSERT among row-touching statements, no UPDATE
  - `latest_run_job` skips the row while `list_recent(limit=1)` still lists it

### Task 2: route wiring
- `_record_rejected_submit` is gone. In its place:
  - `_record_refused_submit(job_store, form, *, error)` calls only `create_rejected_job`.
  - `_reject_created_job(job_store, job_id, *, error)` keeps the post-submit single `finish_job(ERROR, REJECTED)`.
  - Both keep the same warning log and return `False` on failure.
- In `start_scan`, the unhealthy branch now calls `_record_refused_submit` and the post-submit refusal calls `_reject_created_job`. The `start_scan` docstring is still accurate, so it was left unchanged.
- New test `test_a_refused_submit_never_leaves_an_active_row_when_the_store_fails[down|degraded]`.
- `test_scan_degraded_store_failing_is_503_without_a_loader` now fails `create_rejected_job` instead of `create_job`, and also asserts `list_recent(limit=50) == before` (no row).
- `grep -rn "_record_rejected_submit" src tests docs` finds nothing.

## TDD Gate Compliance

| Gate | Commit | Evidence |
|------|--------|----------|
| RED (store) | 13bf421 | 3 failed: `AttributeError: 'JobStore' object has no attribute 'create_rejected_job'` (expected RED for a new API contract) |
| GREEN (store) | 5dcbdb1 | `tests/test_job.py`: 62 passed |
| RED (route) | 9c4c599 | Both params failed at `assert [job for job in store.list_recent(limit=50) if job.is_active] == []`. The surviving row was `Job(title='Refused Mid-Write', state=<JobState.PENDING ...>)`, and the log showed `finish_job` raising `disk I/O error` inside `_record_rejected_submit`. This is the real WR-01 failure, not a fixture or import error. |
| GREEN (route) | e527d48 | test_web_errors + test_web + test_job: 184 passed |

No REFACTOR commit was needed.

## Verification

- `uv run pytest -m "not browser and not sane_hardware" -q`: 1391 passed
- `uv run ruff check .`, `uv run ruff format --check .`: clean
- `uv run ty check`: all checks passed
- `uv run pyrefly check src tests`: 0 errors. The 4 hidden warnings are informational and in files this plan did not touch (pipeline.py, sane_backend.py, fake_sane.py).
- `uv run prek run --all-files`: all hooks pass
- The acceptance `inspect.getsource(_record_refused_submit)` assertion passes.

## Deviations from Plan

None in the delivered code; the plan was executed as written.

Process incident (fixed during execution, no effect on the commits): the first GREEN insert for Task 1 used Serena `insert_after_symbol`. Serena's active project is the main checkout, so the method was written to `/home/kris/git/saneless/src/saneless/job.py` instead of the worktree. I removed it there with Serena `replace_content`, fixed an indentation slip that removal caused, and confirmed the main checkout's `job.py` is byte-identical to base `54fdd97` (`cmp` against `git show 54fdd97:src/saneless/job.py`). The method was then added to the worktree copy with Edit. Nothing else in the main checkout was touched.

## Known Stubs

None.

## Threat Flags

None. The new insert uses the existing bound-parameter `_INSERT` (T-26-65) and carries `@_locked` (T-26-67). The mitigations for T-26-64 and T-26-66 are in place and tested.

## Self-Check: PASSED

- FOUND: src/saneless/job.py (`def create_rejected_job(` preceded by `@_locked`)
- FOUND: tests/test_job.py (`class TestCreateRejectedJob`)
- FOUND: src/saneless/web/routes.py (`create_rejected_job(`, `_reject_created_job(`)
- FOUND: tests/test_web_errors.py (`def test_a_refused_submit_never_leaves_an_active_row_when_the_store_fails`)
- FOUND commits: 13bf421, 5dcbdb1, 9c4c599, e527d48
