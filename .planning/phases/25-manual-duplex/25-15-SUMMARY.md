---
phase: 25-manual-duplex
plan: 15
subsystem: docs
tags: [manual-duplex, docs, gap-closure, WR-06, WR-07, CR-01]
gap_closure: true
requires:
  - 25-10 (job-scoped, armed WorkerFlipCoordinator; job_id on flip routes)
  - 25-11 (flip acknowledgment in the status partial)
  - 25-12 (flip_timeout_seconds bounds)
  - 25-13 (single-sided feeder preference, both-sides refusal, no-source-option path)
  - 25-14 (prompt failure aborts the CLI scan)
provides:
  - "web-api.md documents the required job_id form field (422), job-scoped drop rules and the acknowledgment copy"
  - "cli-commands.md exit-code table contiguous (0-3), manual-duplex paragraph after it"
  - "ADF how-to describes shipped feeder selection, both-sides-only refusal and no-source-option behaviour with code-verified messages"
  - "UI-SPEC records AWAITING_FLIP (answered) and job_id via hx-vals"
  - "deferred-items.md entry marked RESOLVED with corrected reach"
affects: []
tech-stack:
  added: []
  patterns:
    - "Quoted error messages verified by executing the shipped functions, not by copying plan text"
key-files:
  created: []
  modified:
    - docs/reference/web-api.md
    - docs/reference/cli-commands.md
    - docs/reference/configuration.md
    - docs/how-to/set-up-adf-duplex.md
    - docs/getting-started/first-web-ui-scan.md
    - docs/explanation/architecture.md
    - .planning/UI-SPEC.md
    - .planning/phases/25-manual-duplex/deferred-items.md
decisions:
  - "The IN-04 / legacy-source deprecation warnings stay undocumented (D-18): no page mentions the legacy source form"
requirements: [DPLX-04, DPLX-05, DPLX-06]
metrics:
  duration: "~15 min"
  completed: 2026-09-14
  tasks: 2
  commits: 2
---

# Phase 25 Plan 15: Docs and paper trail after the gap fixes Summary

The flip API reference, UI contract, architecture explanation, CLI/configuration references and
ADF duplex how-to now describe the shipped job-scoped flip answer, the acknowledgment, the
1..86400 timeout bound, single-sided feeder selection and the prompt-failure abort; the
exit-code table renders as one table and deferred-items.md records CR-01's real reach.

## Tasks

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Correct flip endpoint reference, UI contract and CR-01 paper trail (WR-07) | 43a412b | web-api.md, UI-SPEC.md, deferred-items.md, architecture.md, first-web-ui-scan.md |
| 2 | Repair exit-code table; feeder selection, timeout bounds, prompt-failure abort (WR-06) | a799314 | cli-commands.md, configuration.md, set-up-adf-duplex.md |

## What changed

- **web-api.md:** both flip endpoints gained a request-fields table with `job_id` (string,
  required, sent by the buttons, `422` when missing). Response text describes the acknowledgment
  (`Flip confirmed. Scanning reverse sides next...` / `Aborting scan...`, copied from
  `vocabulary.flip_answer_label`) shown while an answered job is still `AWAITING_FLIP`. The
  "How the two flip endpoints interact" bullets now say: first answer is final; an answer counts
  only for the named job once it is waiting (pass-A, other-job and post-job answers are dropped
  and still return status); a repeated click cannot reach the next job; the prompt is
  acknowledged, then replaced by `Scanning reverse sides...`. The false "The web UI cannot do
  this" and "gone before a second click" sentences are gone.
- **UI-SPEC.md:** "Eleven presentations" with AwaitingFlipAnswered; status table split into
  AWAITING_FLIP (unanswered)/(answered) rows; SCANNING_REVERSE paragraph adds the `job_id` drop;
  Flip routes note covers `hx-vals` `job_id`, CR-01 drops and the acknowledgment.
- **deferred-items.md:** heading marked RESOLVED by 25-10 and 25-11; Reach rewritten (the original
  classification was wrong; double-click/retry reached it; verifier reproduced a second Abort
  ending the next queued job; CR-01 BLOCKER); Resolution names the arming, `job_id` scoping,
  acknowledgment and regression tests (names checked in tests/test_worker.py and tests/test_web.py).
- **architecture.md:** one sentence on the job-bound, armed coordinator and job-naming routes.
- **first-web-ui-scan.md / set-up-adf-duplex.md web bullet:** the prompt is replaced by a short
  confirmation as soon as the click is received.
- **cli-commands.md:** row 3 moved up, paragraph after the table; adds that a terminal read error
  at the prompt aborts at once and is logged as `Flip prompt failed; treating it as an abort`
  (exact `logger.exception` text in `cli.py`).
- **configuration.md:** `flip_timeout_seconds` must be 1..86400 (`Field(ge=1, le=86_400)`);
  `duplex` row says "single-sided feeder source".
- **set-up-adf-duplex.md:** feeder note rewritten for single-sided preference and the warning when
  a both-sides source is named; new subsections "Scanners whose only feeder scans both sides"
  (refusal message, `duplex = "hardware"`, link to ADF Hardware Duplex) and "Scanners that report
  no source list" (runs when `source` names the feeder; refusal message for `Flatbed`); CLI
  bullet adds the terminal-error abort.

## Verification

- Task 1 and Task 2 grep gates pass (no removed sentences; `job_id` x3, `422` in flip section,
  `RESOLVED` once, "was wrong" present, `Eleven presentations` present / `Ten presentations` 0,
  table rows 0-3 contiguous, `86400` in timeout row, `single-sided` x5, `duplex = "hardware"`
  in the new refusal text, no `source = "Manual Duplex"` under docs/).
- Quoted refusal messages checked by executing `_resolve_feeder_source` / `_resolve_source` with
  the documented inputs and asserting each resulting message is a substring of the how-to (all
  three IN DOC). Executing the code was used instead of `grep -F` against sane_backend.py because
  the messages are split string literals with f-string parts that no single source line contains.
- `uv run mkdocs build` into the scratchpad: 0 WARNING lines before and after; rendered HTML has
  the `#adf-hardware-duplex` anchor and a single exit-code table.
- `uv run prek run --all-files` passes.

## Deviations from Plan

**1. [Rule 1 - Bug] Two sentences made false by 25-11 were corrected rather than appended to**
- **Found during:** Task 1 and Task 2
- **Issue:** first-web-ui-scan.md said "The prompt disappears as soon as the back sides start
  scanning" and set-up-adf-duplex.md's web bullet said "The buttons disappear on their own as
  soon as pass B starts". Both are false now that the prompt is replaced on click.
- **Fix:** replaced each with the confirmation sentence instead of adding it beside the stale one.
- **Commits:** 43a412b, a799314

**2. [Plan conflict] deferred-items.md does not quote "direct API callers only"**
- **Issue:** the action asked to quote the original classification, but the verify gate requires
  the phrase to be absent from deferred-items.md.
- **Fix:** paraphrased it ("that only a direct API caller could send the early answer, never a
  browser user -- was wrong"); the gate passes and the record stays clear.

## Known Stubs

None.

## Self-Check: PASSED

- All eight modified files exist; commits 43a412b and a799314 present on the branch.
