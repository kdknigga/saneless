---
phase: 10-automatic-scanner-profile-creation
plan: 01
subsystem: config
tags: [tomlkit, scanner-profiles, toml, pydantic, tdd]

# Dependency graph
requires:
  - phase: 01-core-pipeline
    provides: "DeviceCapabilities dataclass from scanner abstraction layer"
  - phase: 01-core-pipeline
    provides: "ProfileConfig and Settings from config module"
provides:
  - "auto_profiles module with pure profile generation functions"
  - "source_to_slug mapping for SANE source names"
  - "Comment-preserving TOML config writing via tomlkit"
  - "auto_generated field on ProfileConfig"
  - "is_bare_default detection for uncustomized profiles"
affects: [10-02]

# Tech tracking
tech-stack:
  added: [tomlkit]
  patterns: [cast for tomlkit Container type narrowing, fallback slugification for unknown sources]

key-files:
  created:
    - src/saneless/auto_profiles.py
    - tests/test_auto_profiles.py
  modified:
    - src/saneless/config.py
    - pyproject.toml
    - uv.lock

key-decisions:
  - "Top-level imports for ProfileConfig/Settings (no circular dependency), TYPE_CHECKING for DeviceCapabilities"
  - "ADF Back excluded from simplex matching via early 'back' check before ADF pattern"
  - "cast() for tomlkit Container to satisfy ty type checker on 'in' operator"

patterns-established:
  - "tomlkit cast pattern: cast('dict[str, object]', doc[section]) for ty compatibility"
  - "Pure function profile generation: no I/O in generate_profiles, separate write step"

requirements-completed: [AP-01, AP-02, AP-03, AP-04, AP-05, AP-08, AP-09]

# Metrics
duration: 4min
completed: 2026-03-22
---

# Phase 10 Plan 01: Auto Profile Generation Summary

**Pure-function scanner profile generation with tomlkit TOML persistence and 28 TDD tests**

## Performance

- **Duration:** 4 min
- **Started:** 2026-03-21T23:57:15Z
- **Completed:** 2026-03-22T00:01:48Z
- **Tasks:** 1
- **Files modified:** 5

## Accomplishments
- Created auto_profiles module with 7 exported pure functions for scanner-to-profile mapping
- Added auto_generated bool field to ProfileConfig (backward compatible with existing configs)
- 28 unit tests covering all generation logic, slug mapping, resolution/mode selection, bare default detection, and comment-preserving TOML persistence
- All linters (ruff), type checkers (ty, pyrefly), and 254 total tests pass clean

## Task Commits

Each task was committed atomically:

1. **Task 1: Add tomlkit dependency, auto_generated field, config path resolver, and pure generation functions with tests** - `0dded8c` (feat)

**Plan metadata:** (pending)

_Note: TDD task with single commit covering RED+GREEN+REFACTOR phases_

## Files Created/Modified
- `src/saneless/auto_profiles.py` - Core auto profile generation module with 7 pure functions
- `src/saneless/config.py` - Added auto_generated: bool = False to ProfileConfig
- `tests/test_auto_profiles.py` - 28 unit tests across 7 test classes
- `pyproject.toml` - Added tomlkit dependency
- `uv.lock` - Updated lockfile

## Decisions Made
- Top-level imports for ProfileConfig/Settings since no circular dependency exists; DeviceCapabilities in TYPE_CHECKING since only used in annotations
- ADF Back excluded from simplex pattern by checking "back" before "adf" in source_to_slug
- Used cast("dict[str, object]") for tomlkit Container to satisfy ty type checker unsupported-operator diagnostic

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] ADF Back incorrectly matched as adf-simplex**
- **Found during:** Task 1 (TDD GREEN phase)
- **Issue:** source_to_slug("ADF Back") returned "adf-simplex" because "adf" matched before fallback
- **Fix:** Added early "back" check before ADF pattern to route to fallback slugification
- **Files modified:** src/saneless/auto_profiles.py
- **Verification:** Test test_adf_back_fallback passes
- **Committed in:** 0dded8c

---

**Total deviations:** 1 auto-fixed (1 bug)
**Impact on plan:** Minor ordering fix in pattern matching. No scope creep.

## Issues Encountered
None beyond the ADF Back slug ordering issue (handled as deviation above).

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- auto_profiles module ready for integration in Plan 02 (CLI command and startup hook)
- All pure functions tested and type-checked
- ProfileConfig.auto_generated field available for downstream use

---
*Phase: 10-automatic-scanner-profile-creation*
*Completed: 2026-03-22*
