---
phase: 29-geometry-memory-and-timeouts
verified: 2026-09-16T04:05:12Z
status: passed
score: 5/5 must-haves verified
overrides_applied: 0
---

# Phase 29: Geometry, Memory, and Timeouts Verification Report

**Phase Goal:** A long scan is ordered, bounded in memory, and cancellable without wedging the
process — pages spooled to disk with explicit ordered records, a shared ADF/flatbed timeout that
waits for the cancelled read, and `sane.init()`/`sane.exit()` guarded as process-global — with the
architecture explanation page updated in-phase.

**Verified:** 2026-09-16T04:05:12Z
**Status:** passed
**Re-verification:** No — initial verification

## Method

This verification did not trust `29-SUMMARY.md` files or `29-REVIEW-FIX.md`'s own claims. For
every finding in the code review (2 critical + 11 warnings) I read the current source at HEAD
(`4a2cbad`) and confirmed the fix is actually present, then read the regression test that pins it
and, for one (WR-04/`get_devices` wedge refusal), reverted the fix in a scratch copy, re-ran the
pinning test, watched it fail with `DID NOT RAISE`, and restored the file (`git status` clean
afterward — no residual diff). I re-ran the full single-process suite (`uv run pytest -q`, the
exact command the release gate runs) and got **2123 passed, 0 failed**, matching the orchestrator's
own measurement. I read the doc-truth sections of `docs/explanation/architecture.md` and three
other touched docs against what the source actually does, not against what the phase intended.

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | A 12-page scan yields records 1..12, duplex interleave reorders records not files, peak memory bounded by ~1 page | ✓ VERIFIED | `tests/test_pipeline.py::test_twelve_page_order_survives_into_the_assembled_pdf` reads back the assembled PDF with `pikepdf`, asserts byte-identical PNG-IDAT-to-PDF-stream equality per page and 12 distinct page contents in `sequence` order 1..12 (ran green: 6/6 in the class). `test_interleave_records_never_recovers_order_from_the_filesystem` asserts on-disk name order (`a-0001..a-0003,b-0001..b-0003`) differs from document order (`a1,b3,a2,b2,a3,b1`) and that no file moved. `tests/test_scanner.py::test_live_page_images_stays_bounded` asserts high-water ≤ 2 live Pillow images and identical for 3 and 12 pages (weakref instrument, not RSS/tracemalloc — both were measured and rejected in the module docstring for good, cited reasons). `tests/test_pdf.py`'s merge tests separately prove assembly's peak stays flat (per-page `img2pdf.convert` + `pikepdf.Job` qpdf merge; `spool.py`/`pdf.py` confirmed doing exactly this). `docs/explanation/architecture.md` states the two proofs separately and does not let either sentence cover the other's mechanism (line 56: "roughly one decoded page… measured at two live page images"; line 50: assembly "does not grow with page count… no point is more than one page's worth of PDF being built"). |
| 2 | A mid-batch scanner error after N pages keeps those N pages and reports the error with the count | ✓ VERIFIED | `test_partial_scan_preserved_when_the_scanner_fails_mid_batch` opens the preserved PDF with `pikepdf` and asserts `len(pdf.pages) == 3` (read-back, not inferred from a count or filename) after a jam on page 4; message asserts `"3 page(s)"` and the path. `test_cancel_preserves_nothing` asserts `failed_dir` does not even exist and no ERROR-level log line after a `ScanCancelledError`. `test_duplex_mismatch_assembly_failure_keeps_every_spooled_page` (new, CR-01 regression) reads the preserved directory and asserts the exact five file names (`a-0001..a-0003`, `b-0001..b-0002`) with nonzero sizes — closing the hole the review found where a duplex-mismatch assembly failure destroyed every scanned page of both passes. Confirmed in source: `_handle_duplex_mismatch` now wraps both `assemble_pdf` calls in `_preserving_page_files` (pipeline.py:1392-1408). |
| 3 | A fake with a blocking read proves close() never called while blocked, and the process exits — a stuck read never blocks docker stop/pytest | ✓ VERIFIED | `TestStuckReadDoesNotBlockProcessExit::test_process_exits` starts a real child process (`subprocess.run(timeout=20)`) whose reader thread blocks in a genuine GIL-releasing syscall (`os.read` on an empty pipe — not a `time.sleep` or an `Event.wait` standing in for it, per the module's own comment explaining why that distinction matters). The child asserts `returned=False cancels=1 closes=0` and the parent asserts `returncode == 0`. This is the daemon-thread mechanism (`_acquire_with_timeout` in `sane_backend.py`), which is what actually makes the process exit-able (a `ThreadPoolExecutor`'s non-daemon workers were rejected for exactly this reason per D-11). Ran green. In-process tests (`test_a_wedged_backend_refuses_the_next_call_with_no_sane_traffic`, `test_the_wedge_clears_when_the_late_read_finally_returns`) independently confirm cancel-then-wait-then-close-or-leave ordering. |
| 4 | The flatbed path enforces the same timeout and image validation as the ADF path | ✓ VERIFIED | `_snap_flatbed` takes a `_PageBudget` (timeout + grace) defaulting to the same module constants the ADF path uses (`_DEFAULT_PAGE_BUDGET`, sane_backend.py:1805-1813) — closing WR-10, which found the flatbed grace was previously non-injectable and untestable without a real 10s wait. `test_flatbed_did_not_respond_to_cancel` now mirrors the ADF equivalent and bounds its own elapsed time (so a regression that stopped forwarding the grace would fail fast, not silently pass in 10s). `test_flatbed_timeout_matches_the_adf_message_shape` pins both the timeout and grace defaults. Flatbed validation reuses `_validate_page_image` (confirmed pre-existing, tested fatal-on-flatbed). Ran green (10/10 relevant tests). |
| 5 | sane.init() runs once per process behind a re-entry guard, sane.exit() runs at shutdown, neither reachable from a request path | ✓ VERIFIED | `tests/test_app_lifespan.py::test_sane_lifecycle_across_startup_every_route_and_shutdown` drives a real `SaneBackend` over every app route (asserting `uncovered == set()` so a new route can't silently escape the proof), asserting `init_call_count == 1` and `exit_call_count == 0` after every single request, then `exit_call_count == 1` only after lifespan shutdown — and independently confirms the scan actually reached the fake device (`fake.open(...).calls`), so the proof isn't satisfied by a scan that never started. `tests/test_scanner.py`'s `init_once` tests cover two-backend-one-init and the differing-host WARNING. **Critical finding CR-02** (the `_INIT` guard leaking across test modules, silently poisoning `tests/test_sane_hardware.py` and making the release-gate command `uv run pytest` fail 3/2123 despite `ci.yml`'s split-by-marker runs hiding it) is fixed: `tests/conftest.py::sane_process_state` is a suite-wide autouse fixture (confirmed in source, replacing the old module-local `sane_init_guard`), and the deterministic repro the reviewer used now passes. Confirmed by re-running the full single-process suite live: **2123 passed, 0 failed**. |

