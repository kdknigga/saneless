---
phase: 25-manual-duplex
verified: 2026-09-14T17:49:11Z
status: passed
score: 5/5 must-haves verified
overrides_applied: 0
re_verification:
  previous_status: gaps_found
  previous_score: 4/5
  gaps_closed:
    - "During pass B the job reports SCANNING_REVERSE, Abort at the flip prompt cancels the job, and wait_transition no longer exists (DPLX-06 / success criterion 4) — the CR-01 race (stale double-click Abort landing on a different, already-running job; early Continue during pass A) is closed. WorkerFlipCoordinator is now bound to one job_id, armed only when the pipeline announces AWAITING_FLIP (before the state is persisted), and continue_flip/abort_flip compare the posted job_id against the live coordinator's own job_id from a single snapshot. Independently reproduced against the real ScanWorker/run_pipeline (not the phase's own test file): both scenarios now behave correctly — the stale click is dropped and the correct job's own click is what starts/aborts it."
  gaps_remaining: []
  regressions: []
---

# Phase 25: Manual Duplex Verification Report

**Phase Goal:** Manual duplex actually works and is honest about where it is — `duplex` is its own
profile field, `source` is passed to SANE verbatim, exactly one place decides the strategy, a
required `FlipCoordinator` with a timeout serves both CLI and web, and pass B is visible — with the
ADF duplex how-to rewritten in-phase to stop documenting `source = "Manual Duplex"` as current.

**Verified:** 2026-09-14T17:49:11Z
**Status:** passed
**Re-verification:** Yes — after gap closure (plans 25-10 through 25-15, gap waves 1-4)

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | A legacy `source = "Manual Duplex"` config still loads and scans, is translated to `duplex = "manual"` at load with a deprecation warning, and `source` is never inspected for strategy anywhere in the codebase | ✓ VERIFIED | `config.py:111` (`duplex: Literal[...]`), `:123` (`_translate_legacy_manual_duplex` before-validator, unchanged since initial verification), `:264-311` (`warn_on_legacy_duplex_sources`, now called from `cli.py:196` after `configure_logging`, per the user-approved D-03 amendment). Only reads of `duplex` decide strategy: `pipeline.py:1140`, `worker.py:392`, `cli.py:222`. |
| 2 | `saneless scan` with a manual-duplex profile prompts the operator on stdin and blocks until answered, then completes a two-pass scan; manual duplex with no coordinator, or with no interactive terminal, is refused before the scanner is opened | ✓ VERIFIED | `cli.py:225-232` refuses (exit 2) before the scanner opens when `not _stdin_is_interactive()`. `ClickFlipCoordinator` runs `click.confirm` on a daemon thread bounded by `flip_timeout_seconds`, now built on the shared `FlipAnswerSlot` (25-14/IN-02). No regression found. |
| 3 | A flip wait that exceeds the timeout fails the job with a clear message and releases the scanner for the next job | ✓ VERIFIED | `pipeline.py:959` (`FlipOutcome.TIMED_OUT` raises `ScanError`), unchanged in behaviour. `flip_timeout_seconds` is now bounded to `1..86_400` (`config.py`, `Field(ge=1, le=86_400)`), closing the previously-open WR-01 (initial review) unbounded-timeout warning. |
| 4 | During pass B the job reports `SCANNING_REVERSE`, Abort at the flip prompt cancels the job, and `wait_transition` no longer exists | ✓ VERIFIED (gap closed) | `SCANNING_REVERSE` persisted and visible (`vocabulary.py:47`, `pipeline.py` match arms). `grep -rn "wait_transition\|_transition_event" src/ tests/` → 0 matches. Abort-cancels-the-job is now reliable: see "CR-01 Independent Reproduction" below. |
| 5 | A write-then-load round trip proves auto-profiles always emits a `default` profile for flatbed-only, feeder-only, and mixed devices | ✓ VERIFIED | `tests/test_auto_profiles.py` round-trip tests still pass (3 passed, re-run directly). `is_bare_default` was hardened in gap closure (25-13, WR-04) to compare the whole profile against `ProfileConfig()`, not ignoring `duplex` — this strengthens rather than weakens the truth. |

**Score:** 5/5 truths verified

### CR-01 Independent Reproduction (primary focus of this re-verification)

