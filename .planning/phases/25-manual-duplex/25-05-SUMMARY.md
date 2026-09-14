---
phase: 25-manual-duplex
plan: 05
subsystem: worker, web
tags: [manual-duplex, flip, worker, routes, e2e, tdd]
requires:
  - 25-02 (JobState.SCANNING_REVERSE persisted)
  - 25-03 (WorkerFlipCoordinator, FlipOutcome, flip timeout raise)
  - 25-04 (profile.duplex as sole strategy reader)
provides:
  - transition-event protocol deleted from src/ and tests/
  - routes._current_or_recent_job shared by index, current_job_status, continue_flip, abort_flip
  - end-to-end proof that a flip timeout fails the job and frees the worker thread
affects:
  - src/saneless/worker.py
  - src/saneless/web/routes.py
tech-stack:
  added: []
  patterns:
    - concrete gated scanner stub (second scan_pages waits on a test-held Event) to make a transient state observable
    - existing conftest wait_for_state with explicit 2 s budgets, no new sleep loops
key-files:
  created: []
  modified:
    - src/saneless/worker.py
    - src/saneless/web/routes.py
    - tests/test_worker.py
    - tests/test_web.py
    - tests/test_outcomes_e2e.py
decisions:
  - "D-17 extraction covers four sites (index included), as the plan recorded"
  - "_current_or_recent_job takes (worker, job_store), not request, following _get_cached_or_fetch"
  - "A separate legacy-duplex-source e2e case keeps DPLX-02's 'still loads and scans' proven after _DUPLEX_SOURCE moved to the two-key form"
  - "_Case.duplex is Optional: None omits the key so the legacy translation actually runs (an explicit 'none' blocks it)"
  - "_Case.operator_flips distinguishes 'parks at the prompt' from 'the test answers it'; the flip-timeout case skips the AWAITING_FLIP wait because a zero timeout passes through that state in microseconds"
metrics:
  duration: ~25 min
  completed: 2026-09-14
  tasks: 3
  files: 5
requirements: [DPLX-05, DPLX-06]
---

# Phase 25 Plan 05: Delete the transition-event protocol, share the route lookup, prove the flip timeout Summary

`ScanWorker.wait_transition` and `_transition_event` are gone entirely. `continue_flip` no longer blocks the event loop for 2 s. All four status-rendering routes now use one `_current_or_recent_job(worker, job_store)` lookup, so a flip route that arrives as a job ends reports that job instead of "Ready to scan.". An end-to-end test proves a zero-second flip timeout fails the job and that the next job still runs.

## Tasks

| Task | Name | Commit | Files |
| ---- | ---- | ------ | ----- |
| 1 (RED) | Pin pass-B visibility, drop defect-pinning tests, pin route lookup | 1afda50 | tests/test_worker.py, tests/test_web.py |
| 2 (GREEN) | Delete transition-event protocol, extract shared route lookup | c648f9c | src/saneless/worker.py, src/saneless/web/routes.py |
| 3 | Prove flip timeout fails the job and releases the worker | de51fef | tests/test_outcomes_e2e.py |

## What changed

- **worker.py:** removed the `_transition_event` attribute, the whole `wait_transition` method, all `set()` calls (after SCANNING, busy states and finish) and the `clear()` call. `_status_cb` now just persists every active state that differs from the last one it wrote. The unused `BUSY_STATES` import is gone.
- **routes.py:** added the module-level helper `_current_or_recent_job(worker, job_store) -> Job | None`, with a full docstring. It replaced the full copies in `index` and `current_job_status` and the partial copies (current job only) in `continue_flip` and `abort_flip`. The `wait_transition(timeout=2.0)` call is deleted outright, with no shorter replacement wait. Every handler still returns the same `partials/status.html` response. `grep -c _current_or_recent_job` returns 5 (one definition, four calls).
- **tests/test_worker.py:** added `_PassBGatedScanner`, a concrete `ScannerBackend` subclass whose second `scan_pages` waits on `release_pass_b` (up to a 4 s ceiling). Replaced `TestWorkerFlipTiming` with `TestWorkerPassB`, which runs the real `run_pipeline` and covers three things:
  - pass B is persisted as `SCANNING_REVERSE`, then the job ends `DONE`;
  - Abort at the prompt ends `ERROR` naming the flip prompt, with only one scan call;
  - a late Abort after Continue is dropped and the job ends `DONE` (D-16).

  The fallback-waiter test now uses `wait_for_state` instead of `_transition_event`.
