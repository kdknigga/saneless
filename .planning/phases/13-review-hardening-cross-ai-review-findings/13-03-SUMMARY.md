---
phase: 13-review-hardening-cross-ai-review-findings
plan: 03
subsystem: pipeline
tags: [duplex, scanning, error-recovery, paperless-ngx]

requires:
  - phase: 13-02
    provides: PipelineEvent StrEnum and typed status_callback
provides:
  - Duplex mismatch recovery handler that saves partial PDFs
  - Both passes uploaded to paperless-ngx as separate documents
  - DONE status with warning message instead of ScanError on mismatch
affects: []

tech-stack:
  added: []
  patterns:
    - "Tuple return for multi-path results (list | tuple) instead of union with str"
    - "Extract helper (_resolve_device) to stay under ruff complexity limits"

key-files:
  created: []
  modified:
    - src/saneless/pipeline.py
    - tests/test_pipeline.py

key-decisions:
  - "_scan_manual_duplex returns (fronts, backs) tuple on mismatch instead of raising ScanError or returning str"
  - "Extracted _resolve_device() from run_pipeline to stay under PLR0912/PLR0915 complexity limits"
  - "Used tuple[list, list] as passes param to _handle_duplex_mismatch to stay under PLR0913 arg limit"

patterns-established:
  - "Graceful degradation: save partial data on mismatch instead of discarding all scanned pages"

requirements-completed: [RH-02]

duration: 5min
completed: 2026-03-22
---

# Phase 13 Plan 03: Duplex Mismatch Recovery Summary

**Duplex page count mismatch saves fronts and backs as separate PDFs to paperless-ngx instead of raising ScanError**

## Performance

- **Duration:** 5 min
- **Started:** 2026-03-22T14:50:38Z
- **Completed:** 2026-03-22T14:56:22Z
- **Tasks:** 1
- **Files modified:** 2

## Accomplishments
- Added `_handle_duplex_mismatch()` that assembles fronts and backs as separate PDFs and uploads both to paperless-ngx
- Changed `_scan_manual_duplex()` to return a tuple of (fronts, backs) on mismatch instead of propagating ScanError
- Pipeline returns `{"status": "DONE", "warning": "Page count mismatch: N fronts, M backs. Partial PDFs saved."}` on mismatch
- Normal duplex (matching counts) still interleaves correctly via existing `_interleave_duplex`
- Extracted `_resolve_device()` helper to keep `run_pipeline` under ruff complexity limits

## Task Commits

Each task was committed atomically:

1. **Task 1: Duplex mismatch recovery -- save partial PDFs and upload both** - `b7e5207` (feat)

**Plan metadata:** pending (docs: complete plan)

## Files Created/Modified
- `src/saneless/pipeline.py` - Added `_handle_duplex_mismatch()`, `_resolve_device()`, modified `_scan_manual_duplex()` return type and `run_pipeline()` mismatch handling
- `tests/test_pipeline.py` - Added `test_duplex_mismatch_saves_partial_pdfs` and `test_duplex_match_still_interleaves_normally`, replaced old `test_manual_duplex_count_mismatch`

## Decisions Made
- `_scan_manual_duplex` returns `list[Image.Image] | tuple[list[Image.Image], list[Image.Image]]` -- a tuple on mismatch, a list on success. This avoids changing the function signature to accept recovery-context parameters.
- Extracted `_resolve_device()` from `run_pipeline` to reduce branch/statement count under ruff PLR0912/PLR0915 limits.
- Used `tuple[list, list]` as a single `passes` parameter to `_handle_duplex_mismatch` to stay within the 5-argument PLR0913 limit.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Refactored for ruff complexity limits**
- **Found during:** Task 1
- **Issue:** Adding mismatch recovery logic pushed `run_pipeline` over PLR0912 (13 branches > 12) and PLR0915 (53 statements > 50), and `_handle_duplex_mismatch`/`_scan_manual_duplex` exceeded PLR0913 (too many arguments)
- **Fix:** Extracted `_resolve_device()` helper from `run_pipeline`; changed `_scan_manual_duplex` to return tuple instead of accepting recovery params; bundled fronts/backs into single tuple param for `_handle_duplex_mismatch`
- **Files modified:** src/saneless/pipeline.py
- **Verification:** `uv run ruff check src/saneless/pipeline.py` passes clean
- **Committed in:** b7e5207

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** Refactoring was necessary to satisfy ruff linting. No scope creep.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Phase 13 complete -- all 3 plans executed
- All review hardening findings addressed

---
*Phase: 13-review-hardening-cross-ai-review-findings*
*Completed: 2026-03-22*