Per instructions, I did not trust the review's or the phase's own regression tests as sole evidence.
I wrote a fresh reproduction script (not part of the test suite, not derived from
`tests/test_worker.py`) that drives the real `ScanWorker` / `run_pipeline` with a gated fake
scanner, reproducing both of the prior verification's falsifying scenarios:

**Scenario 1 — stale double-click Abort landing on the next job.** Two manual-duplex jobs queued.
Abort clicked at job 1's prompt (claimed). A second, stale Abort naming job 1 is sent again once
job 2's pass A is already in flight (modelling the double-click's second click arriving late).

```
First Abort click on job1: claimed=True
job1 state: ERROR
Second (stale) Abort click still naming job1: claimed=False
job2 state after pass A: AWAITING_FLIP
job2 flip_answer immediately after reaching prompt: None
job2's own Continue click: claimed=True
job2 final state: DONE, error=None
PASS
```

**Scenario 2 — early Continue sent during pass A.** A Continue is sent for the job while its pass A
scan is still in flight (gated open).

```
Early Continue during pass A: claimed=False
job state after pass A completes: AWAITING_FLIP
flip_answer right when AWAITING_FLIP is reached: None
Real Continue click at the actual prompt: claimed=True
final state: DONE
```

Both scenarios that previously falsified DPLX-06 / success criterion 4 now behave correctly: the
stale/foreign/early signal is dropped, and only the correctly-named signal sent after the job's own
`AWAITING_FLIP` is announced is claimed. This independently confirms the code review's CR-01
disposition ("Closed") rather than merely trusting it.

