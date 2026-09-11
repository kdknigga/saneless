---
phase: 23-honest-outcomes-and-never-lose-a-scan
plan: 07
subsystem: database
tags: [sqlite, job-store, worker, scan-outcome, assert-never, tdd, threading]

# Dependency graph
requires:
  - phase: 21-vocabulary-and-contracts
    provides: ScanOutcome, the match/assert_never enforcement finding (D-08), the parametrised list(JobState) completeness tests
  - phase: 22-job-store-hardening
    provides: the six result columns with no writer, the @_locked decorator, TestLockDiscipline's reflective and AST tests, NULL-means-never-recorded (D-08)
  - phase: 23-honest-outcomes-and-never-lose-a-scan
    provides: "plan 23-01's JobState.FALLBACK and vocabulary.job_state_for; plan 23-03's PipelineRequest.job_id and build_pdf_filename; plan 23-06's raising, preserving run_pipeline that returns a real ScanResult on both the simplex and duplex paths"
provides:
  - "JobResult: a frozen dataclass carrying the five result facts as one argument"
  - "JobStore.finish_job: the @_locked terminal write, eight columns in one UPDATE"
  - "The worker consumes the pipeline's ScanResult instead of discarding it"
  - "A FALLBACK delivery persists JobState.FALLBACK, its outcome, its warning and its three page counts"
  - "A failure persists ERROR with message and category and leaves outcome, warning and all three counts NULL"
  - "PipelineRequest carries job.id in production, so the assembled PDF name is collision-proof"
  - "A wait_for_state pytest fixture, making plan 23-01's conftest helper reachable under pytest 9"

affects: [23-08, ui-rendering-of-fallback, phase-24, phase-30]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Terminal writes are a separate JobStore method from in-flight writes, because an unconditional SET cannot serve both"
    - "A value object parameter bundles related arguments to stay under ruff PLR0913 rather than raising the limit"
    - "A terminal state is derived from an outcome through a total match, never chosen by an inline if/else"

key-files:
  created: []
  modified:
    - src/saneless/job.py
    - src/saneless/worker.py
    - tests/test_job.py
    - tests/test_worker.py
    - tests/conftest.py

key-decisions:
  - "finish_job takes a JobResult value object rather than five loose parameters, because create_job proves ruff's PLR0913 ceiling is five non-self parameters and CLAUDE.md forbids raising or suppressing it"
  - "JobResult mirrors pipeline.ScanResult's fields without importing it; taking a ScanResult directly would make job.py depend on pipeline.py and invert the layering"
  - "update_state is byte-identical to its pre-plan form; its unconditional SET would blank the result columns on every in-flight transition"
  - "The failure path passes no result at all, so the three page counts stay NULL rather than 0"
  - "TestWorkerEnumDispatch's terminal-write assertion was inverted: DONE must now never reach update_state, and must arrive exactly once through finish_job"
  - "wait_for_state is exposed as a pytest fixture because pytest 9's importlib import mode keeps tests/ off sys.path"

patterns-established:
  - "Separate terminal write: in-flight state changes and terminal result writes are different methods on the store, so neither can blank the other's columns"
  - "Argument bundling over limit-raising: a frozen dataclass parameter is the answer to PLR0913, not a per-file ignore"
  - "conftest helpers reach test modules as fixtures, never as `from conftest import ...`"

requirements-completed: [OUTC-01, OUTC-02, OUTC-03]

# Metrics
duration: 22min
completed: 2026-09-11
---

# Phase 23 Plan 07: The Worker Consumes the Answer Summary

**`JobStore.finish_job` writes state, outcome, warning and three page counts in one locked UPDATE, and the worker now derives its terminal state from the `ScanResult` it used to throw away — so a consume-directory fallback is recorded as FALLBACK rather than silently as DONE.**

## Performance

- **Duration:** 22 min
- **Started:** 2026-09-11T16:11:00Z
- **Completed:** 2026-09-11T16:33:04Z
- **Tasks:** 2
- **Files modified:** 5

## Accomplishments

- **C-03 is closed.** `worker.py` bound `run_pipeline`'s return value, and the outcome, the
  warning and the three page counts now reach the database. Before this commit every fact the
  pipeline computed was discarded and `JobState.DONE` was written unconditionally.
