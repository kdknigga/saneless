---
phase: 25-manual-duplex
verified: 2026-09-14T15:15:49Z
status: gaps_found
score: 4/5 must-haves verified
overrides_applied: 0
gaps:
  - truth: "During pass B the job is in a visible SCANNING_REVERSE state, Abort at the flip prompt cancels the job, and wait_transition no longer exists (DPLX-06 / success criterion 4)"
    status: partial
    reason: >
      SCANNING_REVERSE and the deletion of wait_transition/_transition_event are fully verified
      in code. But "Abort at the flip prompt cancels the job" is not reliably true: the
      WorkerFlipCoordinator is constructed and begins accepting signals at job start (worker.py:256),
      well before the pipeline ever announces AWAITING_FLIP, and continue_flip/abort_flip
      (worker.py:171-183, routes.py:309-345) apply an incoming signal to whichever coordinator
      currently exists with no job-id or state check. Reproduced independently (outside the
      reviewer's own script) against the real ScanWorker/run_pipeline: a normal "did that do
      anything?" double-click on Abort at the flip prompt -- caused by the route re-rendering the
      stale AWAITING_FLIP partial before the worker thread has woken and cleared the job -- lands
      the second click on a different, already-started job and aborts its pass A before that job's
      operator ever saw a flip prompt. The mirror-image Continue case reproduces C-02 itself (pass
      B starts on an unflipped stack) because a stale early Continue is claimed by the coordinator
      before pass A even finishes. This is the code review's CR-01 (rated BLOCKER, not a minor API
      quirk), and it directly falsifies the "Abort ... cancels the job" clause of DPLX-06 as well as
      the phase's own goal statement ("manual duplex actually works ... pass B is visible").
    artifacts:
      - path: "src/saneless/worker.py"
        issue: "WorkerFlipCoordinator is created at _process_job start (line 256), before pass A, and continue_flip/abort_flip (171-183) signal it unconditionally -- no arming gate tied to AWAITING_FLIP, no job-id check"
      - path: "src/saneless/web/routes.py"
        issue: "continue_flip/abort_flip routes (309-345) call worker.continue_flip()/abort_flip() and immediately render _current_or_recent_job with no verification that the signal was claimed by, or even intended for, that job"
      - path: ".planning/phases/25-manual-duplex/deferred-items.md"
        issue: "Claims 'Reach: direct API callers only. ... a browser user cannot send the early answer.' This is false -- an ordinary double-click/retry after a legitimate Abort click reaches it, as reproduced against the real worker."
    missing:
      - "Arm the coordinator only when the pipeline actually announces AWAITING_FLIP (or SCANNING_REVERSE has not yet started), not at job construction"
      - "Have continue_flip/abort_flip name the job they are answering (e.g. job_id in the flip partial's hx-vals) and drop a signal that does not match the current job"
      - "A worker test that sends an Abort during pass A and during the next queued job's pass A, and asserts both are dropped rather than misapplied"
      - "Correct docs/reference/web-api.md's false claim that the web UI cannot send an early answer (WR-07), and remove or correct deferred-items.md's reach classification"
---

# Phase 25: Manual Duplex Verification Report

**Phase Goal:** Manual duplex actually works and is honest about where it is — `duplex` is its own
profile field, `source` is passed to SANE verbatim, exactly one place decides the strategy, a
required `FlipCoordinator` with a timeout serves both CLI and web, and pass B is visible — with the
ADF duplex how-to rewritten in-phase to stop documenting `source = "Manual Duplex"` as current.

