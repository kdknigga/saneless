---
phase: 17-fix-paperless-upload-error
plan: 01
subsystem: api
tags: [httpx, paperless-ngx, multipart, datetime]

# Dependency graph
requires:
  - phase: 01-core-pipeline
    provides: PaperlessClient upload_document and pipeline upload paths
provides:
  - Correct date-only format (YYYY-MM-DD) for paperless-ngx created field
  - Idiomatic httpx data= + files= multipart encoding
affects: []

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "httpx data= for form fields, files= for binary uploads (not combined files= list)"

key-files:
  created: []
  modified:
    - src/saneless/paperless.py
    - src/saneless/pipeline.py
    - tests/test_paperless.py

key-decisions:
  - "Removed FileTypes import entirely rather than keeping TYPE_CHECKING guard"
  - "Tags sent as list in data dict; httpx handles repeated field encoding"

patterns-established:
  - "httpx multipart: data= for form fields, files= for binary content"

requirements-completed: [D-01, D-02, D-03, D-04, D-05, D-06]

# Metrics
duration: 2min
completed: 2026-03-23
---

# Phase 17 Plan 01: Fix Paperless Upload Error Summary

**Date-only strftime format and idiomatic httpx data=/files= split for paperless-ngx uploads**

## Performance

- **Duration:** 2 min
- **Started:** 2026-03-23T02:19:42Z
- **Completed:** 2026-03-23T02:21:40Z
- **Tasks:** 2
- **Files modified:** 3

## Accomplishments
- Fixed datetime format from full ISO 8601 to date-only YYYY-MM-DD at both pipeline call sites
- Refactored upload_document to use idiomatic httpx data= for form fields and files= for PDF binary
- Removed FileTypes type annotation workaround that was no longer needed
- Added test proving form fields lack filename attribute (data=) while PDF has one (files=)

## Task Commits

Each task was committed atomically:

1. **Task 1: Fix upload_document multipart encoding and datetime format** - `2ee1e8f` (fix)
2. **Task 2: Add and update tests for date format and data/files separation** - `1681f4f` (test)

## Files Created/Modified
- `src/saneless/paperless.py` - Refactored upload_document to use data= + files= split, removed FileTypes import
- `src/saneless/pipeline.py` - Changed both datetime.now().isoformat() to strftime("%Y-%m-%d")
- `tests/test_paperless.py` - Added date-only assertion and test_form_fields_sent_as_data_not_files

## Decisions Made
- Removed FileTypes import entirely rather than keeping TYPE_CHECKING guard -- no longer needed after removing the combined files= list pattern
- Tags sent as list in data dict -- httpx handles repeated field encoding natively

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Paperless-ngx upload path is now correct for both date format and multipart encoding
- All 55 upload-related tests pass with zero regressions

---
*Phase: 17-fix-paperless-upload-error*
*Completed: 2026-03-23*

## Self-Check: PASSED