**Score:** 5/5 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/saneless/spool.py` | `SpooledPageSink`/`PageRecord`/`PageSink` contract, D-07 disk check, no raw OSError escape | ✓ VERIFIED | Read in full. `add()` appends the record before firing the (unwrapped, deliberately) thumbnail callback — WR-01 fix confirmed (record survives a raising callback). `_check_room_for`'s `shutil.disk_usage` call is now wrapped in `try/except OSError` — WR-11 fix confirmed. |
| `src/saneless/scanner/sane_backend.py` | `_acquire_with_timeout`, cancel-then-wait, wedge guard, init guard, `get_devices` wedge refusal | ✓ VERIFIED | `_refuse_if_wedged` confirmed present on `get_devices` (WR-04), `_PageBudget`/`_DEFAULT_PAGE_BUDGET` confirmed threading through `_snap_flatbed` (WR-10), `started`/`reader.is_alive()` guard confirmed replacing the unconditional interrupt-tail call (WR-05). |
| `src/saneless/pipeline.py` | Preservation guards (D-09/D-10), duplex-mismatch guard (CR-01), preservation-failure messages naming what survived (WR-02/WR-03) | ✓ VERIFIED | `_handle_duplex_mismatch` wraps both `assemble_pdf` calls in `_preserving_page_files` (CR-01). `_preserve_page_files_after_partial_failure` fallback confirmed wired into `_preserving_partial_scan`'s `(OSError, PdfError)` handler (WR-03). `_preservation_failure_message` helper confirmed used uniformly (WR-02). |
| `tests/conftest.py` | Suite-wide autouse SANE-state reset (CR-02), shared-failed-dir leak guard (WR-07) | ✓ VERIFIED | `sane_process_state` autouse fixture confirmed. `_suite_leaves_the_shared_failed_dir_alone` confirmed present. |
| `docs/explanation/architecture.md` | D-20's pipeline diagram, memory/disk/timeout subsection, PDF-assembly correction, worker-shutdown correction | ✓ VERIFIED | All four sections present and read; "lossless" replaces "byte-for-byte identical" (line 48); the memory sentence and the assembly sentence describe distinct mechanisms without overlap. |

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `SpooledPageSink.add` | `PageRecord` list | append-then-callback ordering | WIRED | Confirmed by reading source; regression test present and passing. |
| `run_pipeline` dispatch | `_handle_duplex_mismatch` | `_preserving_page_files` now scoped inside the mismatch handler, not around `run_pipeline`'s dispatch | WIRED | Confirmed; avoids the double-preservation the review flagged as a risk of the naive fix. |
| `_acquire_with_timeout` | daemon reader thread | per-call `threading.Thread(daemon=True)`, not a shared executor | WIRED | Confirmed in source; subprocess test proves the daemon-thread mechanism is what lets the process exit. |
| `create_app` routes | `SaneBackend` | never constructed or torn down inside a route handler | WIRED | Confirmed behaviourally by `test_sane_lifecycle_across_startup_every_route_and_shutdown` covering every route. |

### Requirements Coverage

| Requirement | Description | Status | Evidence |
|---|---|---|---|
| HARD-01 | Ordered page spool, 1..12 order, duplex record reorder, ~1-page memory bound | ✓ SATISFIED | Truth 1 |
| HARD-02 | Mid-batch error keeps N pages, reports count | ✓ SATISFIED | Truth 2 |
| HARD-03 | Timeout waits for cancelled read before close; stuck read on daemon thread never blocks exit | ✓ SATISFIED | Truth 3 |
| HARD-04 | Flatbed shares ADF timeout and validation | ✓ SATISFIED | Truth 4 |
| HARD-05 | `sane.init()` once behind guard, `sane.exit()` at shutdown, neither from a request path | ✓ SATISFIED | Truth 5 |

No orphaned requirements: HARD-01..05 are the full set mapped to this phase in `REQUIREMENTS.md` lines 119-125, and all five are claimed and satisfied.

### Anti-Patterns Found

Scanned all 33 phase-touched files (`git log --stat f0610e1..HEAD`) for `TBD`/`FIXME`/`XXX`/`TODO`/`HACK`/`PLACEHOLDER`/"not yet implemented" patterns: **none found** in source or test files touched by this phase. One incidental match (`test_correspondent_placeholder` in `tests/test_web.py`) is an HTML `<select>` placeholder-option test unrelated to this phase's scope.

No debt markers, no blockers.

### Deep Review Findings — All 13 Fixed and Confirmed

`29-REVIEW.md` (deep review, 2 critical + 11 warnings + 4 info) found real defects, most notably:
- **CR-01**: a duplex-mismatch assembly failure destroyed every scanned page of both passes with no preservation. **Confirmed fixed** in `pipeline.py` and pinned by three new tests in `TestDuplexMismatchDelivery` that read back the actual preserved file names.
- **CR-02**: the process-global SANE init guard leaked between test modules, silently making `uv run pytest` (the exact release-gate command) fail 3/2123 while the marker-split CI runner hid it entirely. **Confirmed fixed** via a suite-wide autouse fixture in `conftest.py`.

I independently re-verified one warning-tier fix by falsification: reverted WR-04's `_refuse_if_wedged("the scanners", "list")` guard on `get_devices()` in a scratch copy, re-ran the pinning test, and watched it fail with `DID NOT RAISE` — confirming the test would catch a regression, not merely document the fix. File was restored and `git status` verified clean.

All 13 fixes (CR-01, CR-02, WR-01 through WR-11) were read directly in the current source, not merely trusted from `29-REVIEW-FIX.md`'s narrative. The 4 INFO-tier findings were correctly left unfixed as out of scope (recorded, not silently dropped).

### Scope Discipline

Confirmed the phase did NOT implement any of the explicitly deferred items:
- No `scanner.page_timeout_seconds` or similar new config key (grep clean).
- `/health` route present but unmodified for wedge/degraded-scanner status (still only reports job-store degradation, pre-existing behaviour) — the wedge flag exists (`D-13`) but is not surfaced there, matching the Phase 30 deferral.
- No partial-scan upload path to Paperless — `_preserving_partial_scan`'s docstring and code confirm nothing is uploaded; N-02's alternative was explicitly rejected.
- `pages_scanned` on the worker's job-finish path is on the success/fallback branch only (`result.pages_scanned`), not wired into the ERROR path — matches "Phase 30" deferral of structured page counts on failed jobs.
- Mid-pass abort during manual duplex pass B: not implemented; the D-11 cancel helper is present as the seam a later phase would use, per the phase's own boundary statement.

### Doc Truth

- `docs/how-to/troubleshoot-a-failed-scan.md` already shows the correctly-sanitised name (`invoice-partial.pdf`, no brackets) — consistent with `sanitise_title_for_filename`'s allow-list dropping `()`.
- `docs/explanation/consume-directory-fallback.md` and `docs/how-to/set-up-adf-duplex.md` state the acquisition-order rule accurately post-WR-06 (simplex = document order; manual duplex interleaves `a-0001, b-000N, a-0002, ...` and is NOT document order in the preserved directory) — this is a real, load-bearing correction, not cosmetic.
- `docs/explanation/architecture.md`'s memory/assembly split is accurate and does not overstate either mechanism (see Truth 1 evidence).
- `docs/reference/docker.md:70-72` still calls the ~13 MB PNG figure "uncompressed" (IN-03) — this is a real, minor factual error left unfixed. It was explicitly triaged as INFO-tier and out of the fix scope in `29-REVIEW-FIX.md`, and I confirm it is still present at HEAD. It does not affect any HARD requirement and is not a blocker, but is noted here as a genuine, small residual doc inaccuracy for whoever next touches that file.

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|---|---|---|---|
| Full single-process suite (release gate) | `uv run pytest -q` | 2123 passed, 0 failed | ✓ PASS |
| WR-04 regression is real, not decorative | scratch-revert `get_devices` guard, re-run pinning test | `DID NOT RAISE` (fails as expected) | ✓ PASS (falsification) |
| Twelve-page order / interleave / cancel / partial-preservation tests | targeted `-k` runs | all green | ✓ PASS |
| SANE lifecycle / init-once / CLI-closes-backend tests | targeted `-k` runs | all green | ✓ PASS |
| Flatbed timeout / wedge / live-page-image / process-exit tests | targeted `-k` runs | all green (10/10) | ✓ PASS |
| sane_hardware marker suite | `uv run pytest -m sane_hardware` | 6 passed | ✓ PASS |
| Debt-marker scan across 33 phase-touched files | grep TBD/FIXME/XXX/TODO/HACK | none found | ✓ PASS |

### Human Verification Required

None. The one manual-only item recorded in `29-VALIDATION.md` (behaviour against a real network scanner over `net`/`hpaio`, since the blocking-cancel RPC on that backend cannot be reproduced without the hardware) is explicitly out of scope for automated verification and was already correctly flagged as the sole manual item by the phase's own validation contract — every other path, including the real-libsane cancel sequence, is automated and was independently confirmed here.

### Gaps Summary

No gaps. All five success criteria are genuinely met in the codebase, not merely claimed. The phase's own deep code review found two critical defects post-hoc (a duplex-mismatch data-loss hole and a test-isolation leak that silently broke the actual release gate) — both were fixed with real regression tests, and I independently confirmed both fixes hold at HEAD, re-ran the full suite (2123/2123 passed, matching the orchestrator's own measurement), and falsified one fix by reverting it and watching its test correctly fail. One residual INFO-tier doc inaccuracy (`docker.md`'s "uncompressed" wording) remains, correctly triaged as out of fix-scope and not a blocker to this phase's goal.

---

_Verified: 2026-09-16T04:05:12Z_
_Verifier: Claude (gsd-verifier)_
