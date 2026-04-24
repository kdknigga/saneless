---
phase: 03-web-ui
plan: 03
subsystem: testing
tags: [fastapi, testclient, pytest, cache, sqlite]

requires:
  - phase: 03-web-ui/03-01
    provides: "FastAPI app factory, routes, templates, cache"
  - phase: 03-web-ui/03-02
    provides: "HTMX templates, flip prompt, job history partials"
provides:
  - "Full test coverage for all Phase 3 web endpoints"
  - "MetadataCache unit tests with TTL and invalidation"
  - "JobStore list_recent and prune test coverage"
affects: [04-packaging]

tech-stack:
  added: []
  patterns: ["StubScanner concrete ABC for TestClient lifespan tests", "mock_paperless fixture via lambda patching"]

key-files:
  created: []
  modified:
    - tests/test_web.py
    - tests/test_cache.py
    - tests/test_job.py

key-decisions:
  - "StubScanner concrete class instead of MagicMock to avoid ABC issues with lifespan"
  - "Direct job_store manipulation for state-dependent tests instead of form submission (worker processes too fast)"

patterns-established:
  - "StubScanner: concrete ScannerBackend for web integration tests"
  - "mock_paperless: lambda patching for paperless API isolation"

requirements-completed: [UI-01, UI-02, UI-03, UI-04, UI-05, UI-06, UI-07, UI-08, PROF-03, PLSS-04, PLSS-05, HLTH-01, HLTH-02, LOG-03]

duration: 5min
completed: 2026-03-20
---

# Phase 3 Plan 03: Web Test Suite Summary

**25 tests covering all web endpoints, cache TTL/invalidation, and JobStore list/prune -- replacing all xfail stubs with passing implementations**

## Performance

- **Duration:** 5 min
- **Started:** 2026-03-20T21:07:33Z
- **Completed:** 2026-03-20T21:13:04Z
- **Tasks:** 2
- **Files modified:** 3

## Accomplishments
- 15 web endpoint tests covering all route handlers (index, health, scan, status, flip, cache, history)
- 5 MetadataCache tests verifying TTL expiry, invalidation, and per-resource isolation
- 5 JobStore tests verifying list_recent ordering/limits and prune by age/count
- Full suite 178 tests green with zero regressions

## Task Commits

Each task was committed atomically:

1. **Task 1: Web endpoint tests with FastAPI TestClient** - `f20ca77` (test)
2. **Task 2: Cache and JobStore extension tests** - `94d4217` (test)

## Files Created/Modified
- `tests/test_web.py` - 15 endpoint tests with StubScanner and mock_paperless fixtures
- `tests/test_cache.py` - 5 MetadataCache unit tests (TTL, invalidation, per-resource)
- `tests/test_job.py` - 5 JobStore tests (list_recent, prune by age/count)

## Decisions Made
- Used StubScanner concrete class inheriting ScannerBackend instead of MagicMock to avoid ABC metaclass issues when TestClient enters lifespan
- Used direct job_store manipulation for state-dependent tests (flip prompt, thumbnail, error display, active job) since the worker processes stub scan jobs too fast for form-submission-based testing

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed test_status_polling_active_job using direct state setup**
- **Found during:** Task 1 (web endpoint tests)
- **Issue:** Submitting via POST /api/scan and then polling returned DONE state because StubScanner completes instantly
- **Fix:** Directly create job in store, set SCANNING state, and set worker._current_job_id
- **Files modified:** tests/test_web.py
- **Verification:** Test passes, polling attributes present in response
- **Committed in:** f20ca77

---

**Total deviations:** 1 auto-fixed (1 bug)
**Impact on plan:** Necessary for test correctness with fast stub scanner. No scope creep.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- All Phase 3 requirements verified with automated tests
- Full test suite green (178 tests), ready for Phase 4 packaging

---
*Phase: 03-web-ui*
*Completed: 2026-03-20*
