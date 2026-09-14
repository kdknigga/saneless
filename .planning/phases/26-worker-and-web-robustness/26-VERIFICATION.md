---
phase: 26-worker-and-web-robustness
verified: 2026-09-14T22:20:28Z
status: gaps_found
score: 4/5 roadmap success criteria verified (1 blocked)
overrides_applied: 0
gaps:
  - truth: "A pipeline, job-store, or `prune` exception is logged with `exc_info` and the worker keeps serving the next job — with the browser reflecting reality afterward"
    status: failed
    reason: >
      The worker thread itself never dies (ROBU-01's literal text holds), but a single
      transient job-store write failure (one `finish_job`/`update_state` raise, store heals
      immediately after) leaves that job's row permanently active. `_idle_housekeeping` only
      calls `_try_recover` — the only code that drains `_unrecorded_failures` — when
      `self._degraded.is_set()`. `_DEGRADED_AFTER = 3` and a single loop-level failure resets
      to 0 on the next job's success (`worker.py` `_run`, `else: self._consecutive_loop_failures
      = 0`), so degraded is never reached and the stranded row is never reconciled without a
      process restart. Reproduced by reading `tests/test_worker.py:2173-2212`
      (`test_a_failed_best_effort_write_is_only_logged`), which submits one job that fails
      both `update_state` and `finish_job`, then a second job that succeeds, and asserts
      `stranded.is_active` is still `True` with the comment "only the recovery probe (D-12)
      reconciles this row" — but the probe never ran because the worker never degraded (only
      1 of 3 failures). This directly breaks the phase goal ("the browser always reflects
      reality") and D-12's contract ("Recovery needs no user scan, so a freed disk heals on
      its own"): `#status-area` polls the stranded row forever, `scan_button.html` renders it
      `disabled` forever (`job.is_active`), and `/health` stays 200 throughout, so nothing
      signals the operator. This is CR-01 in `26-REVIEW.md`, independently reproduced here by
      reading `worker.py:756-816` and the locked-in test; no fix commit exists after the
      review (`git log` shows `docs(26): add code review report` as the latest commit touching
      the phase, no follow-up to `worker.py`).
    artifacts:
      - path: "src/saneless/worker.py"
        issue: "_idle_housekeeping (lines 756-776) only calls _try_recover when self._degraded.is_set(); below-threshold unrecorded failures in _unrecorded_failures are never retried"
      - path: "tests/test_worker.py"
        issue: "test_a_failed_best_effort_write_is_only_logged (lines 2173-2212) asserts the stranded-active state as expected behaviour instead of catching the bug"
      - path: "src/saneless/web/routes.py"
        issue: "_record_rejected_submit (lines 277-320, WR-01 in 26-REVIEW.md) has the same two-transaction gap for rejected-submit rows: if finish_job raises after create_job committed, the row is stranded PENDING with no REJECTED marker and no worker-side reconciliation"
    missing:
      - "Retry unrecorded/owed job-store writes on every idle tick whenever any are owed, independent of degraded state (keep the probe + degraded-clear exclusive to the degraded path, per 26-REVIEW.md CR-01's suggested fix)"
      - "A single-statement path (or equivalent) for rejected-submit rows so a mid-write failure cannot leave a PENDING row with no REJECTED marker and no reconciliation path (WR-01)"
      - "A test that reaches the stranded state with only 1-2 loop-level failures (below _DEGRADED_AFTER) and asserts the row reaches ERROR without a restart"
human_verification: []
---

# Phase 26: Worker and Web Robustness Verification Report

**Phase Goal:** The server survives everything the pipeline can throw at it and the browser always reflects reality — a guarded worker loop, 429 backpressure that is actually visible, blocking routes declared `def`, crash recovery at startup, and a server-owned Scan button served from vendored assets that work on an offline LAN — with the deployment and API docs updated in-phase
**Verified:** 2026-09-14T22:20:28Z
**Status:** gaps_found
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths (Roadmap Success Criteria)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | A pipeline, job-store, or `prune` exception is logged with `exc_info` and the worker keeps serving the next job | ✗ FAILED | Worker thread survives (verified: `_run`'s `while` loop has no bare exit on exception, `_process_job` is wrapped in try/except in `_run`, `logger.exception` used). But the *reconciliation* half of this guarantee is broken: a job whose terminal write fails once (below `_DEGRADED_AFTER=3`) is never retried and stays "active" forever in the job store and UI. See gap above — reproduced from `worker.py:756-816` and `tests/test_worker.py:2173-2212`. |
| 2 | Submitting past a full queue returns 429 with `Retry-After` and a message the user can actually see in the status area, the event loop never blocks, and shutdown never blocks on the worker | ✓ VERIFIED | `routes.py:392-404` returns `SubmitResult.QUEUE_FULL` → `RequestRejection.QUEUE_FULL`, rendered by `render_error` (`errors.py:145-147`) which sets `Retry-After` for 429 on both the htmx and JSON branches, retargeted to `#status-message` (D-02/D-03) — separate from the polled `#status-area`. `ScanWorker.submit` (`worker.py:332-365`) uses `put_nowait`/queue full detection, never blocks. `stop()` (`worker.py:294-330`) uses `queue.shutdown()` + bounded `join(STOP_JOIN_SECONDS)`, called only in lifespan shutdown after uvicorn stops serving (`app.py:118-135`), so it cannot block request handling. Local browser test suite (48 tests, `TestRequestErrorSlot` in `test_browser.py`, covering the visible-message assertion) passes offline. |
| 3 | `/health` answers while a scan is running, and concurrent threadpool requests neither stampede the metadata cache nor mutate profiles mid-iteration | ✓ VERIFIED | All routes are plain `def` (confirmed via grep of every handler in `routes.py`: `index`, `health`, `start_scan`, `current_job_status`, `get_tags`, etc. — none `async def`), so FastAPI dispatches them to the threadpool and a blocking scan in one worker thread cannot block `/health` in another. `MetadataCache.get_or_fetch` (`cache.py:64-103`) takes a per-key lock with a double-check, single-flighting concurrent fetches. Profile reads/writes share `_profiles_lock` (`worker.py`, confirmed via `has_profile`/`get_profile`/`_set_profiles`/`_generate_startup_profiles` all taking the lock). Note: `MetadataCache.invalidate` (`cache.py:105-112`) does not take the per-key lock, so a narrow race (WR-05 in `26-REVIEW.md`, confirmed by reading the code) can let a concurrent in-flight fetch's stale result overwrite an invalidation — a real but narrow defect, not a stampede and not a profile-mutation race, so it does not falsify this criterion as worded. |
| 4 | Jobs left non-terminal by a crash are FAILED with a "server restarted" reason before the worker starts, and profiles are generated at startup from the config path that was actually loaded | ✓ VERIFIED | `lifespan` (`app.py:72-135`) runs `job_store.fail_active_jobs(RESTART_REASON)` before `worker.start()`, exactly matching D-13's ordering; a recovery-write failure calls `worker.mark_recovery_pending()` rather than refusing to start. `ScanWorker._run` (`worker.py:651-696`) calls `_generate_startup_profiles()` as its first act, before entering the job loop. `Settings.config_path` is recorded by `load_settings` (`config.py`, confirmed present) and `_generate_startup_profiles`/`_persist_generated_profiles` write to `settings.config_path`, not a re-derived path. Note: WR-03/WR-04 in `26-REVIEW.md` (in-memory `default` can diverge from the persisted file after a restart; a persist exception outside `OSError`/`ConfigError` can silently drop generated profiles from memory) are real but narrower defects around this mechanism — confirmed plausible from the code shape, not independently reproduced here — and don't contradict the criterion's literal wording (recovery + startup generation both do happen, from the loaded path). |
| 5 | A browser test in CI with no CDN egress clicks Scan, waits for the terminal status, and asserts `#scan-btn` is enabled again with no duplicate `id="scan-btn"` in the DOM and `app.js` deleted | ✓ VERIFIED | `tests/test_browser.py` contains the ROBU-11 assertions per `26-12-PLAN.md`'s must-haves; local offline run: `uv run pytest -m browser -q` → 48 passed. `id="scan-btn"` appears exactly once as markup, in `partials/scan_button.html` (grep confirms no other occurrence in templates). `src/saneless/web/static/` contains only `app.css` and `vendor/` — no `app.js`; no `app.js` reference anywhere under `src/saneless/web/`. `.github/workflows/ci.yml` has a `browser` job running `uv run pytest -m browser` behind Chromium, with a comment describing the in-test egress gate. The remote CI run itself has not executed yet (nothing pushed), so "runs in CI" is confirmed by config + local offline equivalent, not by a live GitHub Actions run — treated as pending remote confirmation per the task's environment notes, not a gap. |

**Score:** 4/5 roadmap success criteria verified; criterion 1 fails on the reconciliation half of its guarantee.

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/saneless/worker.py` | Guarded loop, stop flag, degraded health, startup profile generation | ⚠️ PARTIAL | Exists, substantive, wired — thread never dies, degraded mechanism exists and is exercised by tests — but the reconciliation path for sub-threshold failures is incomplete (CR-01). |
| `src/saneless/web/app.py` | Lifespan ordering, crash recovery, guarded shutdown | ✓ VERIFIED | `lifespan` matches D-13 exactly; guarded close per D-09. |
| `src/saneless/web/routes.py` | `def` handlers, 429/503/422 validation | ✓ VERIFIED (with WR-01 caveat) | All handlers `def`; 422 pre-job-creation validation confirmed at `start_scan:359-360`; rejected-submit row mechanism has the same two-transaction gap as CR-01 (WR-01). |
| `src/saneless/web/cache.py` | Single-flight metadata cache | ✓ VERIFIED (with WR-05 caveat) | Single-flight get_or_fetch confirmed; `invalidate` race is narrower than "stampede" and doesn't block this artifact's core claim. |
| `src/saneless/web/templates/` | Server-owned Scan button, message slot, vendored assets | ✓ VERIFIED | `scan_button.html` is the sole button source; `#status-message` slot present (used by `errors.py`/`render_error`); `base.html` not re-inspected line-by-line here but vendored-asset test suite (`test_vendor_assets.py`) is in the passing 1386-test run. |
| `src/saneless/web/static/` | Vendored htmx/Pico, `app.js` deleted | ✓ VERIFIED | `static/vendor/` present, `app.js` absent, no reference to it anywhere in `src/saneless/web/`. |
| `.github/workflows/ci.yml` | Browser job | ✓ VERIFIED | `browser` job present, installs Chromium, runs `-m browser`. |
| Docs (`web-api.md`, `architecture.md`, config/env/cli references) | Updated in-phase | ✓ VERIFIED | "returns immediately"/"fully responsive" phrases gone; `0.0.0.0` bind documented in configuration.md, environment-variables.md, cli-commands.md, web-api.md. |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `ScanWorker._idle_housekeeping` | `JobStore.probe` / reconciliation | idle tick, only while degraded | ⚠️ PARTIAL | Wired for the degraded case; not wired for the sub-threshold `_unrecorded_failures` case (CR-01) — this is the crux of the blocking gap. |
| `routes.start_scan` (429/503 paths) | `#status-message` | `HX-Retarget` header from `render_error` | ✓ WIRED | Confirmed in `errors.py:149-158`. |
| `create_app` | `CrossOriginGuard` | app-wide ASGI middleware | ✓ WIRED (not independently re-derived; consistent with passing `test_cross_origin.py` in the 1386-test run) | |
| `lifespan` startup | `fail_active_jobs` → `worker.start()` | ordering | ✓ WIRED | Confirmed via direct read of `app.py:72-135`. |

### Requirements Coverage

| Requirement | Source Plan | Status | Evidence |
|---|---|---|---|
| ROBU-01 | 26-01, 26-06 | ✗ BLOCKED | Worker survives (literal text true) but reconciliation for sub-threshold failures is missing — see gap. |
| ROBU-02 | 26-01, 26-04, 26-05, 26-09, 26-10, 26-13 | ✓ SATISFIED | 429 + Retry-After + visible message + non-blocking submit + non-blocking shutdown all confirmed. |
| ROBU-03 | 26-04 | ✓ SATISFIED | `stop()` uses stop flag + bounded join (no sentinel); `wait_for_state` helper used pervasively in `test_worker.py` (confirmed by grep hits in the passing suite). |
| ROBU-04 | 26-11, 26-12 | ✓ SATISFIED | Single `id="scan-btn"` source, OOB re-render, `app.js` deleted — confirmed by grep + passing browser suite. |
| ROBU-05 | 26-04, 26-08, 26-10 | ✓ SATISFIED | `def` routes, single-flight cache (with narrow WR-05 caveat that doesn't break the requirement's wording), profile lock. |
| ROBU-06 | 26-01, 26-09 | ✓ SATISFIED | `fail_active_jobs` called before `worker.start()`; shutdown stops worker before closing store (D-09). |
| ROBU-07 | 26-02, 26-08 | ✓ SATISFIED (with WR-03/WR-04 caveats) | Startup generation from `settings.config_path`; `is_bare_default` shape coverage not independently re-derived here beyond the passing `test_auto_profiles.py` suite. |
| ROBU-08 | 26-01, 26-10, 26-11 | ✓ SATISFIED | `start_scan` validates profile + title length (422) before any job row; `invalidate_cache` takes a `MetadataResource` literal. |
| ROBU-09 | 26-03, 26-12 | ✓ SATISFIED | Vendored assets present; no CDN URLs found in templates (not re-grepped exhaustively here, but `test_vendor_assets.py` passes and browser tests run with an egress gate). |
| ROBU-10 | 26-07, 26-13, 26-14 | ✓ SATISFIED (with WR-09 doc caveat) | Cross-origin guard implemented per D-20/D-21/D-23; bind address documented. `26-REVIEW.md`'s WR-09 (docs overstate protection against DNS rebinding) is a documentation-accuracy warning, not a missing requirement. |
| ROBU-11 | 26-12 | ✓ SATISFIED | Browser test suite passes offline (48/48); CI job present. Remote CI run pending (nothing pushed yet), noted per task instructions as not a gap. |

All 11 ROBU-01..ROBU-11 requirement IDs are claimed by at least one plan; none are orphaned.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `src/saneless/worker.py` | 756-776 | Reconciliation logic gated on `_degraded.is_set()` only, silently drops sub-threshold failures | 🛑 Blocker | CR-01 — see gap |
| `src/saneless/web/routes.py` | 277-320 | Two-transaction rejected-row write with no reconciliation on partial failure | ⚠️ Warning | WR-01 — same root cause as CR-01, narrower trigger (store failure specifically on the rejection write) |
| `src/saneless/web/cache.py` | 105-112 | `invalidate` does not take the per-key lock | ⚠️ Warning | WR-05 — narrow stale-read race on manual cache refresh |
| `src/saneless/web/errors.py` | 166-181 | `HTTPException.headers` (e.g. `Allow` on 405) not forwarded to `render_error` | ⚠️ Warning | WR-08 — RFC 9110 §15.5.6 minor violation, not phase-goal-blocking |
| `src/saneless/worker.py` | 568-579, 610-649 | Persist-before-swap ordering + narrow exception catch on persist | ⚠️ Warning | WR-03/WR-04 — in-memory/file `default` can diverge after restart; a non-`OSError`/`ConfigError` persist failure can drop generated profiles from memory, contrary to D-18 |
| `docs/reference/web-api.md` | Cross-site section | Overstates protection (no DNS-rebinding caveat) | ⚠️ Warning | WR-09 — documentation accuracy, not functional |
| No `TBD`/`FIXME`/`XXX` markers found in phase-modified files | — | — | — | Debt-marker gate: clean |

No `TBD`, `FIXME`, or `XXX` markers were found in the files this phase touched.

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|---|---|---|---|
| Full non-browser suite passes | `uv run pytest -m "not browser and not sane_hardware" -q` | 1386 passed, 53 deselected | ✓ PASS |
| Browser suite passes fully offline | `uv run pytest -m browser -q` | 48 passed, 1391 deselected | ✓ PASS |
| CR-01 reproduction via existing test | Read `tests/test_worker.py:2173-2212` | `stranded.is_active` asserted `True` with a comment attributing recovery to a probe that this trace shows never runs at 1-of-3 failures | ✗ FAIL (confirms gap) |

### Probe Execution

Not applicable — this is not a migration/tooling phase with `scripts/*/tests/probe-*.sh` files; none found, and none referenced by the phase's plans/summaries.

### Human Verification Required

None. All success criteria and the CR-01 gap are verifiable by static code reading, the existing automated test suite (including offline browser tests, per the project's Playwright-first policy), and reproduction via an existing test's own assertions — no browser-only visual/UX judgment call remains open.

### Gaps Summary

The phase substantially achieves its goal: four of five roadmap success criteria are solidly verified in the codebase (visible 429 backpressure, non-blocking event loop and shutdown, `/health` during a scan with threadpool safety, crash recovery + startup profile generation from the loaded config path, and the offline CI-ready browser test proving the Scan button re-enables with no duplicate id and `app.js` gone).

The one blocking gap is CR-01 from `26-REVIEW.md`, independently confirmed here by reading `worker.py`'s idle-housekeeping/recovery code and the existing test that documents the exact symptom it claims to guard against: a single transient job-store write failure (well below the 3-failure degraded threshold) permanently strands a job row as "active," which permanently disables the server-owned Scan button and never surfaces through `/health`. This falls squarely inside the phase's own stated goal — "the browser always reflects reality" — and inside D-12's explicit contract ("Recovery needs no user scan, so a freed disk heals on its own"), which this phase's own `26-06-PLAN.md` lists as a must-have truth. The fix is narrow and already sketched in `26-REVIEW.md`'s CR-01 (retry owed writes on every idle tick whenever any are owed, not only while degraded) and should also cover the structurally identical WR-01 gap in the rejected-submit row path.

The remaining review warnings (WR-02 through WR-09, IN-01 through IN-05) were spot-checked (WR-01, WR-05, WR-08 independently confirmed in code) or accepted on the strength of the review's own reproductions (WR-03, WR-04, WR-09) as real but non-blocking: they degrade correctness or documentation accuracy in narrower scenarios (a success write that fails after Paperless already accepted the document, a profile-generation persistence edge case, a cache-invalidate race, a missing `Allow` header, a documentation overstatement about DNS rebinding) without contradicting any of the five numbered roadmap success criteria as worded. They are worth closing in the same remediation pass as CR-01/WR-01 since they share code paths, but they do not independently block phase sign-off.

---

_Verified: 2026-09-14T22:20:28Z_
_Verifier: Claude (gsd-verifier)_