**Verified:** 2026-09-14T15:15:49Z
**Status:** gaps_found
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | A legacy `source = "Manual Duplex"` config still loads and scans, is translated to `duplex = "manual"` at load with a deprecation warning, and `source` is never inspected for strategy anywhere in the codebase | ✓ VERIFIED | `config.py:121-141` (`_translate_legacy_manual_duplex` before-validator), `config.py:260-292` (`warn_on_legacy_duplex_source`, named per profile). `pipeline._is_manual_duplex` is gone; the only strategy reads are `profile.duplex == "manual"` in `pipeline.py:1049`, `worker.py:254`, `cli.py:226` — all field reads, none inspect `source`. `grep -n "Manual Duplex"` across `src/` finds only the deprecated-form recogniser, its docstrings, and `scanner/base.py`'s classifier comment. |
| 2 | `saneless scan` with a manual-duplex profile prompts the operator on stdin and blocks until answered, then completes a two-pass scan; manual duplex with no coordinator, or with no interactive terminal, is refused before the scanner is opened | ✓ VERIFIED | `cli.py:218-232` refuses (exit 2) before `SaneBackend` is constructed when `not _stdin_is_interactive()`. `ClickFlipCoordinator` (`cli.py:68-136`) runs `click.confirm` on a daemon thread bounded by `flip_timeout_seconds` (D-19). `pipeline._flip_context` (`pipeline.py:901-...`) raises `ConfigError` when `request.flip_coordinator is None`, called before `_resolve_device` (`pipeline.py:1050-1052`), so no SANE contact happens first. |
| 3 | A flip wait that exceeds the timeout fails the job with a clear message and releases the scanner for the next job | ✓ VERIFIED | `pipeline.py:868-873`: `FlipOutcome.TIMED_OUT` raises `ScanError` with `"... timed out after {timeout:g} seconds: nobody confirmed the stack was flipped"`, before pass B / any further scanner call. `scan_pages` already opens/closes the device per call (pre-existing), so the handle is released; the worker's `finally` (worker.py:332-338) clears `_flip_coordinator`/`_current_job_id` and the thread returns to `_run`'s queue loop, unparked for the next job. `tests/test_worker.py`/`test_outcomes_e2e.py` cover the end-to-end timeout case (25-05 summary). |
| 4 | During pass B the job reports `SCANNING_REVERSE`, Abort at the flip prompt cancels the job, and `wait_transition` no longer exists | ✗ FAILED | `SCANNING_REVERSE` and the transition-event deletion are real (`vocabulary.py`, `grep` for `wait_transition`/`_transition_event` across `src/`+`tests/` returns 0 matches). But "Abort ... cancels the job" is not reliable: CR-01 (see Gaps below), reproduced independently, shows a normal double-click can abort a *different* job than the one the operator meant, and the mirror case can start pass B on an unflipped stack — the very C-02 failure this phase exists to close. |
| 5 | A write-then-load round trip proves auto-profiles always emits a `default` profile for flatbed-only, feeder-only, and mixed devices | ✓ VERIFIED | `tests/test_auto_profiles.py:548-588`, `test_generated_config_round_trips_with_a_default_profile`, parametrised over `flatbed-only`/`feeder-only`/`mixed` (`:551,555,560`), writes via `write_profiles_to_config` and reloads via `load_settings`, asserting `settings.profiles["default"]` for each shape. |

