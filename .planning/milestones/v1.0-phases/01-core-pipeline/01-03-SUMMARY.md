---
phase: 01-core-pipeline
plan: 03
subsystem: cli, pipeline
tags: [click, sqlite, threading, queue, pipeline, cli]

# Dependency graph
requires:
  - phase: 01-core-pipeline plan 01
    provides: config loading, exceptions, logging setup
  - phase: 01-core-pipeline plan 02
    provides: scanner abstraction, PDF assembly, paperless client
provides:
  - Job model with SQLite persistence and state machine
  - Background worker thread with queue-based job processing
  - Pipeline orchestration (scan -> PDF -> upload) with temp cleanup
  - Click CLI with scan and devices commands
  - Entry point wiring (saneless:main -> cli)
affects: [02-adf-duplex, 03-web-ui, 04-packaging]

# Tech tracking
tech-stack:
  added: [click (CLI framework), sqlite3 (job persistence)]
  patterns: [TDD red-green, TemporaryDirectory cleanup, daemon thread worker, status callback pattern]

key-files:
  created:
    - src/saneless/job.py
    - src/saneless/pipeline.py
    - src/saneless/worker.py
    - src/saneless/cli.py
    - tests/test_worker.py
    - tests/test_pipeline.py
    - tests/test_cli.py
  modified:
    - src/saneless/__init__.py
    - tests/conftest.py

key-decisions:
  - "SQLite check_same_thread=False for cross-thread worker access"
  - "Suppress discovery message in JSON output mode for clean parsing"
  - "Lazy cli import in __init__.py main() to avoid loading all deps on package import"

patterns-established:
  - "Pipeline status callbacks: Scanning... -> Assembling PDF... -> Uploading to paperless-ngx... -> Done: {title}"
  - "Exit codes: 0 success, 1 scan error, 2 config error, 3 paperless error"
  - "Worker thread with sentinel None for clean shutdown"

requirements-completed: [ARCH-02, CLI-01, CLI-02]

# Metrics
duration: 6min
completed: 2026-03-20
---

# Phase 01 Plan 03: Pipeline Integration Summary

**Click CLI wiring scan/devices commands through job model, worker thread, and pipeline orchestration with SQLite persistence and TemporaryDirectory cleanup**

## Performance

- **Duration:** 6 min
- **Started:** 2026-03-20T17:03:51Z
- **Completed:** 2026-03-20T17:09:25Z
- **Tasks:** 2
- **Files modified:** 9

## Accomplishments
- End-to-end scan pipeline: `saneless scan --title X` orchestrates scan -> PDF -> upload with status output
- Device discovery: `saneless devices` with table, `--json`, and `--capabilities` output modes
- Job model with SQLite persistence and state machine (PENDING through DONE/ERROR)
- Background worker thread processing jobs from queue.Queue
- Full test coverage: 30 new tests (15 worker/pipeline + 15 CLI), 85 total tests passing

## Task Commits

Each task was committed atomically (TDD: test then feat):

1. **Task 1: Job model, worker thread, and pipeline orchestration**
   - `aa6d1b4` (test) - Failing tests for job, worker, pipeline
   - `b95f88f` (feat) - Implementation with SQLite persistence, TemporaryDirectory cleanup
2. **Task 2: Click CLI commands and entry point wiring**
   - `e88a2b3` (test) - Failing tests for CLI commands
   - `4d0cd68` (feat) - Click CLI with scan/devices, entry point wiring

## Files Created/Modified
- `src/saneless/job.py` - Job model with JobState enum, JobStore SQLite persistence
- `src/saneless/pipeline.py` - Pipeline orchestration: scan -> assemble -> upload with temp cleanup
- `src/saneless/worker.py` - Background daemon thread consuming jobs from Queue
- `src/saneless/cli.py` - Click CLI group with scan and devices commands
- `src/saneless/__init__.py` - Entry point wired: main() -> cli() via lazy import
- `tests/test_worker.py` - Job state machine, JobStore persistence, worker thread tests
- `tests/test_pipeline.py` - Pipeline happy path, errors, cleanup, status callback tests
- `tests/test_cli.py` - CLI help, scan, devices, flags, exit code tests
- `tests/conftest.py` - Added shared fixtures: default_settings, mock_scanner, mock_paperless

## Decisions Made
- Used `check_same_thread=False` for SQLite to support worker thread accessing JobStore created in main thread
- Suppressed "Discovering scanners..." message in `--json` mode for clean JSON output parsing
- Used lazy `from .cli import cli` in `__init__.py` to avoid loading Click and all deps on bare package import

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] SQLite cross-thread access**
- **Found during:** Task 1 (worker thread tests)
- **Issue:** SQLite default `check_same_thread=True` prevented worker thread from updating job state
- **Fix:** Added `check_same_thread=False` to sqlite3.connect()
- **Files modified:** src/saneless/job.py
- **Verification:** Worker tests pass with cross-thread JobStore access
- **Committed in:** b95f88f

**2. [Rule 1 - Bug] JSON output polluted by discovery message**
- **Found during:** Task 2 (devices --json test)
- **Issue:** "Discovering scanners..." printed before JSON output, breaking json.loads()
- **Fix:** Only print discovery message in non-JSON mode
- **Files modified:** src/saneless/cli.py
- **Verification:** test_devices_json_output passes with clean JSON
- **Committed in:** 4d0cd68

**3. [Rule 3 - Blocking] Click CliRunner mix_stderr not available**
- **Found during:** Task 2 (CLI test execution)
- **Issue:** Click 8.3.1 CliRunner does not support `mix_stderr` parameter
- **Fix:** Removed mix_stderr=False, changed stderr assertions to check result.output
- **Files modified:** tests/test_cli.py
- **Verification:** All 15 CLI tests pass
- **Committed in:** 4d0cd68

---

**Total deviations:** 3 auto-fixed (2 bugs, 1 blocking)
**Impact on plan:** All auto-fixes necessary for correctness. No scope creep.

## Issues Encountered
None beyond the auto-fixed deviations above.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Phase 1 core pipeline is complete: config, scanner abstraction, PDF assembly, paperless client, job model, worker, pipeline, CLI
- All 85 tests passing
- Ready for Phase 2 (ADF/duplex scanning) or Phase 3 (web UI)

## Self-Check: PASSED

All 7 created files verified. All 4 task commits verified.

---
*Phase: 01-core-pipeline*
*Completed: 2026-03-20*
