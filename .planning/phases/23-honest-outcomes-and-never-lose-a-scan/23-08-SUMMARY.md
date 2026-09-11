---
phase: 23-honest-outcomes-and-never-lose-a-scan
plan: 08
subsystem: testing
tags: [end-to-end, httpx-mocktransport, parametrize, threading, sqlite, mutation-probe]

# Dependency graph
requires:
  - phase: 23-honest-outcomes-and-never-lose-a-scan
    plan: 04
    provides: "poll_task raising PaperlessError on FAILURE/REVOKED and PaperlessTimeoutError on the monotonic deadline; the v9/v10 both-shape task parsing this module drives from the outside"
  - phase: 23-honest-outcomes-and-never-lose-a-scan
    plan: 06
    provides: "_preserving, the guard spanning upload_document and poll_task that puts the finished PDF in <data_dir>/failed/ before the exception continues"
  - phase: 23-honest-outcomes-and-never-lose-a-scan
    plan: 07
    provides: "JobStore.finish_job, job_state_for, the worker consuming the pipeline's ScanResult, and the wait_for_state pytest fixture"
provides:
  - "tests/test_outcomes_e2e.py: the only test in the repository running the real run_pipeline inside the real ScanWorker"
  - "Five parametrised outcomes asserted from the persisted job row: SUCCESS, Paperless FAILURE, TIMEOUT, consume-directory fallback, duplex mismatch"
  - "A v10-shaped paginated /api/tasks/ response exercised at the integration layer, not only the unit layer"
  - "run_pipeline recording a warning on the simplex consume-directory fallback, closing OUTC-02's second half"

affects: [23-09, phase-24, phase-30, phase-32]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Frozen case dataclass per parametrised scenario, with the label doubling as the pytest id"
    - "Handler factories rather than handlers, so per-case request counters start fresh on every run"
    - "A transport that answers unexpected paths with a self-naming 418, so a mis-dispatch fails loudly instead of drifting into a timeout"
    - "Mutation probes as the evidence that an end-to-end test is not vacuous"
    - "Config-typed test levers: an int-typed setting is driven with an int, never with a runtime type lie"

key-files:
  created:
    - tests/test_outcomes_e2e.py
  modified:
    - src/saneless/pipeline.py
    - tests/test_pipeline.py

key-decisions:
  - "The fallback warning was missing from production and was added here (Rule 2): OUTC-02 requires FALLBACK 'with a warning', plan 23-05 shipped the UI that renders one, and plan 23-09 is told to read the shipped string -- there was none"
  - "paperless_task_timeout is 0 for the timeout case, not the plan's 0.05: the field is int-typed, pydantic rejects a fractional float, and assigning one anyway would be a type lie ty and pyrefly are right to reject"
  - "Settings requires a profile named 'default', and exactly one untouched 'default' is what is_bare_default matches; both a 'default' and the 'e2e' profile the scans run under are defined, so auto-generation never fires"
  - "Handler factories, not handlers, because the duplex case needs a counter that issues one task id per upload and refuses to recognise one it never issued"
  - "Assertions read store.get_job, never a mock call list, because the row is what the web UI and saneless jobs render"

patterns-established:
  - "Prove an integration test discriminates by mutating production and watching the right cases fail, then reverting"
  - "Docstring prose is reworded so acceptance greps measure code rather than commentary"

requirements-completed: [OUTC-10, OUTC-02]

# Metrics
duration: 15min
completed: 2026-09-11
---

# Phase 23 Plan 08: The Five-Outcome End-to-End Proof Summary

**One parametrised module drives the real `ScanWorker` and the real `run_pipeline` through SUCCESS, Paperless FAILURE, TIMEOUT, consume-directory fallback and duplex mismatch, asserting the persisted row and the real filesystem for each in 0.17 seconds — and it found the one production gap the phase still had.**

## Performance

- **Duration:** 15 min
- **Started:** 2026-09-11T11:37:00Z
- **Completed:** 2026-09-11T11:51:40Z
- **Tasks:** 1 (plus one Rule 2 production fix)
- **Files created:** 1
- **Files modified:** 2

## Accomplishments

- **`tests/test_outcomes_e2e.py` is the only test in the repository that runs the real
  `run_pipeline` inside the real `ScanWorker`.** All 49 `ScanWorker(...)` construction sites in
  `tests/test_worker.py` replace the pipeline entry point in the worker's module namespace, which
  makes them tests of the worker's *handling* of a result. This module replaces nothing there, and
  an acceptance grep for the patch target's dotted name returns 0.