**Score:** 4/5 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/saneless/config.py` | `duplex` field + legacy translation + named warning | ✓ VERIFIED | `ProfileConfig.duplex: Literal["none","hardware","manual"] = "none"` (`:109`); before-validator (`:121-141`); `warn_on_legacy_duplex_source` (`:260-292`) |
| `src/saneless/pipeline.py` | Single strategy read, `FlipCoordinator` ABC, `match`/`assert_never` dispatch | ✓ VERIFIED | `:1049` (sole canonical read + comment naming DPLX-03), `:114-136` (ABC), `:862-875` and `:1097-1107` (both dispatches use `match`/`assert_never`, no `isinstance`) |
| `src/saneless/worker.py` | `WorkerFlipCoordinator`, no duplicated string-matching rule, `wait_transition` gone | ⚠️ WIRED but with race | Rule is a field read (not a duplicated heuristic) — fine. `wait_transition`/`_transition_event` fully removed. But the coordinator's lifetime/arming is broken (CR-01) — see gap above. |
| `src/saneless/cli.py` | `click.confirm`-based prompt, non-TTY refusal, exit-2 | ✓ VERIFIED | `cli.py:68-232` |
| `src/saneless/web/routes.py` | Shared "current or most recent job" lookup on all three status routes | ✓ VERIFIED (lookup); ⚠️ (signal gating) | `_current_or_recent_job` used at `:213`, `:321`, and in `abort_flip`; but the routes do not scope the *signal* to the job being viewed (part of CR-01) |
| `src/saneless/vocabulary.py` | `JobState.SCANNING_REVERSE` (9th member), `FlipOutcome` | ✓ VERIFIED | confirmed via `PipelineEvent.job_state` no longer `Optional` and match arms in `pipeline.py:83-97` |
| `src/saneless/scanner/` | Feeder-source resolution, no `Auto` fallback reachable for manual duplex | ✓ VERIFIED (core case) | `sane_backend.py:791-847`, `_resolve_feeder_source`; `Auto` substitution structurally unreachable via disjoint early return (`:875-880`). Edge-case gaps noted below (WR-02/WR-03), not roadmap blockers. |
| `src/saneless/auto_profiles.py` | `duplex` key on generated profiles, DPLX-07 round trip | ✓ VERIFIED | `_duplex()` helper, write-only-when-non-default; round-trip test at `tests/test_auto_profiles.py:564` |
| `docs/how-to/set-up-adf-duplex.md` | Rewritten to stop teaching `source = "Manual Duplex"` | ✓ VERIFIED | Only the two-key form (`source` + `duplex = "manual"`) appears; grep confirms no legacy `source = "Manual Duplex"` example remains |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `run_pipeline` | `_flip_context` | `manual_duplex` gate before `_resolve_device` | WIRED | `pipeline.py:1049-1052` |
| `ScanWorker._process_job` | `WorkerFlipCoordinator` | construction keyed on `profile.duplex == "manual"` | WIRED but unscoped | Constructed correctly, but not *armed* correctly — see gap |
| `routes.continue_flip`/`abort_flip` | `ScanWorker.continue_flip`/`abort_flip` | direct call, then shared job lookup for render | WIRED but unscoped | No job-id/state check on the signal itself (CR-01) |
| `cli.scan` | `ClickFlipCoordinator` | `PipelineRequest.flip_coordinator` | WIRED | `cli.py` (confirmed by 25-07 summary and code read) |
| `auto_profiles.generate_profiles` | `write_profiles_to_config` → `load_settings` | round-trip test | WIRED | `tests/test_auto_profiles.py:564-588` |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Full test suite | `uv run pytest -q` | 1043 passed | ✓ PASS |
| Lint | `uv run ruff check .` | No issues found | ✓ PASS |
| Type check (ty) | `uv run ty check` | All checks passed | ✓ PASS |
| Type check (pyrefly) | `uv run pyrefly check src tests` | 0 errors | ✓ PASS |
| `wait_transition`/`_transition_event` removed | `grep -rn "wait_transition\|_transition_event" src/ tests/` | 0 matches | ✓ PASS |
| CR-01 reproduction (double-click Abort mis-hits a queued job) | `uv run python3 <verifier's own repro script against the live ScanWorker/run_pipeline, independent of reviewer's script>` | `job1: ERROR ... aborted at the flip prompt` / `job2: ERROR ... aborted at the flip prompt scan calls: 2` — job 2's pass A was aborted by a click meant to confirm job 1's abort landed | ✗ FAIL | Confirms CR-01 as a genuine, reproducible defect, not a documentation nuance |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|-------------|--------|----------|
| DPLX-01 | 25-01, 25-04, 25-06, 25-08, 25-09 | `duplex` field; `source` never overloaded | ✓ SATISFIED | See Truth 1 |
| DPLX-02 | 25-01, 25-06, 25-09 | Legacy translation + deprecation warning | ✓ SATISFIED (with WR-05 caveat below) | See Truth 1 |
| DPLX-03 | 25-04, 25-09 | Single decision point; no duplicated rule/`isinstance` | ✓ SATISFIED | See Truth 1 and dispatch verification |
| DPLX-04 | 25-03, 25-06, 25-07, 25-09 | Required `FlipCoordinator` ABC w/ timeout, serves CLI + web, refused without one | ✓ SATISFIED | See Truth 2 |
| DPLX-05 | 25-03, 25-07, 25-09 | Timeout fails job with clear message, releases scanner | ✓ SATISFIED | See Truth 3 |
| DPLX-06 | 25-02, 25-05, 25-09 | `SCANNING_REVERSE` visible, Abort cancels job, `wait_transition` gone | ✗ BLOCKED (partial) | `SCANNING_REVERSE`/deletion verified; Abort-cancels-job guarantee broken by CR-01 |
| DPLX-07 | 25-08, 25-09 | Auto-profiles always emits `default`, proven by round trip | ✓ SATISFIED | See Truth 5 |

No orphaned requirements: all seven DPLX IDs appear in at least one plan's `requirements:` frontmatter and are cross-referenced in `.planning/REQUIREMENTS.md:75-81`.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `src/saneless/worker.py` | 171-183, 256 | Coordinator armed at job start, not at `AWAITING_FLIP`; no job-scoping on signals | 🛑 Blocker | CR-01 — see gaps |
| `src/saneless/web/routes.py` | 309-345 | Flip routes signal without checking the job they're answering | 🛑 Blocker (same root cause as above) | CR-01 |
| `src/saneless/config.py` | 258-292 | Deprecation warning logged before `configure_logging` runs; never reaches `log_file` (WR-05) | ⚠️ Warning | Undercuts D-18's premise that the warning is the operator's sole migration instruction |
| `src/saneless/scanner/sane_backend.py` | 824-828 | `_resolve_feeder_source` accepts a `FEEDER_DUPLEX` source as if it were simplex (WR-02) | ⚠️ Warning | Silent scrambled-page corruption on a narrow hardware/config combination |
| `src/saneless/scanner/sane_backend.py` | 879-886 | Manual duplex unconditionally refused on a device with no `source` option, a regression vs. pre-phase behaviour (WR-03) | ⚠️ Warning | Narrow device-compatibility regression |
| `src/saneless/auto_profiles.py` | 197-222 (used `worker.py:209-224`) | `is_bare_default` ignores `duplex`, so a hand-written `duplex="manual"` default profile can be silently overwritten by `_maybe_auto_generate` (WR-04) | ⚠️ Warning | First web job on such a config takes one flatbed snapshot instead of prompting |
| `src/saneless/config.py` | 280-282 | Legacy-looking source with an explicit non-`manual` `duplex` is neither translated nor warned about (IN-04) | ℹ️ Info | Narrow; explicit operator override, but reaches SANE unchanged with C-01's failure mode possible |
| `docs/reference/cli-commands.md` | 34-38 | Exit-code table broken by an inserted paragraph (WR-06) | ⚠️ Warning | Cosmetic doc defect, not functional |
| `docs/reference/web-api.md` | 149-150 | States the web UI "cannot" send an early flip answer and that buttons "are gone before a second click" — both false per CR-01 (WR-07) | ⚠️ Warning | Same root cause as the CR-01 gap; the doc overstates a safety property the code doesn't have |
| `.planning/phases/25-manual-duplex/deferred-items.md` | — | Classifies the early-answer defect as "direct API callers only" | ⚠️ Warning | Contradicted by reproduction; not a TBD/FIXME marker, but a factual misclassification that understated CR-01's severity going into review |

No `TBD`/`FIXME`/`XXX` debt markers found in the phase's changed files.

### Human Verification Required

None. The one uncertain item (whether the CR-01 race is reachable through ordinary browser interaction, not just direct API calls) was resolved programmatically: a script driving the real `ScanWorker`/`run_pipeline`/`_current_or_recent_job` — the same objects the HTTP routes call — reproduces the mis-applied Abort deterministically. No browser automation or hardware is needed to settle it.

### Gaps Summary

Four of five roadmap success criteria hold cleanly, and the mechanical claims of the phase goal —
`duplex` as its own field, `source` passed verbatim, a single `match`/`assert_never` strategy
dispatch, a required `FlipCoordinator` ABC with a timeout serving both CLI and web, `SCANNING_REVERSE`
persisted and visible, `wait_transition` deleted, the ADF how-to rewritten — are all real and present
in the code, not just claimed in SUMMARY.md. Quality gates (pytest, ruff, ty, pyrefly) are clean.

The one blocking gap is concurrency, not architecture: the flip coordinator is constructed and starts
accepting signals the instant a manual-duplex job begins, not when the pipeline actually reaches
`AWAITING_FLIP`, and the Continue/Abort routes apply a signal to whichever coordinator is live with no
check that it belongs to the job the operator is looking at. This was flagged by the code review as
CR-01 (BLOCKER) and independently reproduced here against the live worker/pipeline: an ordinary
double-click on Abort — the natural reaction when the UI doesn't visibly change on the first click,
because the render races the worker thread waking up — can abort a different, already-running job
before its operator ever saw a flip prompt. The mirror case (an early, stale Continue) reproduces
C-02, the original failure this entire phase exists to close: pass B can start on an unflipped stack.
This directly falsifies the "Abort at the flip prompt cancels the job" clause of DPLX-06 / success
criterion 4, and the `deferred-items.md` note that shipped alongside the docs plan incorrectly
asserts the defect is reachable only from direct API callers, understating its severity for anyone
reading the phase's own paper trail.

The fix is scoped and already sketched in the code review (CR-01's fix section): arm the coordinator
only when `AWAITING_FLIP` is actually persisted, and have the routes name the job they are answering
so a stale or misdirected signal is dropped rather than applied. This does not require reopening any
of the phase's locked decisions (D-09, D-16, D-17) — it tightens D-16's "one atomic answer" guarantee
to also cover *which job* the answer belongs to, which D-16 assumed but did not implement.

The remaining warnings (WR-01 through WR-08, IN-01 through IN-04) are real but narrower: unbounded
`flip_timeout_seconds`, a hardware-duplex source being accepted as if simplex, manual duplex
regressing on devices with no `source` option, `is_bare_default` not accounting for `duplex`, the
deprecation warning missing the log file, a broken doc table, and two other doc-accuracy issues. None
of these falsify a named success criterion on their own, but several (WR-04, WR-05) compound the same
"is it actually safe to trust this in an appliance the operator doesn't babysit" concern the phase
goal raises, and are worth closing in the same pass as CR-01 rather than carried forward silently.