- **`JobStore.finish_job`** is the first and only writer of the five result columns Phase 22
  created and deliberately left unwired. It carries `@_locked`, opens its own `with self._conn:`,
  calls no other public method, and writes eight columns in a single `UPDATE` so the web request
  thread can never observe a job that has finished but has not yet recorded how.
- **`update_state` is untouched** — verified by extracting its AST source segment from
  `git show HEAD:src/saneless/job.py` and comparing byte-for-byte. D-04 is not style: its SQL is an
  unconditional `SET`, so six more columns there would blank the results on every in-flight
  SCANNING / ASSEMBLING / UPLOADING transition.
- **The terminal state is derived, not assumed.** `job_state_for(result.outcome)` is a `match`
  with `assert_never`, so a future third `ScanOutcome` member fails the type gate at edit time
  rather than falling silently into an `else` (D-02).
- **OUTC-05's remaining half is closed.** `PipelineRequest(job_id=job.id)` means production PDFs
  are named from the job id, not from timestamp-plus-title alone — the loose end 23-03's summary
  handed to this plan.
- **The failure path records nothing it did not measure.** No `result` argument is passed, so
  `outcome`, `warning` and all three counts stay NULL. Phase 22's D-08: NULL means "never
  recorded", `0` means "counted, and there were none".

## Task Commits

Each task was committed atomically:

1. **Task 1: JobResult and a @_locked finish_job** — `c623774` (feat)
2. **Task 2: The worker consumes the ScanResult** — `c981215` (feat)

### Why there is no standalone RED commit

Both tasks were driven tests-first and the red output was observed before any implementation, but
the red state could not be committed on its own. `prek` runs `ty` and `pyrefly` on every commit and
both reject a test file naming symbols that do not exist yet; `--no-verify`, `# type: ignore` and
`SKIP=` are all forbidden by CLAUDE.md or blocked by the worktree guard. The observed red output is
recorded verbatim in each commit message instead — this is the same constraint every prior executor
in this phase hit, and the same resolution.

Observed red, Task 1:

```
tests/test_job.py:23: in <module>
    from saneless.job import ErrorCategory, Job, JobResult, JobState, JobStore
E   ImportError: cannot import name 'JobResult' from 'saneless.job'
```

Observed red, Task 2 (5 failed, 2 passed):

```
assert captured[0].job_id == job.id
E   AssertionError: assert '' == 'bc1fc3ac-0c5a-4f5b-854d-4f9f717bb5fa'
assert finished.outcome is ScanOutcome.SUCCESS
E   AssertionError: assert None is <ScanOutcome.SUCCESS: 'SUCCESS'>
assert finished.state is JobState.FALLBACK
E   AssertionError: assert <JobState.DONE: 'DONE'> is <JobState.FALLBACK: 'FALLBACK'>
```

The two that passed at red were the error-path test and the prune test — both are regression guards
for behaviour the old `update_state` path already had and the new `finish_job` path must not lose.

## Files Created/Modified

- `src/saneless/job.py` — adds the frozen `JobResult` dataclass and the `@_locked finish_job`;
  rewrites the `create_job` comment that claimed the result columns had no writer. `update_state`
  is byte-identical.
- `src/saneless/worker.py` — `PipelineRequest(job_id=job.id)`, the bound `result`, the
  `job_state_for`-derived terminal write, and an error path that passes no result.
- `tests/test_job.py` — `TestFinishJob` (10 tests) plus a `_finish_stress_worker` helper.
- `tests/test_worker.py` — `TestWorkerFinish` (7 tests); `TestWorkerEnumDispatch`'s terminal-write
  assertion inverted.
- `tests/conftest.py` — a `wait_for_state` fixture wrapping the existing module-level helper.

## Decisions Made

**1. `JobResult` bundles the five result facts; the two alternatives RESEARCH offered are rejected.**
`finish_job(job_id, state, result, error, error_category)` is exactly five non-self parameters,
which is ruff `PLR0913`'s ceiling — `create_job` sits at the same five and passes, which is the
evidence. Spelling the five result facts out individually would make nine.
- *Rejected: take a `ScanResult` directly.* `job.py` would then import `pipeline.py`, inverting the
  dependency direction. The pipeline is the layer that knows about persistence, not the reverse.
  The worker does the five-field copy at its single call site instead.