- **Exactly two things are stubbed.** The scanner, because CI has no SANE device, and the HTTP
  layer through the existing `_transport` kwarg. `httpx.MockTransport` sits *below* `httpx.Client`,
  so the real `upload_document` retry loop and the real `poll_task` deadline loop execute. Real PDF
  assembly, real empty-page filtering, real preservation into `<data_dir>/failed/`, a real
  file-backed SQLite store and a real background thread are all in the picture.
- **Every assertion reads the persisted row** through `store.get_job`, never a mock's call list
  (T-23-38). State, outcome, all three page counts, warning and error come out of the database; the
  preserved and delivered files come off a real filesystem.
- **The v10 wire shape is exercised end to end**, not only at the unit level: the failure case
  answers with a paginated `{"count","next","previous","results":[…]}` body, a lowercase
  `"failure"`, and the message under `result_data["error_message"]`.
- **It found the gap.** The simplex consume-directory fallback produced no `warning`, so OUTC-02's
  "recorded in the `FALLBACK` state **with a warning**" was only half true. Fixed here — see
  Deviations.
- **0.17 s for the whole module, 0.04 s for the slowest case**, with no patched sleep primitive and
  no flat pause anywhere in the file.
- **Two mutation probes prove the cases discriminate.** Not a claim — both were run.

## Task Commits

| Task | Name | Commit | Files |
|------|------|--------|-------|
| Rule 2 fix | Record a warning when a scan falls back to the consume directory | `e418414` | `src/saneless/pipeline.py`, `tests/test_pipeline.py` |
| 1 | The parametrised five-outcome end-to-end test | `5d43cba` | `tests/test_outcomes_e2e.py` |

The Rule 2 fix is committed first and separately because the end-to-end fallback case asserts the
warning it produces; landing them together would have hidden a production change inside a test
commit.

## The five cases

| Case | State | Outcome | Pages (scanned, removed, uploaded) | Wire shape | Files |
|------|-------|---------|------------------------------------|-----------|-------|
| `success` | `DONE` | `SUCCESS` | (2, 0, 2) | v9 bare list, uppercase | `failed/` empty |
| `paperless-failure` | `ERROR` | `None` | (None, None, None) | **v10** paginated, lowercase, `result_data` | 1 PDF in `failed/`, named in `job.error` |
| `poll-timeout` | `ERROR` | `None` | (None, None, None) | v10 empty page, forever | 1 PDF in `failed/`, named in `job.error` |
| `consume-dir-fallback` | `FALLBACK` | `FALLBACK` | (2, 0, 2) | 500 on every upload attempt | 1 PDF in `consume/`, no dotfile, `failed/` empty |
| `duplex-mismatch` | `DONE` | `SUCCESS` | (5, 0, 5) | v9, two uploads two polls | `failed/` empty |

The two failure cases assert `outcome is None` and all three counts `None` — D-08's
NULL-means-never-recorded, not `0` claiming a measurement nobody made.

## Structure, and why

**One `@pytest.mark.parametrize` over five frozen `_Case` records, and a single test method.** A
record beats a tuple because five heterogeneous cases with fourteen fields each are unreadable
positionally, and `label` doubles as the pytest id, so a failure names itself. The method was not
forked: the one case-specific step — releasing the manual-duplex flip — is an `awaits_flip` field,
and the two assertion groups are module-level helpers taking the case.

**Handler *factories*, not handlers.** `_accepting_handler()` closes over a list of issued task ids:
each upload gets its own id, and a poll for an id the handler never issued is answered by
`_unexpected`. That is what stops the duplex case passing by polling one task twice or by polling a
task that was never created. A factory also guarantees no state leaks between parametrised runs.

**`_unexpected` answers 418 with the offending method and path.** A silently-tolerated stray request
is how an end-to-end test stops being one. A non-200 turns a mis-dispatch into a `PaperlessError`
quoting the path, instead of letting the run drift into a timeout whose message says nothing about
the real mistake.

**The flip is released from the main thread after observing persisted `AWAITING_FLIP`**, not from
inside the scanner stub. `worker.continue_flip()` is a no-op if it arrives before `_process_job` has
created the event, so waiting on the state first removes the race — and it drives the real flip
coordination rather than side-stepping it.

## Speed: the decided recipe, measured

