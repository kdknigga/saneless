---
phase: 02-adf-and-multi-page
plan: 03
subsystem: pipeline
tags: [manual-duplex, thumbnail, empty-page-filter, threading, PIL]

# Dependency graph
requires:
  - phase: 02-adf-and-multi-page/02-01
    provides: "Page processing utilities (is_empty_page, filter_empty_pages, generate_thumbnail)"
  - phase: 02-adf-and-multi-page/02-02
    provides: "ADF multi-page scanning with per-page timeout"
provides:
  - "Pipeline with thumbnail generation, empty page filtering, manual duplex interleaving"
  - "Worker with AWAITING_FLIP event coordination, continue_flip(), abort_flip()"
  - "PipelineRequest with flip_event, abort_event, thumbnail_callback fields"
affects: [03-htmx-web-ui]

# Tech tracking
tech-stack:
  added: []
  patterns: ["threading.Event for cross-thread scan coordination", "two-pass manual duplex with back-reversal interleaving"]

key-files:
  created: []
  modified:
    - src/saneless/pipeline.py
    - src/saneless/worker.py
    - tests/test_pipeline.py
    - tests/test_worker.py
    - tests/conftest.py
    - tests/test_cli.py

key-decisions:
  - "Extracted _scan_manual_duplex and _scan_simplex helpers to keep run_pipeline under ruff branch/statement limits"
  - "Threading imports moved to TYPE_CHECKING block per ruff TC003 rule"

patterns-established:
  - "Manual duplex two-pass: fronts first, wait for flip signal, backs reversed then interleaved"
  - "Worker detects manual duplex from profile.source containing 'manual' and 'duplex'"
  - "Status callback message 'Awaiting flip...' triggers AWAITING_FLIP state transition"

requirements-completed: [SCAN-06, SCAN-07, SCAN-11, SCAN-12]

# Metrics
duration: 7min
completed: 2026-03-20
---

# Phase 02 Plan 03: Pipeline & Worker Integration Summary

**Manual duplex two-pass scanning with threading.Event coordination, empty page filtering with profile thresholds, and thumbnail generation after first scanned page**

## Performance

- **Duration:** 7 min
- **Started:** 2026-03-20T19:58:49Z
- **Completed:** 2026-03-20T20:05:49Z
- **Tasks:** 2
- **Files modified:** 6

## Accomplishments
- Pipeline handles manual duplex via two ADF passes with back-reversal interleaving
- Page count mismatch between passes raises ScanError before empty page detection
- Empty page filtering uses profile-configurable thresholds on interleaved result
- Thumbnail generated from first scanned page and delivered via callback
- Worker creates threading.Event pairs for manual duplex, exposes continue_flip()/abort_flip()
- Worker stores thumbnail on job via JobStore.update_thumbnail
- EXIF data stripped from all images before PDF assembly
- All 153 tests pass including 28 pipeline tests and 17 worker tests

## Task Commits

Each task was committed atomically:

1. **Task 1: Pipeline - thumbnail, empty page filter, manual duplex** - `e6e33f4` (test) + `1003a70` (feat)
2. **Task 2: Worker - AWAITING_FLIP event coordination** - `c361774` (test) + `09d2ed9` (feat)

_TDD tasks have separate test and implementation commits._

## Files Created/Modified
- `src/saneless/pipeline.py` - Added thumbnail generation, empty page filtering, manual duplex interleaving with _scan_manual_duplex and _scan_simplex helpers
- `src/saneless/worker.py` - Added flip_event/abort_event coordination, continue_flip(), abort_flip(), current_job_id property, thumbnail storage callback
- `tests/test_pipeline.py` - 28 tests covering thumbnail, empty page filter, manual duplex happy path/mismatch/abort, EXIF stripping, flatbed regression
- `tests/test_worker.py` - 17 tests covering manual duplex coordination, abort, thumbnail storage, queuing, current_job_id tracking
- `tests/conftest.py` - Updated mock_scanner to use content image instead of pure white (empty page filter would reject it)
- `tests/test_cli.py` - Updated MockSaneBackend.scan_pages to return content image

## Decisions Made
- Extracted `_scan_manual_duplex()` and `_scan_simplex()` from `run_pipeline()` to stay within ruff's branch/statement limits (PLR0912, PLR0915)
- Moved `threading` and `PIL.Image` imports into `TYPE_CHECKING` block per ruff TC003/TC002 rules

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Updated mock_scanner fixture and CLI test mock to use content images**
- **Found during:** Task 1 (Pipeline implementation)
- **Issue:** Existing mock_scanner returned pure white images which the new empty page filter correctly rejects, causing existing tests to fail with "All pages were detected as empty"
- **Fix:** Changed conftest.py mock_scanner and test_cli.py MockSaneBackend to return images with drawn black rectangles (detectable as content)
- **Files modified:** tests/conftest.py, tests/test_cli.py
- **Verification:** All 153 tests pass
- **Committed in:** 1003a70 (Task 1 feat commit)

**2. [Rule 1 - Bug] Refactored run_pipeline to avoid ruff complexity limits**
- **Found during:** Task 1 (Pipeline implementation)
- **Issue:** Adding manual duplex logic to run_pipeline pushed it over ruff's PLR0912 (13 branches > 12) and PLR0915 (56 statements > 50) limits
- **Fix:** Extracted `_scan_manual_duplex()` and `_scan_simplex()` helper functions
- **Files modified:** src/saneless/pipeline.py
- **Verification:** `uv run ruff check .` passes clean
- **Committed in:** 1003a70 (Task 1 feat commit)

---

**Total deviations:** 2 auto-fixed (2 bugs)
**Impact on plan:** Both auto-fixes necessary for correctness. No scope creep.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Pipeline and worker fully integrated with all Phase 02 capabilities
- Manual duplex, empty page filtering, thumbnail generation all wired in
- Ready for Phase 03 (HTMX web UI) to call worker.submit(), worker.continue_flip(), worker.abort_flip()
- current_job_id property available for UI status polling

---
*Phase: 02-adf-and-multi-page*
*Completed: 2026-03-20*

## Self-Check: PASSED
