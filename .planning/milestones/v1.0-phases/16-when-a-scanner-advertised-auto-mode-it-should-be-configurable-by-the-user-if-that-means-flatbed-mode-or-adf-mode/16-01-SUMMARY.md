---
phase: 16-when-a-scanner-advertised-auto-mode-it-should-be-configurable-by-the-user-if-that-means-flatbed-mode-or-adf-mode
plan: 01
subsystem: scanner
tags: [sane, config, routing, adf, flatbed, auto-source]

# Dependency graph
requires:
  - phase: 02-adf-scanning
    provides: ADF scan path and _is_adf_source routing
  - phase: 01-core-pipeline
    provides: ProfileConfig, ScanSettings, pipeline orchestration
provides:
  - auto_source_mode config field on ProfileConfig (Literal["flatbed", "adf"])
  - auto_source_mode field on ScanSettings dataclass
  - Auto source conditional routing in scan_pages()
affects: [16-02-web-ui-auto-source, documentation]

# Tech tracking
tech-stack:
  added: []
  patterns: [config-driven scan routing via effective_source override]

key-files:
  created: []
  modified:
    - src/saneless/config.py
    - src/saneless/scanner/base.py
    - src/saneless/pipeline.py
    - src/saneless/scanner/sane_backend.py
    - tests/test_config.py
    - tests/test_scanner.py

key-decisions:
  - "Auto source override placed after _is_adf_source() call, not inside it, preserving D-05"
  - "Literal type on ProfileConfig for validation; plain str on ScanSettings dataclass for simplicity"

patterns-established:
  - "Config-driven routing: effective_source == 'Auto' guard for conditional override"

requirements-completed: [D-01, D-02, D-03, D-04, D-05, D-06]

# Metrics
duration: 2min
completed: 2026-03-22
---

# Phase 16 Plan 01: Auto Source Mode Config and Routing Summary

**auto_source_mode Literal field on ProfileConfig/ScanSettings with conditional routing in scan_pages() for Auto source to flatbed or ADF path**

## Performance

- **Duration:** 2 min
- **Started:** 2026-03-22T23:03:02Z
- **Completed:** 2026-03-22T23:04:56Z
- **Tasks:** 2
- **Files modified:** 6

## Accomplishments
- ProfileConfig validates auto_source_mode as Literal["flatbed", "adf"] with "flatbed" default
- ScanSettings carries auto_source_mode through pipeline to scan_pages()
- scan_pages() routes Auto source to ADF or flatbed path based on config
- Explicit sources (Flatbed, ADF, ADF Duplex) completely unaffected

## Task Commits

Each task was committed atomically:

1. **Task 1: Add auto_source_mode to ProfileConfig, ScanSettings, and pipeline bridge** - `1477686` (feat)
2. **Task 2: Implement Auto source conditional routing in scan_pages()** - `55c09eb` (feat)

_Note: TDD tasks - tests written first (RED), then implementation (GREEN), committed together._

## Files Created/Modified
- `src/saneless/config.py` - Added auto_source_mode Literal field to ProfileConfig
- `src/saneless/scanner/base.py` - Added auto_source_mode str field to ScanSettings dataclass
- `src/saneless/pipeline.py` - Wired profile.auto_source_mode to ScanSettings constructor
- `src/saneless/scanner/sane_backend.py` - Auto source conditional routing after _is_adf_source()
- `tests/test_config.py` - ProfileConfig auto_source_mode validation tests
- `tests/test_scanner.py` - ScanSettings field tests and Auto source routing tests

## Decisions Made
- Auto source override placed after `_is_adf_source()` call rather than modifying that function, preserving D-05 (function body unchanged)
- Used `Literal["flatbed", "adf"]` on ProfileConfig for Pydantic validation; plain `str` on ScanSettings dataclass for simplicity since validation happens at config layer

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Auto source routing backend complete, ready for web UI integration (plan 16-02)
- Users can set `auto_source_mode = "adf"` in TOML config to route Auto source to ADF scanning

---
*Phase: 16-when-a-scanner-advertised-auto-mode-it-should-be-configurable-by-the-user-if-that-means-flatbed-mode-or-adf-mode*
*Completed: 2026-03-22*