- *Rejected: raise the `PLR0913` limit.* CLAUDE.md forbids disabling rules, and the limit is doing
  its job here — nine parameters would be a genuine smell.

**2. `finish_job` is one `UPDATE`, not two statements.** The worker thread writes this row while the
web request thread reads it. A two-statement version would let the reader observe a job that had
finished but had not yet recorded its outcome. A test asserts exactly one `self._conn.execute` and
one `with self._conn:` in the method's source.

**3. The `warning` column is written on the SUCCESS path too.** A duplex page-count mismatch resolves
to `ScanOutcome.SUCCESS` with a non-None warning — outcome and warning are independent, and
`TestFinishJob` and `TestWorkerFinish` each carry a case proving both survive together (OUTC-03).

**4. `TestWorkerEnumDispatch`'s final assertion was inverted rather than deleted.** It asserted
`states_seen[-1] is JobState.DONE`, tracking `update_state` calls — now false, and it must not
become true again. It now asserts `JobState.DONE not in states_seen` and
`finished_states == [JobState.DONE]`, tracking both methods. That converts a test that would have
been silently weakened into one that actively enforces D-04 at the call site.

**5. `_status_cb` was left completely alone**, as the plan required. Its
`if state not in ACTIVE_STATES: return` guard covers the newly-terminal FALLBACK for free.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] `wait_for_state` was unreachable from any test module**

- **Found during:** Task 2 (worker tests)
- **Issue:** The plan required using the `wait_for_state` helper plan 23-01 added to
  `tests/conftest.py`. `from conftest import wait_for_state` raises
  `ModuleNotFoundError: No module named 'conftest'`. Measured, not assumed: a throwaway probe test
  was written, run, and the failure observed, then deleted. The cause is pytest 9.0's default
  `--import-mode=importlib`, under which `tests/` never lands on `sys.path`. The helper 23-01 added
  had had no caller at all, so nothing had exercised the import path before now.
- **Fix:** Added a `@pytest.fixture(name="wait_for_state")` to `tests/conftest.py` returning the
  existing function uncalled. The module-level function is unchanged; the fixture is the supported
  route for a conftest helper and is the only one that also satisfies `ty` and `pyrefly`. Five of
  the seven new worker tests take it.
- **Files modified:** `tests/conftest.py`, `tests/test_worker.py`
- **Verification:** `uv run pytest tests/test_worker.py -k finish -q` → 7 passed; full suite 741
  passed; `ty` and `pyrefly` clean.
- **Committed in:** `c981215` (Task 2 commit)

**2. [Rule 1 - Bug] An always-true identity comparison in a new test**

- **Found during:** Task 2 (verification)
- **Issue:** `assert finished.state is not JobState.DONE`, written immediately after
  `assert finished.state is JobState.FALLBACK`, drew a new `pyrefly` warning:
  *"Identity comparison `JobState.FALLBACK is not JobState.DONE` is always True
  [unnecessary-comparison]"*. `pyrefly` narrows the state after the first assertion, so the second
  proves nothing. It was caught only by comparing hidden-warning counts against the pre-plan
  baseline — `uv run pyrefly check src tests` reports `0 errors (N warnings not shown)`, and N had
  gone from 1 to 2.
- **Fix:** Removed the redundant assertion; the surviving `is JobState.FALLBACK` already carries the
  full claim, and a comment records why the "not DONE" half cannot be spelled out separately.
- **Files modified:** `tests/test_worker.py`
- **Verification:** `uv run pyrefly check src tests` back to the baseline `0 errors (1 warning)`.
- **Committed in:** `c981215` (Task 2 commit)

---

**Total deviations:** 2 auto-fixed (1 blocking, 1 bug)
**Impact on plan:** Neither changes the plan's shape. The first makes a wave-1 helper usable for the
first time; the second removes an assertion that asserted nothing. No scope creep.

### Note on one acceptance criterion's literal wording

The plan required `grep -c 'job_state_for' src/saneless/worker.py` to return 2 (import plus use).
The first draft returned 3, because an explanatory comment also named the function. The comment was
reworded to say "the mapping below" rather than repeating the identifier, so the count is 2 as
specified and the comment still explains why an inline `if/else` is wrong. No behaviour change.

