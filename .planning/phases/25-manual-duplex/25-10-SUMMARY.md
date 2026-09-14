---
phase: 25-manual-duplex
plan: 10
subsystem: worker, web
tags: [manual-duplex, flip-coordinator, concurrency, htmx, gap-closure, CR-01]
gap_closure: true
requires:
  - 25-05 (WorkerFlipCoordinator, flip routes, D-16/D-17)
provides:
  - "WorkerFlipCoordinator(job_id) with arm()/armed/answer; signals return bool and drop unarmed or late answers"
  - "ScanWorker.continue_flip(job_id)/abort_flip(job_id) -> bool, ScanWorker.flip_answer(job_id)"
  - "Flip routes require a job_id form field; flip partial posts it through hx-vals"
affects:
  - 25-11 (status rendering reads ScanWorker.flip_answer)
  - 25-14 (IN-02 shared single-answer helper must keep arming semantics)
  - 25-15 (web-api.md must document the required job_id)
tech-stack:
  added: []
  patterns:
    - "Arm-before-persist: the coordinator is armed before AWAITING_FLIP is written to the store"
    - "Single snapshot of self._flip_coordinator, compared with its own job_id (no TOCTOU across jobs)"
    - "Concrete _GatedScanner with per-call entered/gate events to stage 'click during pass A' deterministically"
key-files:
  created: []
  modified:
    - src/saneless/worker.py
    - src/saneless/web/routes.py
    - src/saneless/web/templates/partials/flip.html
    - tests/test_worker.py
    - tests/test_web.py
    - tests/test_outcomes_e2e.py
decisions:
  - "D-16 is now also job-scoped and window-scoped: an answer counts only if it names the waiting job and only after that job announced AWAITING_FLIP"
  - "Early signals are dropped, not queued; wait_for_flip arms as an idempotent backstop; _resolve stays the timeout-only unconditional claim path"
  - "The job-id check compares against the live coordinator's own job_id, never self._current_job_id"
requirements: [DPLX-06, DPLX-04]
metrics:
  duration: "~17 min"
  completed: 2026-09-14
  tasks: 2
  commits: 4
---

# Phase 25 Plan 10: Job-scoped, armed flip coordinator (CR-01) Summary

**What changed:** a flip answer now belongs to one job, and it counts only after that job announces `AWAITING_FLIP`. `WorkerFlipCoordinator` is built with the job id. It is armed in `_status_cb` before `AWAITING_FLIP` is written to the store. Any signal that arrives before arming, or after the answer is taken, is dropped and returns `False`. The routes, the worker and the flip partial all carry `job_id`, and the worker only signals the coordinator whose own `job_id` matches. The verifier's double-click Abort scenario now ends with job 2 `DONE`, and a Continue sent during pass A is no longer kept.

## Tasks

| # | Task | Commits |
|---|------|---------|
| 1 | Bind `WorkerFlipCoordinator` to one job; arm it only when AWAITING_FLIP is announced | `211664d` (test), `0f12c1c` (feat) |
| 2 | Job-scoped flip signals from routes to worker; CR-01 race regressions through the real pipeline | `df1f285` (test), `babf9c7` (feat) |

## Implementation notes

- `WorkerFlipCoordinator`:
  - New pieces: `__init__(job_id)`, the read-only `job_id`, `armed` and `answer` properties (the last two read under the lock), and an idempotent `arm()`.
  - `signal_continue` and `signal_abort` go through a new `_signal` method. It checks `armed` and "no answer yet" under the lock, stores the answer, then sets the event. The existing rule still holds: the answer is written before the event is set.
  - `_resolve` is still the unconditional claim path, and only the timeout uses it.
  - `wait_for_flip` calls `arm()` first.
- `_process_job` binds a local `coordinator`, passes it to `PipelineRequest`, and calls `coordinator.arm()` right before `update_state(_jid, state)` for AWAITING_FLIP. A comment explains why it must come first.
- `ScanWorker._signal_flip(job_id, action)` reads `self._flip_coordinator` once and compares that coordinator's `job_id`. It logs one of these:
  - `claimed`
  - `dropped: not the job waiting at the flip prompt`
  - `dropped: not yet at the flip prompt`
  - `dropped: already answered: <OUTCOME>`

  The old unconditional "signal sent" log is gone (IN-03).
- Routes: `continue_flip` and `abort_flip` take `job_id: str = Form(...)`. They still render `_current_or_recent_job` (D-17 unchanged; the acknowledgment belongs to 25-11).
- `flip.html`: both buttons now have `hx-vals='{{ {"job_id": job.id} | tojson }}'`. Nothing else in the template changed.

## Verification

