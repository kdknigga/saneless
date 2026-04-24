---
phase: 04-packaging-and-deployment
plan: 01
subsystem: cli
tags: [click, sqlite, jobs, consume-dir]

requires:
  - phase: 01-core-pipeline
    provides: "JobStore, Job, JobState models with SQLite persistence"
  - phase: 01-core-pipeline
    provides: "PaperlessClient with consume_dir fallback"
provides:
  - "`saneless jobs` CLI command with --json and --limit flags"
  - "Consume directory auto-creation on first fallback use"
affects: [04-packaging-and-deployment]

tech-stack:
  added: []
  patterns: ["CLI command pattern with JobStore DB path matching web layer"]

key-files:
  created: []
  modified:
    - src/saneless/cli.py
    - src/saneless/paperless.py
    - tests/test_cli.py
    - tests/test_paperless.py

key-decisions:
  - "DB path for jobs command matches web layer: tmp_dir/saneless.db"

patterns-established:
  - "CLI jobs command follows devices command pattern with --json and table output"

requirements-completed: [CLI-03, PLSS-06]

duration: 3min
completed: 2026-03-20
---

# Phase 04 Plan 01: Jobs CLI Command and Consume Dir Fallback Summary

**`saneless jobs` CLI command with table/JSON output and consume directory auto-creation on fallback**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-20T21:46:14Z
- **Completed:** 2026-03-20T21:49:42Z
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments
- Added `saneless jobs` command showing scan history as table with Timestamp, Profile, Title, Status columns
- Added `--json` flag for machine-readable JSON array output and `--limit N` flag to control row count
- Consume directory is auto-created with `mkdir(parents=True)` on first fallback use with warning log

## Task Commits

Each task was committed atomically:

1. **Task 1: Add `saneless jobs` CLI command** - `09b47eb` (feat)
2. **Task 2: Complete consume directory fallback with startup validation** - `31e3021` (feat)

_Both tasks followed TDD: RED (failing tests) -> GREEN (implementation) -> verify_

## Files Created/Modified
- `src/saneless/cli.py` - Added `jobs` command with `--json` and `--limit` options
- `src/saneless/paperless.py` - Added `mkdir(parents=True)` before consume dir fallback copy
- `tests/test_cli.py` - Added `TestJobsCommand` class with 7 test methods
- `tests/test_paperless.py` - Added `TestConsumeDir` class with 3 test methods

## Decisions Made
- DB path for jobs command uses `str(Path(settings.output.tmp_dir) / "saneless.db")` matching web layer exactly

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- CLI jobs command complete, ready for packaging
- All CLI commands (scan, devices, jobs) functional
- Consume directory fallback robust with auto-creation

---
*Phase: 04-packaging-and-deployment*
*Completed: 2026-03-20*
