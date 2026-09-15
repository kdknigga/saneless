---
phase: 26-worker-and-web-robustness
verified: 2026-09-15T05:30:00Z
status: passed
score: 5/5 roadmap success criteria verified
overrides_applied: 0
re_verification:
  previous_status: gaps_found
  previous_score: 4/5 roadmap success criteria verified (1 partially reopened by a fresh finding)
  gaps_closed:
    - "WR-10: a persistent (non-healing) job-store fault below `_DEGRADED_AFTER` that starts below the degraded threshold is now escalated to degraded through the idle-flush retry path within a bounded number of idle ticks, not only via an hourly `prune` failure. `_idle_housekeeping`'s non-degraded branch now counts a `_failed_flush_ticks` streak, kept apart from `_consecutive_loop_failures`, and sets `_degraded` once the streak reaches `_OWED_RETRY_DEGRADED_AFTER` (3), read at call time (26-18). Verified by `tests/test_worker.py::TestOwedWriteStreak` (3 tests: bounded degrade for a guard debt and a rejection debt, recovery, sub-threshold heal never degrades) and `tests/test_web_state_rendering.py::test_health_reports_the_job_store_failing_while_an_owed_failure_cannot_be_written`, and independently reproduced here with a scratch pytest against a real `ScanWorker`/`JobStore` pair (see Behavioral Spot-Checks): a `finish_job` that never heals reaches `WorkerHealth.DEGRADED` with `streak=3, loop_failures=0` well within the 2 s test budget (production bound ~15 s at the 5 s idle tick)."
    - "WR-11: the below-threshold heal test's read of `worker._unrecorded_failures` immediately after `wait_for_state`, without waiting for or locking the flush's post-write delete, is fixed: it now waits `_wait_until(lambda: not worker._unrecorded_failures, ...)` and copies the dict under `worker._unrecorded_lock` before asserting it is empty (26-18). Confirmed by reading `tests/test_worker.py:2284-2287`."
    - "IN-08: a refused post-submit attempt whose REJECTED write failed and is now owed to the worker no longer renders as the live job in the status area. `JobStore.latest_run_job(exclude_ids=...)` (job.py:891) and `ScanWorker.owed_rejection_ids()` (worker.py:318) are wired through `_current_or_recent_job` (`routes.py:128`: `job_store.latest_run_job(exclude_ids=worker.owed_rejection_ids())`) (26-19). Verified by `tests/test_job.py::test_latest_run_job_skips_excluded_ids` / `test_latest_run_job_exclusions_combine_with_the_rejection_skip` / `test_latest_run_job_ignores_excluded_ids_that_match_no_row`, `tests/test_worker.py::test_owed_rejection_ids_lists_only_rejections_still_owed`, and `tests/test_web_errors.py::test_a_refused_attempt_whose_rejection_is_still_owed_is_not_shown_as_the_live_job[queue_full|down|degraded]`."
  gaps_remaining: []
  regressions: []
gaps: []
human_verification: []
---

# Phase 26: Worker and Web Robustness Verification Report

**Phase Goal:** The server survives everything the pipeline can throw at it and the browser always reflects reality — a guarded worker loop, 429 backpressure that is actually visible, blocking routes declared `def`, crash recovery at startup, and a server-owned Scan button served from vendored assets that work on an offline LAN — with the deployment and API docs updated in-phase
**Verified:** 2026-09-15T05:30:00Z
**Status:** passed
**Re-verification:** Yes — after gap-closure round 2 (26-18, 26-19), referencing the previous `26-VERIFICATION.md` (status `gaps_found`, one blocking gap WR-10 on success criterion 1) and the fresh `26-REVIEW.md` (`03d9ea2`, re-review after 26-18/19).

## Goal Achievement

