---
phase: 06-gap-closure-fixes
plan: 02
subsystem: api
tags: [paperless-ngx, httpx, connection-test, auth]

requires:
  - phase: 06-01
    provides: "Paperless connection test web route with 502 error handling"
provides:
  - "Fixed test_connection() that correctly verifies auth tokens via /api/tags/"
affects: [web-ui, testing]

tech-stack:
  added: []
  patterns: ["Use auth-requiring endpoint for connection validation"]

key-files:
  created: []
  modified:
    - src/saneless/paperless.py
    - tests/test_paperless.py

key-decisions:
  - "Use /api/tags/?page_size=1 instead of /api/ for connection testing -- DRF browsable API root returns 200 regardless of auth"

patterns-established:
  - "Auth verification: always test against an endpoint that enforces auth, not the API root"

requirements-completed: [PLSS-03]

duration: 1min
completed: 2026-03-21
---

# Phase 06 Plan 02: Fix Paperless Connection Test Summary

**test_connection() now hits /api/tags/?page_size=1 to correctly distinguish valid vs invalid tokens**

## Performance

- **Duration:** 1 min
- **Started:** 2026-03-21T19:30:20Z
- **Completed:** 2026-03-21T19:31:23Z
- **Tasks:** 1
- **Files modified:** 2

## Accomplishments
- Fixed test_connection() to use auth-requiring endpoint /api/tags/?page_size=1
- Added 403 test case (previously only tested 401 for token rejection)
- All 4 connection test cases pass with URL and query param assertions

## Task Commits

Each task was committed atomically:

1. **Task 1 (RED): Failing tests for auth-requiring endpoint** - `c771063` (test)
2. **Task 1 (GREEN): Fix test_connection implementation** - `a3b538c` (feat)

## Files Created/Modified
- `src/saneless/paperless.py` - Changed test_connection() from /api/ to /api/tags/?page_size=1
- `tests/test_paperless.py` - Updated mock handlers to assert correct URL and added 403 test case

## Decisions Made
- Use /api/tags/?page_size=1 instead of /api/ -- the DRF browsable API root returns 200 regardless of auth token validity, making it useless for connection verification

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Phase 06 gap closure complete
- Connection test now correctly validates auth tokens
- All paperless and web tests passing

---
*Phase: 06-gap-closure-fixes*
*Completed: 2026-03-21*