| Lever | Effect |
|-------|--------|
| `max_retries=1` on `PaperlessClient` | Both exponential-backoff pauses in `upload_document` become unreachable. The fallback case costs 0.00 s instead of 3.00 s. |
| `paperless_task_timeout=0` for the timeout case | The monotonic deadline has already passed when the first poll returns without a terminal status. 0.00 s. |
| The other four cases | Reach a terminal status, or fall back, on the first request, before any backoff. |

Measured: **0.17 s for the module**, slowest case 0.04 s. The acceptance criterion was "no single
test above 1 s".

All three rejected alternatives stayed rejected: the sleep primitive is not patched (that would
disable the `min(delay, remaining)` deadline clamp these cases follow into the preservation guard,
and Phase 32 / M-34 owns that sweep), no `sleep_fn` parameter was injected, and the backoff base was
not made configurable. **No new production seam exists anywhere in this plan.**

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 — Missing critical functionality] A consume-directory fallback recorded no warning**

- **Found during:** Task 1, writing the fallback case
- **Issue:** `run_pipeline`'s simplex fallback path built its `ScanResult` without a `warning`
  argument, so the column stayed NULL. **OUTC-02** requires the job to be "recorded in the
  `FALLBACK` state **with a warning**", and D-05 specifies the content: the warning carries the
  detail that title, tags and correspondent were not applied. Plan 23-05 shipped the guarded amber
  `<p class="status-fallback">{{ job.warning }}</p>` that renders it; plan 23-07 wired the worker to
  persist whatever the pipeline returns. Nothing ever produced one. The corroborating evidence that
  this is a gap rather than a plan error: **plan 23-09's task (b) instructs the docs executor to
  keep its wording "consistent with whatever plan 23-07's `warning` string actually says. Read the
  shipped string rather than inventing one."** There was no shipped string to read.
  This plan's own `<behavior>` block asserts `warning is not None` for this case.
- **Fix:** A `_consume_dir_warning(destination)` helper in `pipeline.py`, called only on the
  `task_uuid is None` branch. Its wording follows D-05 and matches the consequence already
  documented at `docs/explanation/consume-directory-fallback.md:62` — the title, tags and
  correspondent chosen for the scan are not applied, and paperless-ngx uses its own matching rules
  instead. Register is deliberately warning, not error: nothing was lost and no rescan is needed.
- **Also changed:** `tests/test_pipeline.py`'s `test_fallback_outcome_when_not_delivered_to_api`
  asserted `result.warning is None`, encoding the incomplete behaviour. Its assertion was replaced
  with one that checks the warning exists, names the destination path, and names what was not
  applied.
- **Not changed:** the duplex-mismatch fallback path, which already returns a non-None warning
  describing the mismatch. OUTC-02's "with a warning" is satisfied there.
- **Verification:** red observed first
  (`assert result.warning is not None` → `AssertionError: assert None is not None`), then green;
  full suite 741 → 746 passed; ruff, ruff format, ty and `pyrefly check src tests` all clean.
- **Commit:** `e418414`

**2. [Rule 3 — Blocking] `Settings` rejected a profile set without a `"default"` entry**

- **Found during:** Task 1, first run of the new module
- **Issue:** The module deliberately avoids naming its profile `"default"`, because
  `auto_profiles.is_bare_default` matches a profile set of *exactly one untouched profile called
  "default"* — and on that match the worker generates profiles from the stub scanner and writes them
  to whatever `saneless.toml` `resolve_config_path()` finds on the machine running the suite. But
  `Settings.validate_default_profile` requires `"default"` to exist:
  `ValidationError: Value error, A 'default' profile must be defined in config`.
- **Fix:** Define both. A bare `"default"` satisfies the validator and the `"e2e"` profile the scans
  actually run under makes the set two entries long, so `is_bare_default` returns on its
  `len(...) != 1` check and `_maybe_auto_generate` returns on its first line. A comment records the
  pincer, because either constraint alone would suggest the opposite fix.
- **Files modified:** `tests/test_outcomes_e2e.py`
- **Commit:** `5d43cba`

### Deliberate departures from the plan's letter

**3. The timeout budget is `0`, not `0.05`.** `OutputConfig.paperless_task_timeout` is typed `int`.
pydantic rejects `0.05` outright in lax mode (a float with a fractional part is not an int), and
assigning it after construction would be a runtime type lie that `ty` and `pyrefly` are right to
reject — CLAUDE.md forbids suppressing either. Widening the production field to `float` purely for a
test would have been a production change in a test-only plan, for no user-visible benefit. Zero
costs *less* wall clock than 0.05 s and proves the same property end to end: `poll_task` computes
`deadline = monotonic() + timeout`, issues its first poll, sees no terminal status, finds no time
remaining, and raises `PaperlessTimeoutError` — which is the raise this case exists to follow into
the preservation guard. What zero does *not* exercise is the backoff clamp itself, and that is
already covered by `tests/test_paperless.py`'s dedicated wall-clock test at the unit level, where it
belongs. The reasoning is recorded at the constant, not only here.

