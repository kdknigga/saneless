---
phase: 25-manual-duplex
plan: 11
subsystem: web, vocabulary
tags: [manual-duplex, flip-prompt, htmx, playwright, gap-closure, CR-01]
gap_closure: true
requires:
  - 25-10 (job-scoped armed WorkerFlipCoordinator, ScanWorker.flip_answer, job_id on flip routes)
provides:
  - "flip_answer_label(outcome: FlipOutcome) -> str in saneless.vocabulary, registered as a Jinja filter"
  - "routes._status_context(worker, job_store, claimed=None) shared by index, status poll and both flip routes"
  - "status.html AWAITING_FLIP branch acknowledges an answered flip instead of re-rendering the buttons"
affects:
  - 25-15 (UI-SPEC must record the acknowledgment copy and the new AWAITING_FLIP sub-branch)
tech-stack:
  added: []
  patterns:
    - "One status-context builder for every render of partials/status.html (extends D-17)"
    - "Route's own claim is authoritative for its own job; otherwise the worker's held answer"
key-files:
  created: []
  modified:
    - src/saneless/vocabulary.py
    - src/saneless/web/app.py
    - src/saneless/web/routes.py
    - src/saneless/web/templates/partials/status.html
    - tests/test_vocabulary.py
    - tests/test_web.py
    - tests/test_browser.py
decisions:
  - "The store's state still selects the status branch; the AWAITING_FLIP branch additionally consults the worker-held flip answer (D-16 not violated)"
  - "A claimed tuple is used only when its job id equals the rendered job's id (T-25-49)"
  - "Browser test teardown deletes the staged job (as fallback_page does) instead of moving it to a terminal state"
requirements: [DPLX-06]
metrics:
  duration: "~20 min"
  completed: 2026-09-14
  tasks: 3
  commits: 5
---

# Phase 25 Plan 11: Acknowledge a claimed flip answer (CR-01, route half) Summary

**What changed:** after Continue or Abort is claimed, the status area stops re-rendering the flip prompt. While the job row still reads `AWAITING_FLIP`, it shows `Flip confirmed. Scanning reverse sides next...` or `Aborting scan...` under `aria-busy="true"`, with no buttons. The flip route response, the one-second poll and a page reload all show it. A repeat click gets the same acknowledgment. An unanswered prompt renders exactly as before. A real Chromium click on Continue is now proven to answer the right job.

## Tasks

| # | Task | Commits |
|---|------|---------|
| 1 | `flip_answer_label` in the vocabulary | `722ef1b` (test), `fcc0da8` (feat) |
| 2 | Status rendering shows a claimed flip answer instead of the buttons | `063d00e` (test), `13a5596` (feat) |
| 3 | Playwright test: the real button posts the job id and gets the acknowledgment | `05ff034` (test) |

## Implementation notes

- `vocabulary.flip_answer_label` is a total `match` with `assert_never` and is listed in `__all__`. `TIMED_OUT` has an arm only so the match is total.
- `routes._status_context(worker, job_store, claimed=None)`:
  - It calls `_current_or_recent_job` once and returns `{"job", "flip_answer"}`.
  - `flip_answer` is set only when the job is `AWAITING_FLIP`. It comes from `claimed[1]` when `claimed[0] == job.id`, otherwise from `worker.flip_answer(job.id)`.
  - `FlipOutcome` and `JobState` are imported at runtime for the `is` comparison.
- Callers of `_status_context`:
  - `index` merges its context with `**status`.
  - `current_job_status` passes it straight through.
  - `continue_flip` and `abort_flip` pass `claimed=(job_id, <outcome>)` only when the worker call returned `True`.
  - `start_scan` renders `{"job": job, "flip_answer": None}`.
- `app.py` registers the `flip_answer_label` filter next to `state_label` and `progress_label`.
- `status.html`: inside the `AWAITING_FLIP` branch, `{% if flip_answer %}<p aria-busy="true">{{ flip_answer | flip_answer_label }}</p>{% else %}{% include "partials/flip.html" %}{% endif %}`. There is a Jinja comment pointing at CR-01. No other branch changed, and there is no `SCANNING_REVERSE` branch.

## Verification

