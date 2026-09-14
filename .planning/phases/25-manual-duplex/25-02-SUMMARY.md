---
phase: 25-manual-duplex
plan: 02
subsystem: vocabulary
tags: [vocabulary, jobstate, duplex, pipeline, tdd]
requires: [25-01]
provides:
  - "JobState.SCANNING_REVERSE, the ninth member (in ACTIVE_STATES, so also in BUSY_STATES)"
  - "state_label(JobState.SCANNING_REVERSE) == 'Scanning backs'"
  - "progress_label(JobState.SCANNING_REVERSE) == 'Scanning reverse sides...'"
  - "PipelineEvent.job_state -> JobState (no longer Optional)"
affects: [25-03, 25-05, 25-09]
tech-stack:
  added: []
  patterns:
    - "A new enum member lands with all of its match arms in one commit, because the commit-stage ty/pyrefly checks enforce exhaustiveness"
key-files:
  created: []
  modified:
    - src/saneless/vocabulary.py
    - src/saneless/pipeline.py
    - src/saneless/cli.py
    - src/saneless/worker.py
    - tests/test_vocabulary.py
    - tests/test_pipeline.py
    - tests/test_worker.py
decisions:
  - "The new parametrize rows use JobState.SCANNING_REVERSE directly, like every other row, even though that made the RED failure in test_vocabulary.py a collection-time AttributeError"
  - "worker._status_cb sends SCANNING_REVERSE through the general busy-state path, which persists the state and still sets _transition_event, so the pass boundary can still be observed until plan 25-05 deletes the event"
metrics:
  duration: "~15 min"
  completed: 2026-09-14
  tasks: 2
  files: 7
---

# Phase 25 Plan 02: The SCANNING_REVERSE Job State Summary

`JobState.SCANNING_REVERSE` is now the ninth lifecycle state. It is in `ACTIVE_STATES`, and because
`BUSY_STATES` is derived from that set, it counts as busy too. Its labels are "Scanning backs" and
"Scanning reverse sides...". `PipelineEvent.job_state` now returns a `JobState` for every event, so
pass B is saved as its own state and the job leaves `AWAITING_FLIP` as soon as the backs start
feeding. The flip prompt and its Continue and Abort buttons now disappear during pass B.

## Commits

| Gate | Task | Commit | Message |
|------|------|--------|---------|
| RED | 1 | `223548b` | test(25-02): add failing tests for the SCANNING_REVERSE job state |
| GREEN | 2 | `8cec4cb` | feat(25-02): add the SCANNING_REVERSE job state and make the event projection total |

No REFACTOR commit was needed.

## What was built

- `vocabulary.py`: the `SCANNING_REVERSE` member, placed between `AWAITING_FLIP` and `ASSEMBLING`.
  It is added to `ACTIVE_STATES` and has an arm in both `state_label` and `progress_label`.
  `BUSY_STATES` was not edited. The `progress_label` docstring now says "six in-flight strings" and
  explains that the new text is exactly the line the CLI already printed.
- `pipeline.py`: `job_state` is annotated `-> JobState`. The comment that reserved a `None` result
  is gone, and the docstring now describes the total mapping and what it fixes (DPLX-06).
- `cli.py`: the `elif state is None:` branch and its comment are deleted. The general branch prints
  `progress_label(JobState.SCANNING_REVERSE)`, which is exactly the old text, so CLI output does not
  change (D-13).
- `worker.py`: the `if state is None:` early return is deleted. SCANNING_REVERSE now takes the
  general busy-state path, which saves the state and still calls `_transition_event.set()`.

## Verification

- `uv run pytest -q`: 994 passed. That is 982 after 25-01, plus the 12 cases the new rows add to
  existing parametrised tests.
- `uv run pytest tests/test_web_state_rendering.py -q`: 60 passed. **`tests/test_web_state_rendering.py`
  was not modified.** It builds its cases from `list(JobState)`, so it picked up the new state on
  its own. `test_status_area_prose` now shows there is no flip prompt during SCANNING_REVERSE (D-16).
- **`src/saneless/web/templates/partials/status.html` was not modified.** `git diff --name-only
  HEAD~2 HEAD` lists only the seven files above.
- `uv run ruff check .` and `uv run ruff format --check .` are clean. `uv run ty check` passes.
  `uv run pyrefly check src tests` reports 0 errors; its 4 warnings were already there.
- `grep -rn "state is None" src/saneless/cli.py src/saneless/worker.py` and
  `grep -rn "SCANNING_REVERSE is the only event" src/` both return nothing.
  `grep -c SCANNING_REVERSE src/saneless/vocabulary.py` returns 5.
- No migration was added. `jobs.state` is `TEXT NOT NULL` with no `CHECK` constraint.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] A worker test expected SCANNING_REVERSE to save nothing**
- **Found during:** Task 2 (GREEN full-suite run)
- **Issue:** `tests/test_worker.py::TestWorkerEnumDispatch::test_worker_status_cb_dispatches_on_pipeline_event`
  expected the callback's state writes to be `[AWAITING_FLIP, ASSEMBLING, UPLOADING]`, and its comment
  said "SCANNING_REVERSE (no JobState) ... contribute nothing". That is the defect DPLX-06 removes.
  The plan's file list did not include this test.
- **Fix:** Added `JobState.SCANNING_REVERSE` to the expected list after `AWAITING_FLIP` and rewrote
  the comment. It went into the GREEN commit so that commit passes the whole suite.
- **Files modified:** tests/test_worker.py
- **Commit:** `8cec4cb`

### Notes

- **The RED failure in `test_vocabulary.py` happened at collection time.** The plan asked for
  `(JobState.SCANNING_REVERSE, ...)` rows in the `state_label` and `progress_label` parametrize
  lists. Those values are evaluated when the class is defined, so before GREEN the module stopped at
  collection with `AttributeError: type object 'JobState' has no attribute 'SCANNING_REVERSE'`. That
  is the missing feature, not a syntax or import problem. The only way to get test-time failures
  would have been to change the rows to string lookups for good, and I kept the plan's direct member
  rows instead. In `tests/test_pipeline.py` the 3 affected tests failed at test time as intended.
- As in 25-01, commits were made with `/usr/bin/git`. All hooks ran, and none were skipped.
- `docs/explanation/architecture.md:33` still lists the six `PipelineEvent` values. That is still
  true. The docs changes for the ninth `JobState`, such as the `cli-scripting.md` list, belong to the
  phase's docs plan and were not touched here.

## Known Stubs

None.

## Self-Check: PASSED

- FOUND: src/saneless/vocabulary.py, src/saneless/pipeline.py, src/saneless/cli.py, src/saneless/worker.py
- FOUND: 223548b (RED), 8cec4cb (GREEN)