**4. Docstring prose reworded so the acceptance greps measure code.** Two criteria require
`grep -c 'time.sleep'` and `grep -c 'saneless.worker.run_pipeline'` to return 0. The first draft
returned 5 and 1 — every hit was commentary *explaining* that neither is used. The prose now says
"exponential-backoff pause", "the `min(delay, remaining)` clamp" and "the pipeline entry point in
the worker's own module namespace". Both counts are 0, and the module still explains why. Same
correction plan 23-06 made for the same reason.

**5. The plan offered a `conftest.py` extension for `wait_for_state` accepting a `frozenset`.** Not
needed — plan 23-01 already typed it `JobState | frozenset[JobState]` and plan 23-07 exposed it as a
fixture. `tests/conftest.py` is untouched. The fixture is used twice in this module, and no flat
pause was added; `tests/test_worker.py` keeps its 27 and this file adds none.

**6. RED and GREEN share a commit.** The documented constraint every executor in this phase has hit:
`prek` runs `ty` and `pyrefly` on every commit and both reject a test module naming behaviour that
does not exist yet, while `--no-verify`, `# type: ignore` and `SKIP=` are all forbidden. Red was
observed and is recorded verbatim in each commit message.

---

**Total deviations:** 2 auto-fixed (1 missing functionality, 1 blocking) + 4 recorded departures
**Impact on plan:** No scope creep. Every bullet in the `<behavior>` block has an assertion. The one
production change was required by a requirement the plan's own behaviour block asserts.

## Evidence the test is not vacuous

The plan's largest threat, **T-23-38 — "a test that passes without exercising the code under test"**
— was answered by mutation, not by assertion. Both probes were run and reverted:

| Mutation | Expected to break | Observed |
|----------|-------------------|----------|
| `worker.py` writes `JobState.DONE` unconditionally instead of `job_state_for(result.outcome)` | the fallback case | `consume-dir-fallback` FAILED: `<JobState.DONE> is not <JobState.FALLBACK>`; 4 passed |
| the `_preserving` guard removed from the simplex delivery path | both preservation cases | `paperless-failure` and `poll-timeout` both FAILED on `assert len(preserved) == case.expected_failed_pdfs`; 3 passed |

Each mutation broke exactly the cases that should care and left the others alone, which is the
stronger result — a test that fails on everything is as uninformative as one that fails on nothing.

Corroborating evidence from the captured logs during the fallback run, all of it from production
code paths: `saneless.paperless:308 Upload attempt 1/1 failed: Server error '500 …'`,
`paperless.py:322 Created consume directory …`, and
`paperless.py:325 All retries exhausted. Copied PDF to …/20260911-164913-149f2b51-quarterly-report.pdf`
— the real `build_pdf_filename` output, timestamp plus job id plus slug.

## Verification

| Check | Result |
|-------|--------|
| `uv run pytest tests/test_outcomes_e2e.py -q` | 5 passed (criterion: ≥ 5 collected) |
| `uv run pytest -q` | 746 passed (741 at plan start; +5) |
| `--durations`: slowest single test | 0.04 s (criterion: < 1 s) |
| Whole module wall clock | 0.17 s (criterion: < 3 s) |
| `uv run ruff check .` | No issues found |
| `uv run ruff format --check .` | 41 files already formatted |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors, 1 warning — the recorded baseline |
| `# type: ignore` / `# noqa` / disabled rule / `-W ignore` / raised timeout | none |

Acceptance greps on `tests/test_outcomes_e2e.py`:

| Pattern | Required | Actual |
|---------|----------|--------|
| `saneless.worker.run_pipeline` | 0 | 0 |
| `time.sleep` | 0 | 0 |
| `max_retries=1` | ≥ 1 | 2 |
| `wait_for_state` | ≥ 1 | 3 |
| `MockTransport` | ≥ 1 | 3 |
| `"results"` | ≥ 1 | 3 |

`uv run pyrefly check src tests` with explicit paths throughout, per the phase's standing note: a
bare `pyrefly check` inside a `.claude/worktrees/` worktree matches zero files and exits 0.

## Issues Encountered

