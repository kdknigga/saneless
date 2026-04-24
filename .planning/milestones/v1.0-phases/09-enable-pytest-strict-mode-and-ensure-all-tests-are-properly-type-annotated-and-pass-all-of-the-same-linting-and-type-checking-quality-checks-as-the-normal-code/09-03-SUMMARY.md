---
phase: 09-enable-pytest-strict-mode
plan: 03
subsystem: testing
tags: [pytest, strict-mode, filterwarnings, sqlite, resource-cleanup]

requires:
  - phase: 09-02
    provides: "Type-annotated and lint-compliant test files"
provides:
  - "Pytest strict mode with all strictness settings enabled"
  - "filterwarnings=error catching all warnings as failures"
  - "Zero unclosed SQLite connections in test suite"
  - "Complete quality parity between tests/ and src/"
affects: []

tech-stack:
  added: []
  patterns:
    - "try/finally with store.close() for all JobStore instances in tests"
    - "_capture_uvicorn helper for serve command tests to prevent resource leaks"

key-files:
  created: []
  modified:
    - pyproject.toml
    - tests/test_job.py
    - tests/test_cli.py

key-decisions:
  - "Type-narrowing with isinstance(app, FastAPI) in _capture_uvicorn to satisfy ty checker while closing eagerly-created JobStore"
  - "Zero warning allowlisting -- all ResourceWarnings fixed at source rather than suppressed"

patterns-established:
  - "Every JobStore() instantiation in tests must have a corresponding .close() via try/finally or fixture teardown"

requirements-completed: [TQUAL-06, TQUAL-07]

duration: 7min
completed: 2026-03-21
---

# Phase 09 Plan 03: Pytest Strict Mode Summary

**Pytest strict mode (strict_markers, strict_config, xfail_strict, filterwarnings=error) enabled with zero suppressed warnings across all 226 tests**

## Performance

- **Duration:** 7 min
- **Started:** 2026-03-21T21:32:37Z
- **Completed:** 2026-03-21T21:39:35Z
- **Tasks:** 2
- **Files modified:** 3

## Accomplishments
- Fixed all unclosed SQLite connections in test_job.py (5 functions) and test_cli.py (6 serve tests)
- Enabled full pytest strict configuration: strict_markers, strict_config, xfail_strict, filterwarnings=error
- All 226 tests pass under maximum strictness with zero warning filters or suppressions
- Complete quality gate green: ruff check, ruff format, ty, pyrefly, pytest all pass

## Task Commits

Each task was committed atomically:

1. **Task 1: Fix ResourceWarning from unclosed SQLite connections in tests** - `6546c3d` (fix)
2. **Task 2: Enable pytest strict configuration and run full quality gate** - `d4720b7` (feat)

## Files Created/Modified
- `pyproject.toml` - Added strict_markers, strict_config, xfail_strict, filterwarnings=["error"], addopts
- `tests/test_job.py` - Added try/finally with store.close() to 5 standalone test functions
- `tests/test_cli.py` - Refactored serve tests with _capture_uvicorn helper that closes eagerly-created JobStore

## Decisions Made
- Used isinstance type narrowing (FastAPI, JobStore) in _capture_uvicorn to satisfy ty type checker while closing leaked connections
- Fixed all ResourceWarnings at source rather than adding any warning filters or suppressions

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
- ty type checker rejected `app.state.job_store.close()` when app typed as `object` -- resolved with isinstance narrowing through FastAPI then JobStore types (committed with PLC0415 noqa for local import)

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- Phase 09 complete: all three plans executed successfully
- Tests/ and src/ now held to identical quality standards (except S101/S104/S105/S106 per-file-ignores for test-specific patterns)
- All five quality tools pass clean: ruff check, ruff format, ty, pyrefly, pytest

---
*Phase: 09-enable-pytest-strict-mode*
*Completed: 2026-03-21*