- `uv run pytest -q`: 1057 passed, including the browser tests.
- `ruff check .`, `ruff format --check .`, `ty check` and `pyrefly check src tests` are all clean. `prek run --all-files` and `prek run --stage pre-push --all-files` both pass.
- Mutation check (not committed): I removed `coordinator.job_id != job_id` from `_signal_flip`. `test_a_double_clicked_abort_cannot_abort_the_next_job` then failed at the stale `abort_flip(job1.id)` at job 2's prompt, and both jobs ended with `ERROR: Manual duplex scan aborted at the flip prompt`. That is exactly CR-01. I restored the check afterwards.
- Stability: I ran the job-scoped, arm-before-persist and current-job tests 8 times in a row, and all passed every time.
- Browser check: a scratch Playwright script ran real Chromium against a live uvicorn server with a job in AWAITING_FLIP. Clicking Continue and Abort each sent `job_id=<that job's id>` as form data, and the server returned 200. Playwright MCP was not available to this agent, so I ran the check through `playwright.sync_api` instead.
- Acceptance greps:
  - `def arm`, both `-> bool` signals, `WorkerFlipCoordinator(job.id)`, `continue_flip`/`abort_flip(self, job_id: str) -> bool` and `flip_answer` each match once.
  - `arm()` (line 455) comes before `update_state(_jid, state)` (line 456).
  - `WorkerFlipCoordinator()` appears 0 times in tests, "signal sent" 0 times in worker.py, and `hx-vals` twice in flip.html.
  - `job_id: str = Form(...)` matches twice.
  - No `continue_flip()` or `abort_flip()` call is left in src or tests.
  - `-k "scoped or double"` selects 4 tests.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] `test_current_job_id_tracked` sent Continue without waiting for the flip prompt**
- **Found during:** Task 1 (RED)
- **Issue:** The test sent `continue_flip()` as soon as `current_job_id` was set, which can be before AWAITING_FLIP. With arming, that Continue is dropped, and the job would sit in the mock pipeline's 5 s flip wait, racing `stop()`'s 5 s join.
- **Fix:** The test now waits for AWAITING_FLIP before sending Continue, and waits for a terminal state instead of `time.sleep(0.5)`.
- **Files modified:** tests/test_worker.py
- **Commit:** `211664d`

**2. [Rule 3 - Blocking] Serena edits landed in the main checkout, not the worktree**
- **Found during:** Task 1 (RED)
- **Issue:** Serena's project root is `/home/kris/git/saneless`. My first `replace_symbol_body` call therefore rewrote `TestWorkerFlipCoordinator` in the main checkout's `tests/test_worker.py`, not in this worktree.
- **Fix:** I put the original class body back with Serena. `cmp` then showed the main-checkout file is byte-identical to the committed `HEAD:tests/test_worker.py`. After that I made every edit with Edit/Write on absolute worktree paths. Nothing in the main checkout was left modified.
- **Files modified:** none in the end (the main checkout was restored)

### Test additions beyond the plan's minimum
- `test_with_no_job_waiting_every_signal_is_dropped`: when no coordinator exists, `flip_answer` is `None` and both signals return `False`.
- `test_arming_is_idempotent`, plus separate armed and unarmed timeout tests.

## Known Stubs

None.

## Threat Flags

None. The only new surface is the required `job_id` form field, which T-25-45 and T-25-47 already cover. `job.id` is rendered through Jinja `tojson` inside a single-quoted attribute.

## Notes for downstream plans

- `docs/reference/web-api.md` still describes `POST /api/flip/continue` and `/api/flip/abort` with no request body. Plan 25-15 already lists `job_id` for that file.
- 25-14 (IN-02, the shared single-answer helper) must keep the split between `_signal` (checks `armed`) and `_resolve` (the unconditional timeout claim). Merging them into one path would bring CR-01 back.

## TDD Gate Compliance

Both tasks followed the order RED `test(25-10)` then GREEN `feat(25-10)`: `211664d` → `0f12c1c`, then `df1f285` → `babf9c7`. Each RED commit failed for the expected reasons: 14 failures in Task 1 and 17 in Task 2, from the missing constructor argument, `arm`/`armed`/`answer`, the bool returns, `flip_answer`, the `job_id` signatures, `hx-vals`, and the 422 on a missing `job_id`. Neither task needed a refactor commit.

## Self-Check: PASSED

- FOUND: src/saneless/worker.py, src/saneless/web/routes.py, src/saneless/web/templates/partials/flip.html, tests/test_worker.py, tests/test_web.py, tests/test_outcomes_e2e.py
- FOUND commits: 211664d, 0f12c1c, df1f285, babf9c7