- **`pyproject.toml` sets `filterwarnings = ["error"]`, and a worker thread that raised would
  surface as `PytestUnhandledThreadExceptionWarning` → an error.** None occurred: the worker's
  `except Exception` covers every failure path this module drives. Worth recording because that
  guard is what keeps the timeout and failure cases from becoming thread-exception noise.
- **Teardown order matters.** `worker.stop()` joins the thread *before* `store.close()`, so the
  worker's post-job `prune()` has finished writing when the connection closes. Everything is in a
  `finally`, so a case that fails mid-run still tears down.
- **`data_dir` is created before `JobStore` opens.** `sqlite3.connect` does not create parents and
  `OutputConfig.db_path` creates nothing.

## Known Stubs

None. The two stubs in the module are the scanner and the HTTP transport, both required (there is no
SANE device in CI and no paperless-ngx instance), both explicitly sanctioned by the plan, and both
documented as the complete list in the module docstring.

## Threat Flags

None. This plan adds no network endpoint, no auth path and no schema change. The one new
file-access pattern is a test writing under pytest's `tmp_path`, and every path in the module is
relative to it:

| Threat | Status |
|--------|--------|
| T-23-38 (a test that passes without exercising the code) | Mitigated, and *measured* — see the mutation table above |
| T-23-39 (a slow or hanging test in the CI gate) | Mitigated: 0.17 s total; the 2 s `wait_for_state` budget fails with a message naming the job and the state actually observed, long before pytest-timeout's 60 s SIGALRM would print a traceback for the wrong thread |
| T-23-40 (writing outside the fixture directory) | Mitigated: `tmp_dir`, `data_dir` and `consume_dir` are three separate subtrees of `tmp_path`, `failed_dir` derives from `data_dir` through the config property, and no absolute path outside `tmp_path` appears in the module |
| T-23-41 (traversal coverage only by construction) | Accepted as planned — the hostile-input matrix lives in plan 23-03's parametrised sanitiser tests |

The one production change (`_consume_dir_warning`) interpolates the consume-directory path into text
that reaches the job store and the web UI. That path is the operator's own configured directory, not
user input, it is the same class of value `job.error` already carries from the preservation guard
(T-23-30), and the template escapes it — proven by plan 23-05's XSS regression test.

## Verification Against Success Criteria

| Criterion | Result |
|-----------|--------|
| `tests/test_outcomes_e2e.py` drives the real worker and the real pipeline | PASS — and proven by mutation, not asserted |
| Five parametrised cases: SUCCESS, FAILURE, TIMEOUT, consume-dir fallback, duplex mismatch | PASS |
| Each asserts persisted state, outcome, page counts and file preservation from the database row | PASS |
| At least one case uses a v10-shaped paginated `/api/tasks/` response | PASS — `paperless-failure`, including lowercase status and `result_data["error_message"]` |
| No `time.sleep`, no monkeypatched `run_pipeline`, no new production seam | PASS (greps 0 and 0; no seam added) |
| The module runs in under three seconds | PASS — 0.17 s |
| No modifications to STATE.md or ROADMAP.md | PASS — untouched |

## Self-Check: PASSED

- `tests/test_outcomes_e2e.py` — FOUND, contains `MockTransport`, `ScanWorker`, `_transport=`
- `src/saneless/pipeline.py` — FOUND, contains `_consume_dir_warning`
- `tests/test_pipeline.py` — FOUND, fallback test asserts a non-None warning
- Commit `e418414` — FOUND in `git log`
- Commit `5d43cba` — FOUND in `git log`

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

OUTC-10 is satisfied and OUTC-02 is now satisfied in full rather than in half.

Two notes for **plan 23-09** (docs):

- **The shipped fallback warning string now exists**, in `pipeline._consume_dir_warning`. Task (b)
  told the docs executor to read it rather than invent one; it is there to read. Its wording was
  chosen to match `docs/explanation/consume-directory-fallback.md:62` already, so the two should
  need no reconciliation.
- **`docs/explanation/consume-directory-fallback.md:66`** — the paragraph claiming the job status
  does not distinguish the two paths — is now false in every clause, as task (a) anticipated. The
  end-to-end fallback case is the executable proof: `FALLBACK`, its own outcome, its own warning,
  and a file in the consume directory with no staging dotfile beside it.

STATE.md and ROADMAP.md were deliberately left untouched — the orchestrator owns those writes after
the wave completes.

---
*Phase: 23-honest-outcomes-and-never-lose-a-scan*
*Completed: 2026-09-11*
