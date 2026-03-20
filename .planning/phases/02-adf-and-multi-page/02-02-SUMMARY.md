---
phase: 02-adf-and-multi-page
plan: 02
subsystem: scanner
tags: [sane, adf, multi-page, duplex, pillow, threadpool, timeout, validation]

# Dependency graph
requires:
  - phase: 02-adf-and-multi-page/01
    provides: "Foundation types (ScanSource, ScanMode, PageResult), page processing module"
provides:
  - "ADF multi-page scanning via multi_scan() with per-page timeout"
  - "Inline page validation (dimensions, file size, white/black detection)"
  - "Hardware duplex support via ADF Duplex source"
  - "EXIF stripping on all scan paths"
  - "FeederEmptyError for empty ADF feeders"
affects: [02-adf-and-multi-page/03, 03-web-ui]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "ThreadPoolExecutor per-page timeout for ADF scanning"
    - "Inline page validation before yielding scanned images"
    - "Type-safe cast helper for generic future results"

key-files:
  created: []
  modified:
    - src/saneless/scanner/sane_backend.py
    - tests/test_scanner.py

key-decisions:
  - "Used separate except clauses instead of tuple syntax for Python 3.12 AST compatibility in pre-commit hooks"
  - "Added _as_image() cast helper to satisfy ty type checker with ThreadPoolExecutor generic results"
  - "Simplified multi_scan() error handling: any exception on first call treated as empty feeder"

patterns-established:
  - "_validate_page_image: scanner-level page validation with strict thresholds (254/1.0 white, 1.0/1.0 black)"
  - "_is_adf_source: case-insensitive 'adf' substring check for source routing"

requirements-completed: [SCAN-04, SCAN-05, SCAN-10]

# Metrics
duration: 8min
completed: 2026-03-20
---

# Phase 02 Plan 02: ADF Multi-Page Scan Summary

**ADF multi-page scanning via multi_scan() with ThreadPoolExecutor per-page timeout, inline validation (dimensions/size/white/black), EXIF stripping, and FeederEmptyError for empty feeders**

## Performance

- **Duration:** 8 min
- **Started:** 2026-03-20T19:44:58Z
- **Completed:** 2026-03-20T19:52:38Z
- **Tasks:** 1
- **Files modified:** 2

## Accomplishments
- Extended SaneBackend.scan_pages() to use multi_scan() for ADF sources with per-page timeout
- Added inline page validation: nonzero dimensions, minimum file size (10KB), not pure white/black
- Hardware duplex support (ADF Duplex source detected and routed to multi_scan)
- EXIF stripping on all scan paths (ADF and flatbed)
- FeederEmptyError raised for empty feeders and multi_scan errors
- 19 new tests covering ADF, duplex, empty feeder, validation, timeout, and cleanup scenarios

## Task Commits

Each task was committed atomically:

1. **Task 1: Extend SaneBackend for ADF multi-page scan** - `ad43719` (feat)

_Note: TDD RED commit was merged into the feat commit due to pre-commit hook requiring implementation_

## Files Created/Modified
- `src/saneless/scanner/sane_backend.py` - Added _scan_adf_pages(), _validate_page_image(), _is_adf_source(), _as_image() and modified scan_pages() for ADF/flatbed branching
- `tests/test_scanner.py` - Added TestSaneBackendADFScan, TestSaneBackendDuplex, TestSaneBackendEmptyFeeder, TestSaneBackendPageValidation, TestSaneBackendPerPageTimeout, TestSaneBackendADFCleanup test classes and updated MockSaneDev

## Decisions Made
- Used separate `except ScanError:` / `except FeederEmptyError:` clauses instead of tuple syntax to maintain compatibility with Python 3.12 AST parser in pre-commit hooks (prek uses CPython 3.12 for AST checks)
- Added `_as_image()` type-safe cast helper to satisfy ty type checker since ThreadPoolExecutor.submit(next, iterator) loses generic type information
- Simplified multi_scan() initial error handling: any exception when calling multi_scan() is treated as empty feeder (FeederEmptyError)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Python 3.12 AST compatibility for except clause**
- **Found during:** Task 1 (commit phase)
- **Issue:** ruff formatter removed parentheses from `except (ScanError, FeederEmptyError):` producing Python 3.14-only syntax; pre-commit hook uses CPython 3.12 AST parser which rejects this
- **Fix:** Split into two separate except clauses
- **Files modified:** src/saneless/scanner/sane_backend.py
- **Verification:** Pre-commit hooks pass, all tests pass
- **Committed in:** ad43719

**2. [Rule 3 - Blocking] ty type checker error on future.result() generic type**
- **Found during:** Task 1 (ty check)
- **Issue:** ty could not infer Image.Image type from ThreadPoolExecutor.submit(next, iterator).result()
- **Fix:** Added _as_image() cast helper with runtime isinstance check
- **Files modified:** src/saneless/scanner/sane_backend.py
- **Verification:** ty check passes clean, pyrefly check passes clean
- **Committed in:** ad43719

---

**Total deviations:** 2 auto-fixed (2 blocking)
**Impact on plan:** Both fixes were necessary for code quality tools compatibility. No scope creep.

## Issues Encountered
None beyond the auto-fixed deviations above.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- ADF multi-page and hardware duplex scanning fully implemented
- Page validation pipeline in place (scanner-level strict thresholds)
- Ready for Plan 03 (manual duplex, pipeline integration, empty page detection)
- All 126 project tests pass

---
*Phase: 02-adf-and-multi-page*
*Completed: 2026-03-20*