- **tests/test_web.py:** `test_flip_continue` and `test_flip_abort` now create a finished job with no current job id and require the returned partial to name that job, not "Ready to scan.".
- **tests/test_outcomes_e2e.py:** changes to the case table and one new test:
  - `_Case` gains `flip_timeout` (passed through `_build_settings` into `OutputConfig.flip_timeout_seconds`), `duplex` and `operator_flips`.
  - `_DUPLEX_SOURCE = "ADF"`, and the mismatch case now sets `duplex="manual"`.
  - New `legacy-duplex-source` case: `source = "ADF Manual Duplex"` with no duplex key, scanned end to end to `DONE`.
  - New `flip-timeout` case: `flip_timeout=0`, never answered, ends `ERROR` with "flip wait timed out" / "nobody confirmed".
  - New `TestFlipTimeoutReleasesTheWorker`: after the timed-out job, a simplex job on `default` reaches `DONE`. A comment notes the device handle is already released between passes; what the timeout frees is the worker thread.

## DPLX-06 grep gate (verbatim)

```
$ grep -rn "wait_transition\|_transition_event" src/ tests/; echo "exit=$?"
exit=1
```

No output (grep exit 1 means no matches) across both `src/` and `tests/`.

## Verification

- `uv run pytest -q`: 1023 passed (966-test baseline plus earlier-wave additions; no regressions)
- `uv run pytest tests/test_outcomes_e2e.py`: 8 passed in 0.26 s; slowest test 0.04 s, no wall-clock waits
- `uv run pytest tests/test_browser.py` (Playwright): 39 passed
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, `uv run pyrefly check src tests`: all clean

## Deviations from Plan

### TDD gate note

**1. [RED expectation] Pass-B worker tests passed during RED**
- **Found during:** Task 1
- **Issue:** The plan expected the new observability tests to fail at RED. They passed because plan 25-02 had already made `SCANNING_REVERSE` a persisted state (`test_worker_status_cb_dispatches_on_pipeline_event` already asserted it). I checked this under the fail-fast rule: the tests do test what they claim, and the behaviour predates this plan.
- **Outcome:** RED still failed where this plan changes behaviour: both route-lookup tests in `tests/test_web.py` rendered "Ready to scan.". They now guard the SCANNING_REVERSE persistence and the D-16 outcome through the real pipeline, replacing the deleted tests that only pinned the defect.

### Additions

**2. [Rule 2 - Coverage] Added a `legacy-duplex-source` e2e case**
- **Found during:** Task 3
- **Issue:** Moving the only duplex e2e case to the two-key form would have removed the one end-to-end proof of behaviour bullet 3 ("the existing legacy-source e2e case still scans").
- **Fix:** Added a separate case with `source = "ADF Manual Duplex"` and no `duplex` key. The constant is named `_LEGACY_DUPLEX_SOURCE`, and the acceptance grep for `_DUPLEX_SOURCE = "Manual Duplex"` still returns nothing.
- **Commit:** de51fef

**3. [Rule 1 - Test fidelity] `operator_flips` field on `_Case`**
- **Found during:** Task 3
- **Issue:** With `flip_timeout=0` the job leaves AWAITING_FLIP in microseconds, so waiting on that state would be flaky, and calling Continue would defeat the case.
- **Fix:** Added `operator_flips: bool = True`. The test waits and signals only when both `awaits_flip` and `operator_flips` are true.
- **Commit:** de51fef

No files outside this plan's `files_modified` were touched, and none of plan 25-06's files were edited.

## TDD Gate Compliance

- RED: `1afda50 test(25-05): ...`
- GREEN: `c648f9c feat(25-05): ...` (after RED)
- Task 3: `de51fef test(25-05): ...` (a proof test on already-implemented 25-03 behaviour; passed on first run as expected)

## Threat Flags

None. The only new surface is removal: one blocking wait is gone and two routes now read the same lookup as the status poll. T-25-23 (cross-site POST) is still accepted and deferred to ROBU-10.

## Known Stubs

None.

## Self-Check: PASSED

- FOUND: src/saneless/worker.py, src/saneless/web/routes.py, tests/test_worker.py, tests/test_web.py, tests/test_outcomes_e2e.py
- FOUND commits: 1afda50, c648f9c, de51fef