I also read the fix directly: `WorkerFlipCoordinator.__init__(job_id)` binds one coordinator to one
job (`worker.py:79-88`); `arm()` is called from `_status_cb` in `worker.py:429` *before*
`self._job_store.update_state(_jid, state)` persists `AWAITING_FLIP` at `worker.py:430` — so no
observer can ever read `AWAITING_FLIP` from the store and find the coordinator unarmed.
`_signal_flip` (`worker.py:275-321`) reads `self._flip_coordinator` once into a local and compares
`coordinator.job_id != job_id` before signalling — a single snapshot, so the check and the signal
cannot straddle the `finally` block's `self._flip_coordinator = None` (`worker.py:481`) and land on
a different job's coordinator. `routes.py:355-410`'s `continue_flip`/`abort_flip` now require
`job_id: str = Form(...)` and are rejected with 422 without it (confirmed by
`tests/test_web.py::test_flip_routes_require_a_job_id`, run directly: passes). The flip partial
sends it via `hx-vals='{{ {"job_id": job.id} | tojson }}'` (`flip.html:42-43`), and a real Chromium
click (not just a string assertion) exercises the full path end to end
(`tests/test_browser.py::test_flip_continue_click_answers_the_waiting_job`, run directly: passes).

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/saneless/config.py` | `duplex` field + legacy translation + named warning | ✓ VERIFIED | Unchanged core; warning call site moved to `cli.py` post-logging (D-03 amendment) |
| `src/saneless/pipeline.py` | Single strategy read, `FlipCoordinator` ABC, `match`/`assert_never` dispatch, shared `FlipAnswerSlot` | ✓ VERIFIED | `:1140` sole read; `FlipAnswerSlot` (`:154-240`, moved up in the module during gap closure) now composed by both coordinators (IN-02 closed) |
| `src/saneless/worker.py` | `WorkerFlipCoordinator` bound to one job, armed at `AWAITING_FLIP`, no duplicated string rule, `wait_transition` gone | ✓ VERIFIED | Fixed per CR-01; see reproduction above |
| `src/saneless/cli.py` | `click.confirm`-based prompt, non-TTY refusal, exit-2, bounded daemon thread, prompt-failure aborts at once | ✓ VERIFIED | WR-08 (prompt-thread exception hangs) also closed: `except Exception` logs and settles `ABORTED` immediately (`cli.py:143-152`) |
| `src/saneless/web/routes.py` | Shared job lookup, job-scoped flip signals, acknowledgment rendering | ✓ VERIFIED | `_current_or_recent_job`, `_status_context` with `claimed` param, both flip routes require `job_id` |
| `src/saneless/vocabulary.py` | `JobState.SCANNING_REVERSE`, `FlipOutcome`, `flip_answer_label` | ✓ VERIFIED | `flip_answer_label` added in 25-11 for acknowledgment copy |
| `src/saneless/scanner/` | Feeder-source resolution incl. `FEEDER_DUPLEX` handling and no-source-option devices | ✓ VERIFIED | WR-02/WR-03 (initial review) closed by 25-13: `_resolve_feeder_source` prefers `FEEDER`, refuses `FEEDER_DUPLEX`-only devices with a `duplex = "hardware"` pointer; no-source-option devices trust `classify_source` again |
| `src/saneless/auto_profiles.py` | `duplex` key on generated profiles, DPLX-07 round trip, whole-profile `is_bare_default` | ✓ VERIFIED | `is_bare_default` now compares the full `ProfileConfig()` (WR-04 closed) |
| `docs/how-to/set-up-adf-duplex.md` | Rewritten to stop teaching `source = "Manual Duplex"`, updated for the new feeder-preference/refusal behaviour | ✓ VERIFIED | No `source = "Manual Duplex"` example; describes single-sided preference, both-sides-only refusal (25-15) |
| `docs/reference/web-api.md` | No longer claims the web UI "cannot" send an early answer; documents `job_id` requirement | ✓ VERIFIED (with WR-03 caveat) | Corrected prose is accurate for what it claims. It does not tell a *direct API caller with no browser* how to obtain `job_id` (the only place it appears is inside the flip button's `hx-vals` markup) — see Anti-Patterns/Warnings below. This is a real doc gap for headless API callers, but it does not make the corrected claims false, and DPLX-04's requirement ("the web provides the HTMX Continue button") is about the web UI, which is fully documented and tested. |
| `docs/reference/cli-commands.md` | Exit-code table intact | ✓ VERIFIED | Rows 0-3 in one table, paragraph follows (WR-06 closed) |
| `.planning/phases/25-manual-duplex/deferred-items.md` | Corrected reach classification | ✓ VERIFIED | No longer claims the defect was "direct API callers only"; documents the actual CR-01 finding and its resolution in 25-10/25-11 |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `run_pipeline` | `_flip_context` | `manual_duplex` gate before `_resolve_device` | WIRED | Unchanged from initial verification |
| `ScanWorker._process_job._status_cb` | `WorkerFlipCoordinator.arm()` | called before `update_state(AWAITING_FLIP)` | WIRED | `worker.py:421-430` |
| `routes.continue_flip`/`abort_flip` | `ScanWorker.continue_flip`/`abort_flip(job_id)` | required `job_id` form field | WIRED | `routes.py:355-410`; 422 without it |
| `flip.html` Continue/Abort buttons | `job_id` | `hx-vals='{{ {"job_id": job.id} | tojson }}'` | WIRED | Verified end-to-end with a real browser click test |
| `cli.scan` | `ClickFlipCoordinator` | `PipelineRequest.flip_coordinator`, built on shared `FlipAnswerSlot` | WIRED | `cli.py`; IN-02 unification |
| `auto_profiles.generate_profiles` | `write_profiles_to_config` → `load_settings` | round-trip test | WIRED | `tests/test_auto_profiles.py`, re-run directly: 3 passed |
| `cli()` | `config.warn_on_legacy_duplex_sources` | called after `configure_logging(...)` | WIRED (but see WR-02 below) | `cli.py:188-196`; reaches `log_file`, does not reach stderr without `-v` |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| Full test suite | `uv run pytest -q` | 1103 passed | ✓ PASS |
| Lint | `uv run ruff check .` | No issues found | ✓ PASS |
| Type check (ty) | `uv run ty check` | All checks passed | ✓ PASS |
| Type check (pyrefly) | `uv run pyrefly check src tests` | 0 errors | ✓ PASS |
| `wait_transition`/`_transition_event` removed | `grep -rn "wait_transition\|_transition_event" src/ tests/` | 0 matches | ✓ PASS |
| CR-01 independent reproduction (double-click Abort, gated scanner, real ScanWorker/run_pipeline) | custom script, not part of test suite | stale click dropped (`claimed=False`), job 2 unaffected, DONE | ✓ PASS |
| CR-01 independent reproduction (early Continue during pass A) | same script | early click dropped (`claimed=False`), pass B only starts on the real prompt click | ✓ PASS |
| Job-scoping unit regressions | `uv run pytest -q -k "TestFlipSignalsAreJobScoped"` | passed | ✓ PASS |
| Real-browser flip click | `uv run pytest -q -k test_flip_continue_click_answers_the_waiting_job` | 1 passed | ✓ PASS |
| Flip routes require `job_id` | `uv run pytest -q -k test_flip_routes_require_a_job_id` | passed (parametrized) | ✓ PASS |
| Auto-profiles round trip | `uv run pytest -q tests/test_auto_profiles.py -k round_trip` | 3 passed | ✓ PASS |
| WR-02 reproduction (stderr visibility) | read `logging_config.py`: file handler only, no stderr handler without `-v` or on `OSError` | confirmed: warning reaches `log_file` only | Confirms review's WR-02 (see Warnings) |
| WR-01 reproduction (shutdown while parked at flip prompt) | read `worker.py::stop()`: no coordinator resolution before `join(timeout=5)` | confirmed: a parked `wait_for_flip` (up to 86,400s) is not woken by `stop()` | Confirms review's WR-01 (see Warnings) |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|-------------|--------|----------|
| DPLX-01 | 25-01, 25-04, 25-06, 25-08, 25-09 | `duplex` field; `source` never overloaded | ✓ SATISFIED | Truth 1 |
| DPLX-02 | 25-01, 25-06, 25-09, 25-12 | Legacy translation + deprecation warning | ✓ SATISFIED (see WR-02 caveat) | Truth 1; the warning is logged as DPLX-02 literally requires, though it is less visible than D-18 intended — a warning, not a requirement failure |
| DPLX-03 | 25-04, 25-09 | Single decision point; no duplicated rule/`isinstance` | ✓ SATISFIED | Truth 1 and dispatch verification |
| DPLX-04 | 25-03, 25-06, 25-07, 25-09, 25-10, 25-14 | Required `FlipCoordinator` ABC w/ timeout, serves CLI + web, refused without one | ✓ SATISFIED | Truth 2; shared `FlipAnswerSlot` unifies both coordinators |
| DPLX-05 | 25-03, 25-07, 25-09, 25-12 | Timeout fails job with clear message, releases scanner; now bounded 1..86400 | ✓ SATISFIED | Truth 3 |
| DPLX-06 | 25-02, 25-05, 25-09, 25-10, 25-11 | `SCANNING_REVERSE` visible, Abort cancels job, `wait_transition` gone | ✓ SATISFIED (gap closed) | Truth 4; CR-01 fix independently reproduced |
| DPLX-07 | 25-08, 25-09, 25-13 | Auto-profiles always emits `default`, proven by round trip | ✓ SATISFIED | Truth 5 |

No orphaned requirements: all seven DPLX IDs appear in plan `requirements:` frontmatter (initial and
gap-closure plans) and are cross-referenced in `.planning/REQUIREMENTS.md:75-81`.

### Anti-Patterns Found

No `TBD`/`FIXME`/`XXX` debt markers in any file touched between `07054af` and `HEAD`.

| File | Line | Pattern | Severity | Impact | Falsifies a roadmap SC? |
|------|------|---------|----------|--------|--------------------------|
| `src/saneless/worker.py` | 208-212 | `ScanWorker.stop()` does not resolve a live flip wait; a worker shut down while parked at `AWAITING_FLIP` (wait up to 86,400s) leaves the row `AWAITING_FLIP` forever and the thread un-joinable within the 5s bound (WR-01, new in re-review) | ⚠️ Warning | Only reachable via process shutdown/reload while a job is genuinely parked at the flip prompt — not the normal single-process operation the roadmap's success criteria describe. Confirmed real by reading `stop()` directly (no `cancel()`/`settle()` call exists on the coordinator path). Worker shutdown robustness in general is Phase 26's territory (ROBU-03, ROBU-06), though the review correctly notes Phase 26's planned stop-flag fix does not by itself wake an `Event.wait` — this specific fix still needs its own line item, not automatic coverage. | No — SC4 concerns Abort behaviour during normal operation, which is verified working (see CR-01 reproduction). Does not roll into ROBU-03/06 automatically; flagged for explicit follow-up. |
| `src/saneless/cli.py` / `src/saneless/logging_config.py` | 188-196 / 41-66 | The legacy-duplex deprecation warning reaches `log_file` (WR-05 fix) but no longer reaches stderr for an interactive `saneless scan` user without `-v`, or for the documented Docker deployment's `docker logs` (WR-02, new in re-review) | ⚠️ Warning | Confirmed real by reading `configure_logging`: only a `RotatingFileHandler` is attached unless `-v` or the file can't be opened. D-18's premise ("the warning is the operator's only migration instruction") is undercut for two real audiences. | No — DPLX-02 literally requires the config "logs a deprecation warning," which it does (to `log_file`). The *visibility* gap is a real operator-experience regression worth fixing, but it does not falsify the requirement as worded. |
| `docs/reference/web-api.md` | 149-150 | States a direct API caller "should poll `/api/jobs/current/status`... before answering," but that endpoint returns HTML with no job id anywhere except inside the flip button's `hx-vals` markup — a direct (non-browser) caller has no documented way to obtain `job_id` (WR-03, new in re-review) | ⚠️ Warning | Confirmed real by reading the referenced endpoints; none returns a job id in a stable, documented form. | No — DPLX-04 requires "the web provides the HTMX Continue button," which it does, fully tested end-to-end including a real browser click. This is a documentation gap for a use case (scripted direct API calls bypassing the browser) outside DPLX-04's literal text. |

None of the three new warnings are rated critical by the fresh code review (`25-REVIEW.md`: 0
critical, 3 warning, 5 info), and none, on inspection, falsifies a named roadmap success criterion
or a locked PLAN must-have. They are legitimate follow-up items, tracked below, not blockers to
this phase's completion.

### Human Verification Required

None. The CR-01 fix's browser-reachability was resolved programmatically in the prior verification
and remains so: `tests/test_browser.py::test_flip_continue_click_answers_the_waiting_job` drives a
real Chromium click through the rendered `hx-vals` attribute, re-run directly here and confirmed
passing. No new human-verification-only item was introduced by the gap-closure work.

### Gaps Summary

**No gaps remain.** The one gap from the prior verification — "Abort at the flip prompt cancels the
job" being unreliable due to CR-01's coordinator-arming/job-scoping race — is closed. I did not take
the code review's "Closed" disposition or the phase's own regression tests as sufficient evidence on
their own; I wrote an independent reproduction script (not derived from `tests/test_worker.py`) that
drives the real `ScanWorker`/`run_pipeline` through both falsifying scenarios from the prior
verification (a stale double-click Abort meant for a finished job landing on the next queued job; an
early Continue sent during pass A) and confirmed both now behave correctly. I additionally read the
fix's three load-bearing properties directly in `worker.py`: arm-before-persist ordering, a single
job-id snapshot compared against the coordinator's own job_id (no cross-job window), and the routes'
now-required `job_id` field flowing from the rendered button's `hx-vals` through a real browser click.

Three new warnings surfaced by the fresh code review (WR-01: worker shutdown doesn't resolve a live
flip wait; WR-02: the legacy warning no longer reaches stderr; WR-03: the flip endpoints require a
`job_id` no documented API surface exposes to a non-browser caller) were each independently confirmed
real by reading the code directly. None falsifies a named roadmap success criterion or a locked
PLAN must-have: WR-01 is a shutdown/reload edge case outside the normal-operation behaviour SC4
describes; WR-02 satisfies DPLX-02's literal text ("logs a deprecation warning") even though it
undercuts the visibility D-18 intended; WR-03 is a gap for headless direct-API callers, not for the
web UI DPLX-04 actually requires and that is tested end-to-end with a real browser. These are
recorded as warnings for follow-up, consistent with the fresh review's own severity classification
(0 critical, 3 warning, 5 info), and do not block this phase.

All quality gates are clean: `pytest -q` (1103 passed), `ruff check .` (no issues), `ty check` (all
checks passed), `pyrefly check src tests` (0 errors). No debt markers (`TBD`/`FIXME`/`XXX`) in any
file touched by the phase or its gap-closure waves.

---

_Verified: 2026-09-14T17:49:11Z_
_Verifier: Claude (gsd-verifier)_
