---
phase: 26-worker-and-web-robustness
verified: 2026-09-14T23:55:00Z
status: gaps_found
score: 4/5 roadmap success criteria verified (1 partially reopened by a fresh finding)
overrides_applied: 0
re_verification:
  previous_status: gaps_found
  previous_score: 4/5 roadmap success criteria verified (1 blocked)
  gaps_closed:
    - "A single, transient loop-level job-store failure (below `_DEGRADED_AFTER = 3`) that also broke the guard's own best-effort ERROR write no longer strands the row: `ScanWorker._flush_unrecorded_failures` retries it on every idle tick, degraded or not (26-15, CR-01), verified by `tests/test_worker.py::test_a_failed_best_effort_write_is_retried_until_it_lands` and `test_owed_failures_below_the_degraded_threshold_are_written_once_the_store_heals[1|2]`, and at the web layer by `tests/test_web_state_rendering.py::test_status_poll_reenables_the_scan_button_once_an_owed_failure_is_written`."
    - "A submit refused before any job row exists (worker DOWN/DEGRADED) is now written by `JobStore.create_rejected_job` in a single INSERT, so a mid-write store failure leaves no row at all instead of a PENDING row with no REJECTED marker (26-16, WR-01 half 1), verified by `tests/test_job.py::TestCreateRejectedJob` (trace-callback test pins exactly one INSERT, no UPDATE) and `tests/test_web_errors.py::test_a_refused_submit_never_leaves_an_active_row_when_the_store_fails[down|degraded]`."
    - "A submit refused after its row was created (queue full / down / degraded at `submit()`) whose `finish_job(ERROR, REJECTED)` write fails is now owed to the worker via `ScanWorker.owe_rejection` under a new `_unrecorded_lock`, and drained on the next idle tick (26-17, WR-01 half 2), verified by `tests/test_worker.py::TestOwedRejections` (3 tests) and `tests/test_web_errors.py::test_a_refused_submit_whose_rejection_write_fails_is_recorded_by_the_worker[queue_full|down|degraded]`."
    - "The previously locked-in test (`test_a_failed_best_effort_write_is_only_logged`, which asserted the stranded-active state as expected behaviour) is gone; its replacement asserts the row reaches ERROR without a restart."
  gaps_remaining:
    - "WR-10 (new, from the fresh 26-REVIEW.md re-review, independently reproduced here): a *persistent* (non-healing) job-store fault that starts below `_DEGRADED_AFTER` is never escalated to degraded through the idle-flush retry path introduced by 26-15, so `/health` stays 200 and the status area keeps showing the stuck row as the live job indefinitely (bounded only by two later hourly `prune` failures, ~2 hours) instead of promptly surfacing the fault the way a transient failure now does."
  regressions: []
