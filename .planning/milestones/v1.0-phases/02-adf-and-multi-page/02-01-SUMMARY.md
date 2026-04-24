---
phase: 02-adf-and-multi-page
plan: 01
subsystem: scanning
tags: [pillow, image-processing, empty-page-detection, thumbnails, sqlite]

# Dependency graph
requires:
  - phase: 01-core-pipeline
    provides: config, job, exceptions, scanner backend types
provides:
  - pages.py module with is_empty_page, filter_empty_pages, generate_thumbnail
  - ProfileConfig empty page threshold fields
  - JobState.AWAITING_FLIP and Job.thumbnail
  - FeederEmptyError exception
  - SaneDevice multi_scan protocol method
  - Test fixtures for multi-page, empty, and content images
affects: [02-adf-and-multi-page]

# Tech tracking
tech-stack:
  added: []
  patterns: [dual-threshold empty page detection, base64 JPEG thumbnails]

key-files:
  created: [src/saneless/pages.py, tests/test_pages.py]
  modified: [src/saneless/config.py, src/saneless/job.py, src/saneless/exceptions.py, src/saneless/scanner/sane_backend.py, tests/conftest.py, tests/test_config.py, tests/test_worker.py]

key-decisions:
  - "Used Resampling.LANCZOS instead of Image.LANCZOS for ty/pyrefly type checker compatibility"
  - "Used PIL.ImageStat.Stat directly instead of ImageStat.Stat for cleaner imports"

patterns-established:
  - "Dual-threshold algorithm: mean > T1 AND stddev < T2 on grayscale for empty detection"
  - "Base64 JPEG thumbnails with EXIF stripping and max_edge constraint"

requirements-completed: [SCAN-08, SCAN-09]

# Metrics
duration: 5min
completed: 2026-03-20
---

# Phase 02 Plan 01: Foundation Types and Page Processing Summary

**Dual-threshold empty page detection, base64 JPEG thumbnail generation, and ADF foundation types (AWAITING_FLIP state, FeederEmptyError, threshold config fields)**

## Performance

- **Duration:** 5 min
- **Started:** 2026-03-20T19:37:23Z
- **Completed:** 2026-03-20T19:42:41Z
- **Tasks:** 2
- **Files modified:** 9

## Accomplishments
- Created pages.py module with is_empty_page (dual-threshold grayscale), filter_empty_pages (batch filter with logging), and generate_thumbnail (base64 JPEG, <=300px, EXIF stripped)
- Added ProfileConfig fields for per-profile empty page thresholds (250.0/5.0 defaults)
- Extended JobState with AWAITING_FLIP, Job with thumbnail field, JobStore with update_thumbnail and thumbnail persistence
- Added FeederEmptyError exception and SaneDevice.multi_scan protocol method
- 15 new tests for page processing, plus foundation type tests -- 107 total tests passing

## Task Commits

Each task was committed atomically:

1. **Task 1: Foundation types - config, job, exceptions, protocol updates** - `5bd6158` (feat)
2. **Task 2: Page processing module - empty page detection and thumbnail generation** - `8d08f8b` (feat)

## Files Created/Modified
- `src/saneless/pages.py` - Empty page detection and thumbnail generation module
- `tests/test_pages.py` - 15 tests for empty detection, filtering, and thumbnail generation
- `src/saneless/config.py` - Added empty_page_mean_threshold and empty_page_stddev_threshold to ProfileConfig
- `src/saneless/job.py` - Added AWAITING_FLIP state, thumbnail field, update_thumbnail method, schema update
- `src/saneless/exceptions.py` - Added FeederEmptyError(ScanError)
- `src/saneless/scanner/sane_backend.py` - Added multi_scan to SaneDevice Protocol
- `tests/conftest.py` - Added multi_page_images, empty_page_image, content_page_image fixtures
- `tests/test_config.py` - Added threshold and FeederEmptyError tests
- `tests/test_worker.py` - Added AWAITING_FLIP and thumbnail persistence tests

## Decisions Made
- Used `Resampling.LANCZOS` instead of `Image.LANCZOS` for ty and pyrefly type checker compatibility -- both resolve to the same integer value (1) at runtime
- Used `PIL.ImageStat.Stat` direct import instead of `ImageStat.Stat` for cleaner module imports

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed Image.LANCZOS type checker failure**
- **Found during:** Task 2 (pages.py implementation)
- **Issue:** Both ty and pyrefly report `Image.LANCZOS` as unresolved attribute (not in type stubs)
- **Fix:** Changed to `Resampling.LANCZOS` from `PIL.Image` which is the modern API and properly typed
- **Files modified:** src/saneless/pages.py
- **Verification:** ty and pyrefly both pass clean
- **Committed in:** 8d08f8b (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (1 bug)
**Impact on plan:** Minor import change for type checker compatibility. No scope creep.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- All foundation types ready for 02-02 (ADF scanning pipeline)
- pages.py functions ready for inline use in scan pipeline
- Test fixtures available for downstream integration tests

## Self-Check: PASSED

All files exist, both commits verified, all key content patterns confirmed.

---
*Phase: 02-adf-and-multi-page*
*Completed: 2026-03-20*
