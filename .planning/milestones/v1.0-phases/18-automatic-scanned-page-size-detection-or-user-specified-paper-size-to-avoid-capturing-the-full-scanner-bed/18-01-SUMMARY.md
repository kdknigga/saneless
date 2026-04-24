---
phase: 18-automatic-scanned-page-size-detection-or-user-specified-paper-size-to-avoid-capturing-the-full-scanner-bed
plan: 01
subsystem: scanner
tags: [sane, paper-size, geometry, crop, pillow, pydantic]

requires:
  - phase: 01-core-pipeline
    provides: Scanner abstraction layer, config system, pipeline orchestration
  - phase: 16-when-a-scanner-advertised-auto-mode-it-should-be-configurable-by-the-user-if-that-means-flatbed-mode-or-adf-mode
    provides: auto_source_mode field pattern on ProfileConfig and ScanSettings
provides:
  - PaperSize Literal type and PAPER_SIZES_MM dimension lookup
  - crop_to_paper_size Pillow utility for scan area cropping
  - paper_size field on ProfileConfig (Literal, default "full")
  - paper_size field on ScanSettings (str, default "full")
  - SANE geometry option setting (tl_x, tl_y, br_x, br_y) in scan_pages
  - Pillow crop fallback when scanner lacks geometry support
affects: [18-02, docs, web-ui]

tech-stack:
  added: []
  patterns: [SANE geometry option setting with fallback, helper extraction for PLR0912/PLR0915 compliance]

key-files:
  created:
    - src/saneless/paper_sizes.py
    - tests/test_paper_sizes.py
  modified:
    - src/saneless/config.py
    - src/saneless/scanner/base.py
    - src/saneless/pipeline.py
    - src/saneless/scanner/sane_backend.py
    - tests/test_scanner.py

key-decisions:
  - "Inline Literal in ProfileConfig instead of PaperSize import to satisfy TC001 without noqa"
  - "Geometry attributes added to SaneDevice Protocol for type-safe access without type: ignore"
  - "Extracted _set_geometry and _maybe_crop helpers to keep scan_pages under PLR0912/PLR0915 limits"
  - "_NoGeometryDevice test mock with __setattr__ override for geometry rejection testing"

patterns-established:
  - "SANE dynamic option access: add to Protocol + catch Exception for unsupported options"
  - "Keyword-only bool params (FBT001): use * separator for boolean function arguments"

requirements-completed: [PS-01, PS-02, PS-03, PS-04, PS-05]

duration: 10min
completed: 2026-03-24
---

# Phase 18 Plan 01: Paper Size Configuration and Scan Area Control Summary

**Paper size config field with SANE geometry option setting and Pillow crop fallback for constraining scan area to A3/A4/A5/Letter/Legal dimensions**

## Performance

- **Duration:** 10 min
- **Started:** 2026-03-24T11:20:46Z
- **Completed:** 2026-03-24T11:30:49Z
- **Tasks:** 2
- **Files modified:** 7

## Accomplishments
- Paper sizes module with dimension lookup (5 sizes) and DPI-aware crop utility
- paper_size Literal field on ProfileConfig defaulting to "full" (zero-change upgrade path)
- SANE geometry options (tl_x/tl_y/br_x/br_y) set when paper_size is not "full"
- Pillow crop fallback when scanner does not support geometry options
- Both ADF and flatbed paths handle crop fallback correctly
- 24 new tests covering dimensions, cropping, config validation, geometry, and fallback

## Task Commits

Each task was committed atomically:

1. **Task 1: Paper sizes module, config field, data model, and pipeline bridge** - `303c784` (test: RED), `e38776f` (feat: GREEN)
2. **Task 2: Scanner geometry option setting and Pillow crop fallback** - `0b86652` (feat)

## Files Created/Modified
- `src/saneless/paper_sizes.py` - PaperSize Literal, PAPER_SIZES_MM dict, crop_to_paper_size function
- `src/saneless/config.py` - paper_size Literal field on ProfileConfig
- `src/saneless/scanner/base.py` - paper_size str field on ScanSettings dataclass
- `src/saneless/pipeline.py` - paper_size bridged from config to ScanSettings
- `src/saneless/scanner/sane_backend.py` - Geometry setting, crop fallback, _set_geometry/_maybe_crop helpers
- `tests/test_paper_sizes.py` - 17 tests for paper size types, dimensions, cropping, config
- `tests/test_scanner.py` - 7 tests for geometry setting, crop fallback, ADF behavior

## Decisions Made
- Used inline `Literal["full", "a3", "a4", "a5", "letter", "legal"]` in ProfileConfig field annotation instead of importing PaperSize type alias, because Pydantic with `from __future__ import annotations` requires type resolution at model creation time, and TC001 forbids runtime imports of type-only values
- Added geometry attributes (tl_x, tl_y, br_x, br_y) to SaneDevice Protocol to enable type-safe attribute access without type: ignore comments
- Extracted `_set_geometry()` and `_maybe_crop()` module-level helpers from scan_pages() to stay under ruff PLR0912 (12 branches) and PLR0915 (50 statements) limits
- Used `model_validate()` instead of direct constructor for invalid paper_size test to satisfy ty type checker's Literal narrowing

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] PLR0912/PLR0915 scan_pages complexity after geometry addition**
- **Found during:** Task 2
- **Issue:** Adding geometry setting and crop fallback inline pushed scan_pages over 12 branches and 50 statements
- **Fix:** Extracted _set_geometry() and _maybe_crop() as module-level helper functions
- **Files modified:** src/saneless/scanner/sane_backend.py
- **Verification:** `uv run ruff check src/saneless/scanner/sane_backend.py` exits 0
- **Committed in:** 0b86652

**2. [Rule 3 - Blocking] FBT001 boolean positional argument**
- **Found during:** Task 2
- **Issue:** `geometry_set: bool` as positional arg triggers ruff FBT001
- **Fix:** Made it keyword-only with `*` separator
- **Files modified:** src/saneless/scanner/sane_backend.py
- **Committed in:** 0b86652

---

**Total deviations:** 2 auto-fixed (2 blocking lint issues)
**Impact on plan:** Both auto-fixes required for lint compliance. No scope creep.

## Issues Encountered
- TC001 vs Pydantic runtime type resolution: PaperSize import needed at runtime for Pydantic model creation but TC001 wanted it in TYPE_CHECKING -- resolved by using inline Literal
- ty type narrowing on test invalid value: `bad_value: str = "invalid"` still narrowed to `Literal["invalid"]` -- resolved by using `model_validate()` dict API

## User Setup Required

None - no external service configuration required.

## Known Stubs

None - all data flows are fully wired.

## Next Phase Readiness
- Paper size configuration complete, ready for web UI integration (18-02)
- Auto-generated profiles unaffected (paper_size="full" default, nothing written)
- All existing tests pass with no regressions (339 total)

---
*Phase: 18-automatic-scanned-page-size-detection-or-user-specified-paper-size-to-avoid-capturing-the-full-scanner-bed*
*Completed: 2026-03-24*