gaps:
  - truth: "The browser (status area, Scan button, `/health`) reflects reality after a job-store exception, per the phase goal, not only for a transient fault that heals but for the persistent fault D-12's own motivating scenario (\"a freed disk heals on its own\") anticipates"
    status: partial
    reason: >
      26-15's fix (`_flush_unrecorded_failures` retried on every idle tick) correctly closes the
      scenario the previous verification blocked on: a failure that eventually heals now reaches
      ERROR and re-enables the Scan button without a restart. But `_idle_housekeeping`'s non-degraded
      branch (`worker.py:803-810`) swallows every failed retry at `logger.debug` and never counts it
      towards `_consecutive_loop_failures` -- by design, per 26-15's own must-have ("A failed retry of
      an owed write is logged at DEBUG and does not count towards degraded"). If the store fault does
      NOT heal (kept-full disk, read-only remount, a lock held by another process), the guard's
      original failure counted once, and nothing else can add to the count: the stuck row disables the
      Scan button, so no new job runs through the loop to fail again, and the flush's own repeated
      failures are explicitly excluded from `_record_loop_failure()`. Independently reproduced (not
      just taken from `26-REVIEW.md`'s WR-10): a scratch pytest using the project's own `_StoreFault`
      helper against a real `ScanWorker`/`JobStore` pair, with `update_state` failing once and
      `finish_job` failing on every call, ran for 2 real seconds and produced
      `finish_calls=100 health=HEALTHY row_state=PENDING is_active=True
      consecutive_loop_failures=1 latest_run_job=<the same stuck PENDING row>` -- i.e. `/health`
      reports 200, the status poll's `latest_run_job()` returns the stuck row itself (so the status
      area renders it, "Starting scan..." with the button disabled) with no expiry, for as long as the
      fault persists. The only bound is `_idle_housekeeping`'s hourly `prune` call: if `prune` also
      raises against the same broken store, that IS still counted (`worker.py:819-821`), so degraded is
      reached roughly two hourly ticks after the first failure (~2 hours), not never. This is the same
      symptom class CR-01 fixed (stuck row + lying health + disabled button, no restart-free recovery
      signal), reopened for the narrower but realistic precondition of a fault that does not heal on
      its own -- the literal scenario D-12's own comment ("a freed disk heals on its own") implies as
      the headline case being designed for. Consistent with how the previous verification treated an
      analogous narrower-than-the-enumerated-text reading of criterion 1 (appending "with the browser
      reflecting reality afterward" from the phase goal, not from ROBU-01's or criterion 1's own
      literal wording) as blocking, the same standard applies here: no enumerated ROBU-01..11
      requirement or roadmap success-criterion sentence literally demands this, but the phase goal's
      "the browser always reflects reality" does, and this reproduces a genuine, bounded-but-slow
      violation of it.
    artifacts:
      - path: "src/saneless/worker.py"
        issue: "_idle_housekeeping (802-810) logs a failed _flush_unrecorded_failures() retry at DEBUG and never calls _record_loop_failure() for it, so a persistently-failing store's retries never accumulate towards _DEGRADED_AFTER on their own; only a later hourly prune failure eventually does"
      - path: "tests/test_worker.py"
        issue: "No test exercises a store that never heals through the non-degraded flush path and asserts health eventually becomes DEGRADED; TestWorkerGuard's below-threshold tests all call finishes.heal() before asserting ERROR, so the persistent-fault path is untested"
    missing:
      - "Count a streak of failed flush ticks towards degraded (e.g. a `_failed_flush_ticks` counter incremented on each failed retry and folded into `_record_loop_failure()` after N consecutive failures, per 26-REVIEW.md's WR-10 suggested fix), so a persistent fault reaches degraded promptly instead of only via the next hourly prune failure"
      - "A test with a `finish_job`/`update_state` fault that never heals, asserting `worker.health` becomes `WorkerHealth.DEGRADED` within a bounded number of idle ticks (not hours)"
      - "Consider also flagging IN-08 (a refused post-submit attempt can render as a live PENDING job until its rejection lands) and WR-11 (a race in test_owed_failures_below_the_degraded_threshold_are_written_once_the_store_heals reading _unrecorded_failures without waiting for/locking the post-write delete) in the same remediation pass, since they touch the same code paths -- non-blocking on their own"
human_verification: []
---

# Phase 26: Worker and Web Robustness Verification Report

**Phase Goal:** The server survives everything the pipeline can throw at it and the browser always reflects reality — a guarded worker loop, 429 backpressure that is actually visible, blocking routes declared `def`, crash recovery at startup, and a server-owned Scan button served from vendored assets that work on an offline LAN — with the deployment and API docs updated in-phase
**Verified:** 2026-09-14T23:55:00Z
**Status:** gaps_found
**Re-verification:** Yes — after gap closure (26-15, 26-16, 26-17), referencing the previous `26-VERIFICATION.md` (status `gaps_found`, one blocking gap on success criterion 1: CR-01 + WR-01) and the fresh `26-REVIEW.md` (`538df02`, re-review after gap closure).

## Goal Achievement