`grep -c 'JobState.DONE' src/saneless/worker.py` returns 0 — the constant does not survive anywhere
in the module, not merely outside the terminal write.

## Issues Encountered

**The three `tracking_update` helpers in `tests/test_worker.py` are byte-identical.** A
text-anchored edit to `TestWorkerEnumDispatch`'s helper matched all three. Resolved by partitioning
the file at `class TestWorkerEnumDispatch:` and editing only the tail, rather than by inventing a
longer and more brittle anchor.

**`uv run pyrefly check` with no path argument silently checks nothing inside a worktree.** Every
verification run used `uv run pyrefly check src tests` explicitly, per the phase's standing note.
This is also what made deviation 2 findable: the hidden-warning count is only meaningful when the
same file set is checked each time.

## Verification

| Check | Result |
|-------|--------|
| `uv run pytest -q` | 741 passed (was 724 at plan start; +17) |
| `uv run pytest tests/test_job.py -k finish_job -q` | 10 passed (criterion: ≥ 6) |
| `uv run pytest tests/test_worker.py -k finish -q` | 7 passed (criterion: ≥ 5) |
| `uv run ruff check .` | No issues found |
| `uv run ruff format --check .` | 40 files already formatted |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors, 1 warning (baseline) |
| `finish_job` carries `_LOCKED_MARKER` | True |
| `grep -c 'UPDATE jobs SET' src/saneless/job.py` | 3 (was 2 — exactly one added) |
| `update_state` source segment vs. `HEAD` | IDENTICAL |
| `grep -c 'written by nothing' src/saneless/job.py` | 0 |
| `grep -c 'job_state_for' src/saneless/worker.py` | 2 |
| `grep -c 'finish_job' src/saneless/worker.py` | 2 |
| `grep -c 'job_id=job.id' src/saneless/worker.py` | 1 |
| `grep -c 'JobState.DONE' src/saneless/worker.py` | 0 |
| `# type: ignore` / `# noqa` / rule disabled in diff | none |

## Known Stubs

None. `PipelineRequest.job_id`'s empty-string default remains from plan 23-03, but it is now
supplied by the only production caller; the default exists solely so the dozens of tests that build
a request from a profile name and a title need not invent one.

## Threat Flags

None. This plan adds no network endpoint, no auth path, no file access pattern and no schema change.
The one new trust-boundary write, `str(exc)` into `job.error`, persists text the exception already
carried; its upstream is bounded by plan 23-04's 500-character cap and its rendering is escaped,
proven by plan 23-05's XSS regression test.

## Self-Check: PASSED

- `src/saneless/job.py` — FOUND, contains `def finish_job` and `class JobResult`
- `src/saneless/worker.py` — FOUND, contains `finish_job` and `job_id=job.id`
- `tests/test_job.py` — FOUND, contains `class TestFinishJob`
- `tests/test_worker.py` — FOUND, contains `class TestWorkerFinish`
- `tests/conftest.py` — FOUND, contains the `wait_for_state` fixture
- Commit `c623774` — FOUND in `git log`
- Commit `c981215` — FOUND in `git log`

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

The phase's behaviour is now complete end to end in the persistence layer: a FALLBACK delivery is
persisted as FALLBACK with its warning, a Paperless failure raises, preserves the PDF and is recorded
as ERROR with the Paperless message, and a duplex mismatch carries its warning and its three counts.

Ready for **plan 23-08**, which proves all of that against the real pipeline rather than a
monkeypatched `run_pipeline`. Two notes for that plan:

- `TestWorkerFinish` deliberately stubs `saneless.worker.run_pipeline`. It tests the worker's
  *handling* of a result; driving the real pipeline is 23-08's job, and 23-08 should not read these
  tests as already covering that.
- The `wait_for_state` fixture is now available to every test module and should be preferred over
  `time.sleep(0.5)` in the end-to-end test.

STATE.md and ROADMAP.md were deliberately left untouched — the orchestrator owns those writes after
the wave completes.

---
*Phase: 23-honest-outcomes-and-never-lose-a-scan*
*Completed: 2026-09-11*