### Observable Truths (Roadmap Success Criteria)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | A pipeline, job-store, or `prune` exception is logged with `exc_info` and the worker keeps serving the next job — with the browser reflecting reality afterward | ✓ VERIFIED (previously ⚠️ PARTIAL, blocked on WR-10) | WR-10 is closed: `_idle_housekeeping`'s non-degraded branch now increments `self._failed_flush_ticks` on a failed retry, logs WARNING with `exc_info` on the first failure of a streak and DEBUG afterward, and sets `self._degraded` once the streak reaches `_OWED_RETRY_DEGRADED_AFTER` (3), independent of `_consecutive_loop_failures` (`worker.py:849-868`). `_try_recover` resets both counters before `_degraded.clear()` (`worker.py:890-891`). Independently reproduced here (not taken on trust): a scratch pytest with a never-healing `finish_job` fault reached `WorkerHealth.DEGRADED` with `streak=3, loop_failures=0, finish_calls=3` well inside a 2 s budget — matching the production ~15 s bound, a dramatic improvement over the previous ~2 hour silent window. |
| 2 | Submitting past a full queue returns 429 with `Retry-After` and a message the user can actually see in the status area, the event loop never blocks, and shutdown never blocks on the worker | ✓ VERIFIED (regression-checked) | No code touched by 26-18/19 affects this path; full suite green (see Behavioral Spot-Checks). |
| 3 | `/health` answers while a scan is running, and concurrent threadpool requests neither stampede the metadata cache nor mutate profiles mid-iteration | ✓ VERIFIED (regression-checked) | Untouched by 26-18/19 (`cache.py`, route `def` handlers unchanged). Same WR-05 narrow caveat (`cache.py` `invalidate` doesn't take the per-key lock) carried forward, non-blocking as before. |
| 4 | Jobs left non-terminal by a crash are FAILED with a "server restarted" reason before the worker starts, and profiles are generated at startup from the config path that was actually loaded | ✓ VERIFIED (regression-checked) | `app.py` lifespan ordering and `_try_recover`'s `fail_active_jobs(RESTART_REASON)` call untouched in ordering by 26-18/19; `tests/test_app_lifespan.py` still green. |
| 5 | A browser test in CI with no CDN egress clicks Scan, waits for the terminal status, and asserts `#scan-btn` is enabled again with no duplicate `id="scan-btn"` in the DOM and `app.js` deleted | ✓ VERIFIED (regression-checked) | `uv run pytest -m browser -q` → 48 passed, run independently in this verification, fully offline. |

**Score:** 5/5 roadmap success criteria verified. The previously-blocking WR-10 gap on criterion 1 is closed and independently re-reproduced with the fix in place (the same reproduction technique that established the gap now shows it resolved).

### Deferred Items

None.

### Gap-Closure Verification (WR-10, WR-11, IN-08)

| Gap | Claim (SUMMARY) | Independently Verified | Status |
|---|---|---|---|
| WR-10 | A streak of `_OWED_RETRY_DEGRADED_AFTER` (3) failed idle-tick owed-write retries degrades the worker, separate from `_consecutive_loop_failures`; a healed retry resets the streak to 0; recovery clears both counters before `_degraded.clear()` | Read `src/saneless/worker.py:849-868` (streak increment, WARNING/DEBUG split, degrade at threshold) and `:890-891` (reset order in `_try_recover`) directly. Ran `tests/test_worker.py::TestOwedWriteStreak` (3 tests) and the web `/health` 503 test — all pass. Built and ran an independent scratch pytest (not committed, deleted after use) against a real `ScanWorker`/`JobStore`: a `finish_job` fault that never heals reached `DEGRADED` with `streak=3, loop_failures=0` in well under the 2 s test budget. | ✓ CLOSED |
| WR-11 | `test_owed_failures_below_the_degraded_threshold_are_written_once_the_store_heals` waits for the owed dict to empty and reads it under `_unrecorded_lock` before asserting `{}` | `grep -n -B3 "owed_after = dict(worker._unrecorded_failures)"` at `tests/test_worker.py:2284-2287` shows `assert _wait_until(lambda: not worker._unrecorded_failures, _STATE_BUDGET)` immediately followed by `with worker._unrecorded_lock:` before the dict copy | ✓ CLOSED |
| IN-08 | A refused post-submit attempt whose REJECTED write is still owed is excluded from `latest_run_job()` via `owed_rejection_ids()`, so it never renders as the live job | `job.py:891` (`def latest_run_job(self, exclude_ids: Collection[str] = ()) -> Job \| None`), `worker.py:318` (`def owed_rejection_ids(self) -> frozenset[str]`), `routes.py:128` (`job_store.latest_run_job(exclude_ids=worker.owed_rejection_ids())`) all confirmed by direct read. Ran the 7 new tests (`test_job.py` ×3, `test_worker.py` ×1, `test_web_errors.py` ×3 parametrized) — all pass. | ✓ CLOSED |

### New Finding From the Fresh Review: IN-09 — Assessed Non-Blocking

`26-REVIEW.md` (`03d9ea2`) raises IN-09: `_run`'s success branch resets `_consecutive_loop_failures` after a job whose writes all landed (`worker.py:768`) but never resets `_failed_flush_ticks`, so a streak of failed owed-write retries can span across a job that proved the store accepts writes. This is inconsistent with the new `docs/explanation/architecture.md:56` sentence "a store that heals sooner never degrades it."

**Independently reproduced** (not taken on trust from the review): a scratch pytest (built and deleted, not committed) drove a `ScanWorker`/`JobStore` pair through the exact sequence IN-09 describes — an owed rejection, 2 failed idle-tick flush retries (below the 3-tick threshold), then the store healed and a full job ran end-to-end successfully (`finished_state=DONE`, `loop_failures_after_job=0`), then the store broke again for exactly one more tick. Result: `streak_before_job=2`, `streak_after_job=2` (unchanged by the successful job — confirms the bug), and the single subsequent failure alone pushed the streak to 3 and set `DEGRADED` (`degraded_after_one_more_failure=True`), even though only 1 of the 3 counted failures happened after the last proof the store was healthy.

**Assessment — does this falsify a roadmap success criterion or the phase goal? No, applying the same standard the previous verifications used.** The previous verification treated WR-10 as blocking because it was a *sustained, indefinite* misrepresentation: a persistent fault left `/health` reporting 200 and the status area showing a stuck job for as long as the fault lasted (bounded only by an ~2 hour fallback path) — the dangerous direction, where the browser lies "healthy" while broken. IN-09 is the opposite direction and is self-limiting:

- It is a **false-positive** (over-cautious) trigger, not a false-negative: the worst outcome is `/health` briefly reporting `503`/degraded and the Scan button disabling for a store that is, at that exact moment, actually fine.
- It is **bounded to about one idle tick** (~5 s in production) in the realistic case: as soon as the worker sees `_degraded.is_set()`, the very next tick runs `_try_recover()`, which probes and flushes immediately; if the store is genuinely healthy, that tick clears degraded and resets both counters right away. It does not compound into an indefinite or hours-long window the way WR-10's gap did.
- It requires an **unusual, specific interleaving**: an owed rejection debt already in flight, a flaky/intermittent (not merely one-and-done transient, and not persistent) fault, and a job that happens to complete cleanly in between two failed flush ticks. This is narrower and rarer than either of the scenarios WR-10 or CR-01 fixed.
- `26-REVIEW.md` itself classifies IN-09 as a **warning**, not critical, consistent with how WR-02 through WR-09 were carried forward as non-blocking in both previous verifications.

The one-line fix the review suggests (`self._failed_flush_ticks = 0` alongside `self._consecutive_loop_failures = 0` in `_run`'s success branch) is cheap and the `docs/explanation/architecture.md:56` sentence is technically imprecise until it lands, but neither the imprecision nor the underlying behavior rises to the level of falsifying "the browser always reflects reality" the way the closed WR-10 gap did — a spurious, self-correcting ~5 s degraded blip under a narrow precondition is materially different in kind and severity from an indefinite silent lie. Classified as a **non-blocking WARNING**, carried forward alongside WR-02–WR-09, IN-01–IN-07, IN-10 and IN-11, none of which are required for this phase's roadmap success criteria or its ROBU-01..11 requirements as literally worded.

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/saneless/worker.py` — `_OWED_RETRY_DEGRADED_AFTER` / `_failed_flush_ticks` | Streak counter and threshold that degrade the worker on a persistent owed-write fault | ✓ VERIFIED | `_OWED_RETRY_DEGRADED_AFTER: Final = 3` present once (`worker.py:81`); `self._failed_flush_ticks = 0` appears 3× (`__init__`, `_idle_housekeeping` else-branch, `_try_recover`); `self._failed_flush_ticks >= _OWED_RETRY_DEGRADED_AFTER` gate present once; `_record_loop_failure()` still called from exactly 2 sites (unchanged, confirming the streak is not folded into the loop-failure count). |
| `src/saneless/worker.py` — `owed_rejection_ids` | Public method exposing only owed REJECTED ids under the lock | ✓ VERIFIED | `def owed_rejection_ids(self) -> frozenset[str]` at `worker.py:318`, directly after `owe_rejection`. |
| `src/saneless/job.py` — `latest_run_job(exclude_ids=...)` | Bound-parameter exclusion, no interpolated ids | ✓ VERIFIED | `def latest_run_job(self, exclude_ids: Collection[str] = ()) -> Job \| None` at `job.py:891`; `_SELECT_LATEST_RUN` ends `LIMIT ?` (`job.py:175`), bound with `(ErrorCategory.REJECTED.value, len(skip) + 1)` (`job.py:922`). |
| `src/saneless/web/routes.py` — `_current_or_recent_job` | Fallback skips owed rejections | ✓ VERIFIED | `job_store.latest_run_job(exclude_ids=worker.owed_rejection_ids())` at `routes.py:128`, the sole call site of `latest_run_job(`. |
| `tests/test_worker.py` — `TestOwedWriteStreak` | Persistent-fault degrade, recovery, sub-threshold-never-degrades | ✓ VERIFIED | Class present at `worker.py:2509` (test file), between `TestOwedRejections` and `_DEGRADING_JOBS`; all tests pass. |
| `tests/test_worker.py` — `test_owed_rejection_ids_lists_only_rejections_still_owed` | Owed set excludes guard debts, includes only REJECTED entries | ✓ VERIFIED | Present in `TestOwedRejections`; passes. |
| `tests/test_web_state_rendering.py` / `tests/test_web_errors.py` | Web-level proof of `/health` 503 under a persistent fault, and that an owed-but-unwritten rejection never renders as the live job | ✓ VERIFIED | `test_health_reports_the_job_store_failing_while_an_owed_failure_cannot_be_written` and `test_a_refused_attempt_whose_rejection_is_still_owed_is_not_shown_as_the_live_job[queue_full\|down\|degraded]` both present and pass. |
| `docs/explanation/architecture.md` / `docs/reference/web-api.md` | Describe the 3-tick owed-write retry streak that degrades the worker | ✓ VERIFIED | "three idle ticks in a row" appears once in each doc (`architecture.md:56`, `web-api.md:45`); the `web-api.md` degraded-conditions sentence now lists three conditions. Note: the architecture.md clause "a store that heals sooner never degrades it" is imprecise per IN-09 (see above) — a documentation accuracy nit, not an artifact failure. |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `ScanWorker._idle_housekeeping` (non-degraded branch) | `ScanWorker._degraded` | `_failed_flush_ticks >= _OWED_RETRY_DEGRADED_AFTER` | ✓ WIRED | Confirmed at `worker.py:865-871`; closes the WR-10 gap. |
| `ScanWorker._try_recover` | `ScanWorker._failed_flush_ticks` reset | reset alongside `_consecutive_loop_failures`, before `_degraded.clear()` | ✓ WIRED | Confirmed order at `worker.py:890-892`. |
| `routes._current_or_recent_job` | `ScanWorker.owed_rejection_ids` | `exclude_ids` argument to `JobStore.latest_run_job` | ✓ WIRED | `routes.py:128`. |
| `ScanWorker.owed_rejection_ids` | `ScanWorker._unrecorded_failures` | snapshot under `_unrecorded_lock` filtered to `ErrorCategory.REJECTED` | ✓ WIRED | Confirmed at `worker.py:318-330`. |
| `ScanWorker._run` (success branch) | `ScanWorker._failed_flush_ticks` reset | none — this is IN-09, assessed non-blocking above | ✗ NOT_WIRED | `worker.py:768` resets only `_consecutive_loop_failures`; `_failed_flush_ticks` is untouched by a successful job. |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|---|---|---|---|
| Full non-browser suite passes | `uv run pytest -m "not browser and not sane_hardware" -q` | 1412 passed | ✓ PASS |
| Browser suite passes fully offline | `uv run pytest -m browser -q` | 48 passed | ✓ PASS |
| Lint/format/type gates | `ruff check .`, `ruff format --check .`, `ty check`, `pyrefly check src tests` | all clean (0 errors; pyrefly's 4 informational warnings are in files this phase did not touch, unchanged from the previous verification) | ✓ PASS |
| Debt-marker gate | `grep -n -E "TBD\|FIXME\|XXX"` over worker.py, job.py, routes.py, the phase's test files, and both docs files | 0 matches | ✓ PASS |
| **WR-10 independent reproduction** | Scratch pytest (not committed, deleted after use) built directly against a real `ScanWorker`/`JobStore`: `finish_job` wrapped to always raise `sqlite3.OperationalError`, an owed rejection queued, worker polled for up to 2 s | `degraded=True streak=3 loop_failures=0 finish_calls=3 row_state=PENDING health_after=DOWN` | ✓ PASS (confirms the gap is closed) |
| **IN-09 independent reproduction** | Scratch pytest (not committed, deleted after use): 2 failed flush ticks, store healed, a full job ran DONE, store broke again for one more tick | `streak_before_job=2 loop_failures_after_job=0 streak_after_job=2 degraded_after_one_more_failure=True streak_final=3 finished_state=DONE` | ✗ FAIL (confirms IN-09 is real; assessed non-blocking above) |

### Probe Execution

Not applicable — no `scripts/*/tests/probe-*.sh` files exist in this repository and none are referenced by the phase's plans or summaries.

### Requirements Coverage

| Requirement | Source Plan | Status | Evidence |
|---|---|---|---|
| ROBU-01 | 26-01, 26-06, 26-15, 26-18 | ✓ SATISFIED | WR-10 closed; the loop-guard, its DEBUG/WARNING logging, and the new owed-write retry streak all guard against the worker thread ending or lying about health. The narrower IN-09 finding (a self-correcting false-positive degrade under a rare interleaving) does not falsify the requirement's literal text and is tracked as a non-blocking warning. |
| ROBU-02 | 26-01, 26-04, 26-05, 26-09, 26-10, 26-13, 26-16, 26-17 | ✓ SATISFIED (regression-checked) | Unchanged by 26-18/19. |
| ROBU-03 | 26-04 | ✓ SATISFIED (regression-checked) | Unchanged. |
| ROBU-04 | 26-11, 26-12 | ✓ SATISFIED (regression-checked) | Unchanged; browser suite green. |
| ROBU-05 | 26-04, 26-08, 26-10 | ✓ SATISFIED (regression-checked) | Unchanged. |
| ROBU-06 | 26-01, 26-09 | ✓ SATISFIED (regression-checked) | Unchanged. |
| ROBU-07 | 26-02, 26-08 | ✓ SATISFIED (regression-checked, WR-03/WR-04 caveats carried forward) | |
| ROBU-08 | 26-01, 26-10, 26-11 | ✓ SATISFIED (regression-checked) | |
| ROBU-09 | 26-03, 26-12 | ✓ SATISFIED (regression-checked) | |
| ROBU-10 | 26-07, 26-13, 26-14 | ✓ SATISFIED (WR-09 doc caveat carried forward) | |
| ROBU-11 | 26-12 | ✓ SATISFIED (regression-checked) | 48/48 offline browser tests pass. |

All 11 ROBU-01..ROBU-11 requirement IDs are claimed by at least one plan (26-18 additionally claims ROBU-01; 26-19 additionally claims ROBU-02, ROBU-04); none are orphaned. No requirement's literal text is BLOCKED.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `src/saneless/worker.py` | 768, 849-868 | `_failed_flush_ticks` is not reset when a job's writes all land, so the streak can span a proof the store is healthy (IN-09, new) | ⚠️ Warning | Self-correcting false-positive degrade under a narrow interleaving; see assessment above. One-line fix available, not applied in this phase. |
| `src/saneless/web/cache.py` | ~105-113 | `invalidate` does not take the per-key lock | ⚠️ Warning | WR-05 — carried forward unchanged, out of scope for this gap-closure wave. |
| `src/saneless/worker.py` | multiple | WR-02 (aggravated) — a failed success-path write is now guaranteed to eventually reach ERROR rather than stay active until restart | ⚠️ Warning | Carried forward, explicitly out of scope for 26-15/16/17/18/19's own `<objective>` sections. |
| `src/saneless/worker.py` | ~592-607 | WR-03/WR-04 (`default` profile drift after restart; narrow persist-exception catch) | ⚠️ Warning | Carried forward unchanged, out of scope. |
| `src/saneless/web/errors.py` | ~166-225 | WR-08 (missing `Allow` header on 405), WR-07 (raw path logged with `%s`) | ⚠️ Warning | Carried forward unchanged, out of scope. |
| `docs/reference/web-api.md` | Cross-site section | WR-09 (DNS-rebinding caveat missing) | ⚠️ Warning | Carried forward unchanged, out of scope. |
| No `TBD`/`FIXME`/`XXX` markers found in phase-modified files | — | — | — | Debt-marker gate: clean |

### Human Verification Required

None. Every finding in this report was established by static code reading, the existing automated test suite (1412 non-browser + 48 offline browser tests, all passing, run independently in this verification), and two independent scratch reproductions built directly on the codebase's own test patterns (not committed, deleted after use). No browser-only visual/UX judgment call remains open, consistent with the project's Playwright-first policy.

### Gaps Summary

The gap-closure round (26-18, 26-19) fully and verifiably closes everything the previous verification's blocking gap (WR-10) asked for, plus the two items it flagged as worth closing in the same pass (WR-11, IN-08). All three closures are independently re-verified here, not taken from the SUMMARY.md files on trust: the WR-10 fix was rebuilt from scratch against a real `ScanWorker`/`JobStore` and shown to degrade within a bounded window on a never-healing fault; the WR-11 test-race fix and the IN-08 exclusion wiring were confirmed by direct code reading plus the full targeted and regression test suites, all green (1412 non-browser + 48 browser tests, ruff/ruff-format/ty/pyrefly all clean, no debt markers).

The fresh `26-REVIEW.md` (`03d9ea2`) surfaces one new finding, IN-09: the owed-write retry streak is not reset when a job's writes all land, so it can span a job that proved the store healthy, occasionally causing a spurious, self-correcting ~5 s "degraded" blip under a narrow interleaving (an owed debt, an intermittent fault, and a job that happens to succeed in between two failed ticks). This is independently reproduced here, not taken on trust. Applying the same standard the previous verifications used to judge severity — the direction and duration of the "browser lies" — this is assessed as non-blocking: it is a false-positive (over-cautious) error that self-corrects within about one idle tick in the realistic case, the opposite and materially less severe direction from WR-10's indefinite false-negative (browser reporting healthy while actually broken for hours). It is carried forward as a WARNING alongside WR-02 through WR-09 and IN-01, IN-02, IN-03, IN-04, IN-05, IN-06, IN-07, IN-10 and IN-11 — none of which falsify a roadmap success criterion or a ROBU-01..11 requirement as worded, and none of which were treated as blocking in the previous verification either.

With WR-10 closed and no other blocking finding, all 5 roadmap success criteria are verified and the phase goal is achieved. Phase 26 is ready to proceed.

---

_Verified: 2026-09-15T05:30:00Z_
_Verifier: Claude (gsd-verifier)_