### Observable Truths (Roadmap Success Criteria)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | A pipeline, job-store, or `prune` exception is logged with `exc_info` and the worker keeps serving the next job — with the browser reflecting reality afterward | ⚠️ PARTIAL (previously ✗ FAILED) | The three previously-blocking sub-issues are now closed and independently verified (see Re-verification below): a single transient loop-level failure below the degraded threshold now reaches ERROR and re-enables the Scan button without a restart (26-15), and both the pre-row and post-row rejected-submit paths can no longer strand a PENDING row without a REJECTED marker (26-16, 26-17). A **new, narrower** finding from the fresh review (WR-10) is independently reproduced: a *persistent* (non-healing) store fault starting below the degraded threshold is never escalated to degraded through the idle-flush retry path (only a later hourly `prune` failure would eventually do so, ~2 hours out), so `/health` stays 200 and the status area shows the stuck row as the live job indefinitely while the fault persists. See the gap entry for the full reproduction. |
| 2 | Submitting past a full queue returns 429 with `Retry-After` and a message the user can actually see in the status area, the event loop never blocks, and shutdown never blocks on the worker | ✓ VERIFIED (regression-checked) | No code touched by 26-15/16/17 affects this path. `routes.py:409-441`'s `_record_refused_submit`/`_reject_created_job` split (26-16/17) preserves the same `RequestRejected(rejection, refresh_history=written)` shape used by `render_error`'s `Retry-After`/`#status-message` retargeting. Full non-browser suite (1400 passed) and offline browser suite (48 passed) both green. |
| 3 | `/health` answers while a scan is running, and concurrent threadpool requests neither stampede the metadata cache nor mutate profiles mid-iteration | ✓ VERIFIED (regression-checked) | Untouched by the gap-closure plans (`cache.py`, route `def` handlers unchanged). Same WR-05 narrow-caveat noted previously (`cache.py` `invalidate` doesn't take the per-key lock) still applies and still doesn't falsify this criterion as worded. |
| 4 | Jobs left non-terminal by a crash are FAILED with a "server restarted" reason before the worker starts, and profiles are generated at startup from the config path that was actually loaded | ✓ VERIFIED (regression-checked) | `app.py` lifespan ordering untouched by 26-15/16/17. `_try_recover`'s `fail_active_jobs(RESTART_REASON)` call now runs after the shared `_flush_unrecorded_failures()` (26-15), preserving "the guard's own failures first ... so the restart recovery below cannot give them its text" (`worker.py:879-885`), confirmed by reading the code and by the still-passing `test_app_lifespan.py`/`TestWorkerDegradedHealth` suites. |
| 5 | A browser test in CI with no CDN egress clicks Scan, waits for the terminal status, and asserts `#scan-btn` is enabled again with no duplicate `id="scan-btn"` in the DOM and `app.js` deleted | ✓ VERIFIED (regression-checked) | `uv run pytest -m browser -q` → 48 passed locally, offline, run independently in this verification. `id="scan-btn"` still appears exactly once in `scan_button.html`; `static/` still contains only `app.css` and `vendor/` (htmx 2.0.8, Pico 2.1.1); `.github/workflows/ci.yml`'s `browser` job unchanged. |

**Score:** 4/5 roadmap success criteria fully verified; criterion 1's previously-blocking scenario (CR-01/WR-01) is closed, but a new, narrower finding (WR-10) reopens a residual gap in the same criterion under a persistent-fault precondition.

### Deferred Items

None. The WR-10 gap is not addressed by any later phase in the roadmap (Phase 27 "Configuration Strictness" and beyond do not mention job-store degraded-detection); it is a genuine open gap in this phase, not deferred work.

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/saneless/worker.py` — `_flush_unrecorded_failures` | Shared owed-write retry helper called from both the degraded and non-degraded idle paths | ✓ VERIFIED | `grep -c "self._flush_unrecorded_failures()"` = 2 (`_idle_housekeeping:805`, `_try_recover:882`); one INSERT-shaped loop `for job_id, owed in owed_writes:` at 846, snapshot and delete both under `_unrecorded_lock` (844, 851-853). Substantive and wired; exercised by 15 passing targeted tests (see Behavioral Spot-Checks). |
| `src/saneless/worker.py` — `owe_rejection` | Public method under `_unrecorded_lock`, shared dict with request threads | ✓ VERIFIED | `def owe_rejection(self, job_id: str, error: str) -> None` at line 280, body takes `self._unrecorded_lock` (301) and sets `(error, ErrorCategory.REJECTED)`. Called once, from `routes._reject_created_job` (routes.py:357). |
| `src/saneless/job.py` — `create_rejected_job` | Single-INSERT terminal REJECTED row | ✓ VERIFIED | `job.py:658`, `@_locked`, one `with self._conn:` block (695-718) executing `_INSERT` then the `_SELECT_BY_ID` read-back; no call to `create_job`/`finish_job`. `tests/test_job.py::TestCreateRejectedJob` (3 tests, all pass) pins the single-INSERT/no-UPDATE shape with a trace callback. |
| `src/saneless/web/routes.py` — `_record_refused_submit` / `_reject_created_job` | Replace `_record_rejected_submit`, split pre-row vs. post-row paths | ✓ VERIFIED | `_record_refused_submit` (277) calls only `create_rejected_job`; `_reject_created_job` (317) calls `finish_job` and falls back to `worker.owe_rejection(job_id, error)` on failure (357). `grep -rn "_record_rejected_submit" src tests docs` returns nothing. |
| `tests/test_worker.py` | Corrected best-effort test, below-threshold owed-write test, `TestOwedRejections` | ✓ VERIFIED | `test_a_failed_best_effort_write_is_only_logged` absent (0 matches); `test_a_failed_best_effort_write_is_retried_until_it_lands` present; `test_owed_failures_below_the_degraded_threshold_are_written_once_the_store_heals[1|2]` present and passing; `class TestOwedRejections` present with 3 passing tests. But (see gap) no test exercises a *never-healing* fault through this path. |
| `tests/test_web_state_rendering.py` / `tests/test_web_errors.py` | Web-level proof the Scan button re-enables after an owed write lands | ✓ VERIFIED | `test_status_poll_reenables_the_scan_button_once_an_owed_failure_is_written` and `test_a_refused_submit_whose_rejection_write_fails_is_recorded_by_the_worker[queue_full|down|degraded]` and `test_a_refused_submit_never_leaves_an_active_row_when_the_store_fails[down|degraded]` all present and passing. |
| `docs/explanation/architecture.md` | Failure-handling paragraph describes owed-write retries on every idle tick, degraded or not | ✓ VERIFIED | Line 56: "Whenever the worker has no job, every 5 seconds it retries any job-failure records it could not write earlier, degraded or not -- including the rejection of a refused scan that the web request could not record". Honestly scoped: does not claim protection for a fault that never heals, which matches the actual (gapped) behaviour rather than overclaiming. |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `ScanWorker._idle_housekeeping` (non-degraded branch) | `ScanWorker._flush_unrecorded_failures` | every idle tick, degraded or not | ✓ WIRED | Confirmed at `worker.py:803-810`; failure logged at DEBUG only, not counted (by design, per gap). |
| `ScanWorker._try_recover` | `ScanWorker._flush_unrecorded_failures` | after a successful probe, before `fail_active_jobs` | ✓ WIRED | Confirmed at `worker.py:879-885`. |
| `routes._record_refused_submit` | `JobStore.create_rejected_job` | pre-row refusal | ✓ WIRED | `routes.py:301-308`. |
| `routes._reject_created_job` | `ScanWorker.owe_rejection` | except branch after `finish_job` raises | ✓ WIRED | `routes.py:351-358`. |
| `ScanWorker.owe_rejection` | `ScanWorker._flush_unrecorded_failures` | shared `_unrecorded_failures` dict under `_unrecorded_lock` | ✓ WIRED | Confirmed lock usage at 4 sites (`owe_rejection`, `_best_effort_fail`, flush snapshot, flush delete). |
| `ScanWorker._idle_housekeeping` (persistent-fault case) | `ScanWorker._record_loop_failure` | none — this is the gap | ✗ NOT_WIRED | Flush failures never reach `_record_loop_failure()`; only a later `prune` failure (hourly) does. This is the crux of the WR-10 gap. |

### Data-Flow Trace (Level 4)

Traced the status-area / Scan-button rendering path under the WR-10 fault condition specifically, since this is where the gap is user-visible:

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|---------------------|--------|
| `scan_button.html` (`disabled` attr) | `job.is_active` | `JobStore.latest_run_job()` via the status-poll route | Reflects the actual DB row state — but that row state is itself stuck (see gap) | ⚠️ HOLLOW under a persistent fault: technically accurate to the (stuck) row, but the row never resolves, so the rendered "Starting scan..." is misleading about *why* |
| `/health` response | `worker.health` | `ScanWorker._consecutive_loop_failures` / `_degraded` | Independently reproduced: stays `HEALTHY` for the full 2-second/100-retry test window despite every `finish_job` call failing | ✗ DISCONNECTED from the actual store-write success rate under a persistent fault (bounded only by the ~hourly `prune` path) |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|---|---|---|---|
| Full non-browser suite passes | `uv run pytest -m "not browser and not sane_hardware" -q` | 1400 passed, 53 deselected | ✓ PASS |
| Browser suite passes fully offline | `uv run pytest -m browser -q` | 48 passed, 1405 deselected | ✓ PASS |
| All 6 gap-closure tests named in 26-15/16/17 pass together | `uv run pytest tests/test_worker.py tests/test_web_state_rendering.py tests/test_job.py tests/test_web_errors.py -q -k "best_effort_write_is_retried or below_the_degraded_threshold or reenables_the_scan_button or TestCreateRejectedJob or never_leaves_an_active_row or TestOwedRejections or rejection_write_fails_is_recorded_by_the_worker"` | 15 passed | ✓ PASS |
| Lint/format/type gates | `ruff check .`, `ruff format --check .`, `ty check`, `pyrefly check src tests` | all clean (0 errors; pyrefly's 4 informational warnings are in files this phase did not touch) | ✓ PASS |
| Debt-marker gate | `grep -n -E "TBD\|FIXME\|XXX"` over worker.py, job.py, routes.py, architecture.md, and the four gap-closure test files | 0 matches | ✓ PASS |
| **WR-10 independent reproduction** | Scratch pytest built directly on the project's own `_StoreFault` test helper and a real `ScanWorker`/`JobStore`, `update_state` failing call 1 only, `finish_job` failing every call, run for 2 real seconds | `finish_calls=100 health=HEALTHY row_state=PENDING is_active=True consecutive_loop_failures=1 latest_run_job=<the stuck row>` | ✗ FAIL (confirms gap; scratch file deleted after the run, not committed) |
| **WR-11 code inspection** (test race, not independently re-triggered with an injected delay) | Read `tests/test_worker.py:2264-2273` | `owed_after = dict(worker._unrecorded_failures)` is read immediately after `wait_for_state` on the last row without waiting for/locking the post-write delete in `_flush_unrecorded_failures` (`worker.py:851-853`); the sibling many-thread test (`test_owed_rejections_from_many_threads_are_all_written`, line ~2415-2421) *was* hardened with a `not worker._unrecorded_failures` wait, this one was not | ⚠️ Confirmed present by code inspection, consistent with 26-REVIEW.md WR-11 | ⚠️ WARNING (latent flakiness, non-blocking) |

### Probe Execution

Not applicable — no `scripts/*/tests/probe-*.sh` files exist in this repository and none are referenced by the phase's plans or summaries.

### Requirements Coverage

| Requirement | Source Plan | Status | Evidence |
|---|---|---|---|
| ROBU-01 | 26-01, 26-06, 26-15 | ✓ SATISFIED (literal text); ⚠️ residual gap against the broader phase goal | Literal text ("survives any exception ... logged with exc_info and the worker keeps serving") holds even under the WR-10 fault — the worker thread never dies and every failure is logged with `exc_info`. The phase-goal-level "browser reflects reality" aspiration has the WR-10 residual gap documented above. |
| ROBU-02 | 26-01, 26-04, 26-05, 26-09, 26-10, 26-13, 26-16, 26-17 | ✓ SATISFIED | 429/503 + visible message + non-blocking submit/shutdown; both rejected-submit paths (pre-row and post-row) now atomic-or-owed, closing WR-01. |
| ROBU-03 | 26-04 | ✓ SATISFIED (regression-checked) | Unchanged by gap closure. |
| ROBU-04 | 26-11, 26-12 | ✓ SATISFIED (regression-checked) | Unchanged; browser suite green. |
| ROBU-05 | 26-04, 26-08, 26-10 | ✓ SATISFIED (regression-checked) | Unchanged. |
| ROBU-06 | 26-01, 26-09 | ✓ SATISFIED (regression-checked) | `fail_active_jobs` ordering relative to `_flush_unrecorded_failures` inside `_try_recover` preserved correctly (26-15 traced this explicitly and 26-REVIEW.md confirmed it sound). |
| ROBU-07 | 26-02, 26-08 | ✓ SATISFIED (regression-checked, WR-03/WR-04 caveats carried forward, unchanged and out of scope for this gap-closure wave) | |
| ROBU-08 | 26-01, 26-10, 26-11 | ✓ SATISFIED (regression-checked) | |
| ROBU-09 | 26-03, 26-12 | ✓ SATISFIED (regression-checked) | |
| ROBU-10 | 26-07, 26-13, 26-14 | ✓ SATISFIED (WR-09 doc caveat carried forward, unchanged) | |
| ROBU-11 | 26-12 | ✓ SATISFIED (regression-checked) | 48/48 offline browser tests pass; CI `browser` job present. |

All 11 ROBU-01..ROBU-11 requirement IDs are claimed by at least one plan; none are orphaned. No requirement's literal text is BLOCKED; the residual gap sits at the phase-goal level, attached to ROBU-01/criterion 1 for consistency with how the previous verification scoped this same finding class.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `src/saneless/worker.py` | 801-810 | A persistent (non-healing) flush failure is logged at DEBUG only and never counted towards `_DEGRADED_AFTER`, so `/health` and the status area misrepresent reality for as long as the fault lasts (bounded only by an hourly `prune` failure) | 🛑 Blocker | WR-10 — see gap |
| `tests/test_worker.py` | 2264-2273 | `owed_after = dict(worker._unrecorded_failures)` read without waiting for/locking the flush's post-write delete; latent race, reproduced by the review with an injected 50 ms delay (not re-triggered here) | ⚠️ Warning | WR-11 — non-blocking test flakiness, worth fixing alongside WR-10 |
| `src/saneless/web/routes.py` | 412-441 | A refused post-submit attempt can render as a live PENDING job in the status area until its rejection lands (short window for a healthy store, longer while owed) | ⚠️ Warning | IN-08 — narrower than WR-10, self-heals on the very next idle tick in the common case |
| `src/saneless/web/cache.py` | 105-113 | `invalidate` does not take the per-key lock | ⚠️ Warning | WR-05 — carried forward unchanged, out of scope for this gap-closure wave |
| `src/saneless/worker.py` | 1032-1042, 717-722, 773-786, 846-853 | A failed success-path write (a document already accepted by Paperless) is now guaranteed to eventually be recorded as `ERROR` rather than staying active until restart, which the review flags as an aggravation of the pre-existing WR-02 | ⚠️ Warning | WR-02 (aggravated) — carried forward, explicitly out of scope for 26-15/16/17 per those plans' own `<objective>` sections |
| `src/saneless/worker.py` | 592-607, 599-607 | WR-03/WR-04 (`default` profile drift after restart; narrow persist-exception catch) | ⚠️ Warning | Carried forward unchanged, out of scope |
| `src/saneless/web/errors.py` | 166-181, 198-225 | WR-08 (missing `Allow` header on 405), WR-07 (raw path logged with `%s`) | ⚠️ Warning | Carried forward unchanged, out of scope |
| `docs/reference/web-api.md` | Cross-site section | WR-09 (DNS-rebinding caveat missing) | ⚠️ Warning | Carried forward unchanged, out of scope |
| No `TBD`/`FIXME`/`XXX` markers found in phase-modified files | — | — | — | Debt-marker gate: clean |

### Human Verification Required

None. Every finding in this report — including the new WR-10 reproduction — was established by static code reading, the existing automated test suite (1400 non-browser + 48 offline browser tests, all passing), and an independent scratch reproduction built directly on the codebase's own test helpers (not committed). No browser-only visual/UX judgment call remains open, consistent with the project's Playwright-first policy.

### Gaps Summary

The gap-closure wave (26-15, 26-16, 26-17) fully and verifiably closes everything the previous verification's `missing:` list asked for: owed job-store writes (both the loop guard's own failed ERROR writes and now, separately, failed rejected-submit writes) are retried on every idle tick regardless of degraded state; the previously locked-in test that blessed the stranded-active state is corrected; refused-before-row submits are now a single INSERT that can never leave a PENDING row with no marker; and a new below-threshold test proves a 1- or 2-failure stranded row reaches ERROR without a restart. All of this is independently re-verified here, not just re-read from the SUMMARY.md files: 15 targeted tests pass, the full 1400-test non-browser suite and 48-test offline browser suite are green, and ruff/ruff-format/ty/pyrefly are all clean.

The fresh `26-REVIEW.md` re-review (`538df02`) surfaces one new finding, WR-10, that this verification independently reproduces rather than takes on trust: the fix that makes a *transient* below-threshold store fault self-heal deliberately excludes every failed retry from counting towards `_consecutive_loop_failures` (a 26-15 design choice, justified as "the failure that created the debt was already counted"). That exclusion is unconditional, so a *persistent* fault of the same kind — the disk staying full, rather than briefly failing and healing, which is the scenario D-12's own "a freed disk heals on its own" comment names as the headline case — never accumulates towards degraded through this path. `/health` reports 200 and the status area shows the stuck row as the live job indefinitely, bounded only by a much slower, incidental path (two hourly `prune` failures, roughly two hours). This reproduces the same class of user-facing symptom CR-01 fixed (a stuck row that disables the Scan button and a `/health` that doesn't tell the truth), for a narrower but realistic precondition the closed fix does not cover. Following the same interpretive standard the previous verification applied to reach its own blocking conclusion (reading "the browser always reflects reality" from the phase goal into criterion 1, beyond that criterion's own narrower literal text), this is treated here as a genuine, if narrower, unresolved gap rather than a cosmetic one — not because any single enumerated ROBU requirement's literal wording demands it, but because the phase's own stated goal does, and the violation is concretely reproducible rather than theoretical.

This is a materially smaller gap than the one this verification closes: it requires a persistent rather than transient fault, it is bounded (not literally forever), and the operator does get one WARNING-level log line when the debt is first created. The suggested fix is narrow and already sketched in `26-REVIEW.md`'s WR-10 (count a streak of failed flush ticks and fold it into `_record_loop_failure()` after N), and should be paired with a test using a store that never heals, asserting `DEGRADED` within a bounded number of ticks. WR-11 (a latent test race reading `_unrecorded_failures` without waiting for the flush's delete) and IN-08 (a narrow window where a refused-but-not-yet-recorded post-submit job renders as live) touch the same code and are worth closing in the same pass, but are not blocking on their own. WR-02 (aggravated), WR-03, WR-04, WR-05, WR-06, WR-07, WR-08, WR-09 and IN-01 through IN-07 are unchanged from the previous verification's assessment: real but narrower, out of the explicit scope of 26-15/16/17, and non-blocking against the roadmap's five numbered success criteria as worded.

---

_Verified: 2026-09-14T23:55:00Z_
_Verifier: Claude (gsd-verifier)_