- `uv run pytest -q`: 1082 passed, browser tests included (chromium is installed).
- `ruff check .`, `ruff format --check .`, `ty check` and `pyrefly check src tests` are all clean. `prek run --all-files` and `prek run --stage pre-push --all-files` both pass.
- `pytest tests/test_browser.py -m browser -k flip`: 3 passed. That is the whole `TestFlipPromptUI` class: the idle test, the scan-button test and the new click test.
- Mutation check (not committed): I removed `hx-vals` from the Continue button in `flip.html`. The new browser test then failed at `expect(status).to_contain_text("Flip confirmed. ...")`. I restored the file with `git checkout -- src/saneless/web/templates/partials/flip.html`.
- Acceptance greps:
  - `def _status_context` matches once, and `_status_context(` appears 5 times.
  - `filters["flip_answer_label"]` matches once.
  - `flip_answer | flip_answer_label` matches once.
  - `SCANNING_REVERSE` appears 0 times in `status.html`.
  - `def flip_answer_label(outcome: FlipOutcome) -> str`, `"flip_answer_label"` in `__all__` and `assert_never(outcome)` all match.
- Web tests added in `tests/test_web.py`:
  - claimed Abort and claimed Continue, with the polling attributes kept
  - double-clicked Abort
  - Continue after Abort, which shows the Abort that won
  - a foreign `job_id`, where the prompt stays and the answer is `None`
  - a poll after the answer
  - a poll with no answer, where the prompt shows
  - an index reload after the answer

  The staging fixture `waiting_flip` resets `_flip_coordinator` and `_current_job_id` on teardown.

## TDD Gate Compliance

- Task 1: RED `722ef1b` failed at collection with `ImportError: cannot import name 'flip_answer_label'`. GREEN is `fcc0da8`.
- Task 2: RED `063d00e` had 6 of the 8 new tests failing, all on the missing acknowledgment. The other two already passed and guard existing behaviour: the foreign-job prompt and the unanswered prompt. GREEN is `13a5596`.
- Task 3: this is a regression pin written after Task 2, as the plan specifies. It was committed as `test(25-11)`, and the `hx-vals` mutation check stands in for its RED.
- No refactor commits were needed.

## Deviations from Plan

**1. [Judgment - test hygiene] The browser test deletes its staged job on teardown instead of moving it to a terminal state**
- **Found during:** Task 3
- **Issue:** The plan's teardown moves the job to a terminal state. But `test_browser.py` documents, in `fallback_page`, that a surviving row leaks into every later session-scoped test: `index()` and the poll fall back to `list_recent(limit=1)`. Only deleting the row restores the idle page that `TestDarkModeEngagement` relies on.
- **Fix:** The teardown clears `_flip_coordinator` and `_current_job_id`, then calls `job_store.delete_job(job.id)`.
- **Files modified:** tests/test_browser.py
- **Commit:** `05ff034`

**2. [Additional test] `test_continue_after_abort_reports_the_abort`**
- This is beyond the plan's behaviour list. A late Continue on a job that was already aborted is dropped, so the route has no claim of its own. The acknowledgment then has to come from `worker.flip_answer`, and it shows the Abort that won (D-16).

Playwright MCP was not available to this agent, so the browser check ran through the project's pytest-playwright (`uv run pytest -m browser`).

## Known Stubs

None.

## Threat Flags

None. There is no new endpoint or input. T-25-48 and T-25-49 are covered by the claimed, double-click, poll and foreign-job tests. The acknowledgment copy is fixed vocabulary (T-25-50).

## Notes for downstream plans

- 25-15: `.planning/UI-SPEC.md` needs two updates. The status table should describe the AWAITING_FLIP acknowledgment sub-branch (`Flip confirmed. Scanning reverse sides next...` / `Aborting scan...`, `<p aria-busy="true">`). The "Flip routes (Phase 25)" note still says a later click "simply re-renders the job's current status"; it should now say an answered job shows the acknowledgment.

## Self-Check: PASSED

- FOUND: src/saneless/vocabulary.py, src/saneless/web/app.py, src/saneless/web/routes.py, src/saneless/web/templates/partials/status.html, tests/test_vocabulary.py, tests/test_web.py, tests/test_browser.py
- FOUND commits: 722ef1b, fcc0da8, 063d00e, 13a5596, 05ff034
