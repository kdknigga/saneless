---
phase: 16-when-a-scanner-advertised-auto-mode-it-should-be-configurable-by-the-user-if-that-means-flatbed-mode-or-adf-mode
plan: 02
subsystem: scanner
tags: [auto-profiles, toml, sane, configuration, documentation]

requires:
  - phase: 16-01
    provides: "ProfileConfig.auto_source_mode field, ScanSettings.auto_source_mode, pipeline wiring, scan_pages routing"
provides:
  - "Auto source slug mapping (source_to_slug 'Auto' -> 'auto-scan')"
  - "Smart auto_source_mode defaulting in generate_profiles based on available sources"
  - "TOML persistence of auto_source_mode for non-default values"
  - "Configuration reference documentation for auto_source_mode field"
affects: []

tech-stack:
  added: []
  patterns:
    - "kwargs dict pattern for conditional ProfileConfig construction"
    - "Non-default-only TOML writing to keep config clean"

key-files:
  created: []
  modified:
    - "src/saneless/auto_profiles.py"
    - "tests/test_auto_profiles.py"
    - "docs/reference/configuration.md"

key-decisions:
  - "Auto source placed before flatbed check in source_to_slug to prevent 'auto' matching 'flatbed' pattern"
  - "Only write auto_source_mode to TOML when non-default (adf) to keep config clean"
  - "Auto-only scanner (no Flatbed, no ADF) defaults to adf mode for multi-page capability"

patterns-established:
  - "kwargs dict for conditional field injection into Pydantic model construction"

requirements-completed: [D-07, D-08, D-09, D-10]

duration: 2min
completed: 2026-03-22
---

# Phase 16 Plan 02: Auto Source Profile Generation Summary

**Auto source slug mapping, smart auto_source_mode defaults in profile generation, TOML persistence of non-default values, and config reference documentation**

## Performance

- **Duration:** 2 min
- **Started:** 2026-03-22T23:08:02Z
- **Completed:** 2026-03-22T23:10:30Z
- **Tasks:** 2
- **Files modified:** 3

## Accomplishments
- source_to_slug("Auto") returns "auto-scan" with case-insensitive matching
- generate_profiles smartly defaults auto_source_mode based on other available sources (flatbed if Flatbed exists, adf otherwise)
- write_profiles_to_config persists auto_source_mode only when non-default to keep TOML clean
- Configuration reference documents auto_source_mode field with description and example profile

## Task Commits

Each task was committed atomically:

1. **Task 1: Add Auto source handling (RED)** - `d7bfa2d` (test)
2. **Task 1: Add Auto source handling (GREEN)** - `12ded9c` (feat)
3. **Task 2: Document auto_source_mode in config reference** - `bf6a5f8` (docs)

## Files Created/Modified
- `src/saneless/auto_profiles.py` - Auto source slug, smart defaults in generate_profiles, TOML persistence
- `tests/test_auto_profiles.py` - 8 new tests for Auto source handling (slug, generation, TOML writing)
- `docs/reference/configuration.md` - auto_source_mode field in profiles table and example profile

## Decisions Made
- Auto source check placed before flatbed check in source_to_slug to prevent "auto" matching the "flatbed" substring pattern
- Only write auto_source_mode to TOML when value is "adf" (non-default) to keep config files clean
- When scanner has only "Auto" source (no explicit Flatbed or ADF), default to "adf" for multi-page scanning capability

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Phase 16 complete: Auto source mode is fully configurable per profile
- All config, routing, profile generation, and documentation in place
- 314 tests pass with zero regressions

---
*Phase: 16-when-a-scanner-advertised-auto-mode-it-should-be-configurable-by-the-user-if-that-means-flatbed-mode-or-adf-mode*
*Completed: 2026-03-22*
