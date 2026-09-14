# Phase 25 Deferred Items

## From plan 25-09 (docs)

### A flip answer sent before the flip wait is kept and used -- RESOLVED by 25-10 and 25-11

- **Found while:** documenting the flip endpoints in `docs/reference/web-api.md`
- **What:** `ScanWorker._process_job` creates the `WorkerFlipCoordinator` when a manual-duplex
  job starts, before pass A. `continue_flip` / `abort_flip` signal it whenever it exists, and the
  coordinator's first answer is final. A `POST /api/flip/continue` that arrives during pass A is
  therefore claimed as `CONTINUED`, and `wait_for_flip` returns at once after the fronts finish:
  pass B starts without waiting for the stack to be flipped.
- **Reach:** the original classification of this entry -- that only a direct API caller could send
  the early answer, never a browser user -- was wrong. The
  flip routes re-rendered the stale `AWAITING_FLIP` prompt, buttons included, after a click, so an
  ordinary double-click or retry in the web UI reached the defect. The verifier reproduced a second
  Abort click ending the next queued manual-duplex job before its operator ever saw a flip prompt.
  This is the code review's CR-01, rated BLOCKER; see `25-REVIEW.md` and `25-VERIFICATION.md`.
- **Resolution:** 25-10 made the worker's coordinator job-scoped and armed only as the pipeline
  announces `AWAITING_FLIP` (it is armed before that state is persisted); signals sent before arming
  or naming another job are dropped, and the flip routes require a `job_id` form field that the
  flip buttons send via `hx-vals`. 25-11 made the status partial render an acknowledgment
  (`Flip confirmed. Scanning reverse sides next...` / `Aborting scan...`) in place of the buttons
  once the wait is answered. Regression tests live in `tests/test_worker.py` (for example
  `test_signals_during_pass_a_are_dropped`, `test_a_double_clicked_abort_cannot_abort_the_next_job`)
  and `tests/test_web.py` (for example `test_double_clicked_abort_still_acknowledges`,
  `test_foreign_job_id_leaves_the_prompt_open`, `test_flip_routes_require_a_job_id`).
