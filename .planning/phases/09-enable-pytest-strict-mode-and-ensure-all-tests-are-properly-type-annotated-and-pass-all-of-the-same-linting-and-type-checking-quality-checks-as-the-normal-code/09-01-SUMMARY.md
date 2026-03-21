---
phase: 09-enable-pytest-strict-mode
plan: 01
subsystem: testing
tags: [ty, pyrefly, type-checking, test-quality, ABC-stubs]

requires:
  - phase: 08-audit-lint-and-type-checker-ignores
    provides: clean lint baseline with per-file-ignores documented
provides:
  - ty and pyrefly coverage extended to tests/ directory
  - zero type:ignore comments in all test files
  - concrete _FakeSaneDevice stub pattern for mock replacement
  - _app() and _get() type-narrowing helpers for test code
affects: [09-02, 09-03]

tech-stack:
  added: []
  patterns:
    - "_FakeSaneDevice with pluggable callables for protocol-compliant test stubs"
    - "_app(client) helper for TestClient -> FastAPI type narrowing"
    - "_get(store, id) helper for Job|None narrowing in test assertions"

key-files:
  created: []
  modified:
    - tests/test_scanner.py
    - tests/test_browser.py
    - tests/test_job.py
    - tests/test_logging.py
    - tests/test_web.py
    - tests/test_worker.py
    - pyproject.toml

key-decisions:
  - "ARG rule ignored for tests via per-file-ignores (ABC stubs have legitimately unused params)"
  - "_FakeSaneDevice delegates to stored callables via real methods to satisfy SaneDevice protocol"
  - "cls: type = ScannerBackend indirection to test ABC instantiation without type:ignore"
  - "monkeypatch.setattr for method replacement instead of direct assignment to avoid invalid-assignment"

patterns-established:
  - "Type-narrowing helpers (_get, _app) centralize None checks for test readability"
  - "Protocol-compliant fake classes over mock reassignment for type safety"

requirements-completed: [TQUAL-01, TQUAL-02]

duration: 8min
completed: 2026-03-21
---

# Phase 09 Plan 01: Fix Type Checker Errors and Remove Test Exclusions Summary

**ty and pyrefly now cover all test files with zero type:ignore comments, using concrete stub classes and type-narrowing helpers**

## Performance

- **Duration:** 8 min
- **Started:** 2026-03-21T20:49:20Z
- **Completed:** 2026-03-21T20:57:00Z
- **Tasks:** 2
- **Files modified:** 7

## Accomplishments

- Removed ty and pyrefly exclusions for tests/ -- both type checkers now cover entire codebase
- Eliminated all 6 type:ignore comments in test_scanner.py via _FakeSaneDevice concrete stub
- Fixed 38 type checker errors across test_scanner.py, test_browser.py, test_job.py, test_logging.py, test_web.py, test_worker.py
- All 226 tests continue to pass

## Task Commits

Each task was committed atomically:

1. **Task 1: Fix type checker errors in test_browser.py, test_job.py, test_logging.py** - `c72bee5` (fix)
2. **Task 2: Replace mock patterns in test_scanner.py with concrete stubs, remove ty/pyrefly exclusions** - `3a2d7af` (feat)

## Files Created/Modified

- `tests/test_scanner.py` - Added _FakeSaneDevice class, removed all type:ignore, fixed ABC param names
- `tests/test_browser.py` - Fixed ABC override parameter names (device_id, settings)
- `tests/test_job.py` - Added None-narrowing assertions before .error_category access
- `tests/test_logging.py` - Added explicit import logging.handlers
- `tests/test_web.py` - Fixed StubScanner param names, added _app() helper for type narrowing
- `tests/test_worker.py` - Added _get() helper, monkeypatch.setattr for method replacement, None narrowing
- `pyproject.toml` - Removed ty/pyrefly test exclusions, added ARG ignore for tests

## Decisions Made

- Used per-file ARG ignore for tests rather than individual noqa comments (ABC stubs legitimately have unused params)
- Created _FakeSaneDevice with real methods delegating to stored callables (not callable attributes) to satisfy SaneDevice Protocol
- Used `cls: type = ScannerBackend` indirection to test ABC without type:ignore[abstract]
- Used monkeypatch.setattr instead of direct method assignment to avoid ty invalid-assignment errors

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Fixed 32 additional type errors in test_web.py, test_worker.py**
- **Found during:** Task 2 (after removing type checker exclusions)
- **Issue:** Removing ty/pyrefly exclusions exposed errors in test_web.py (client.app.state access, StubScanner param names) and test_worker.py (Job|None narrowing, method reassignment)
- **Fix:** Added _app() helper for TestClient type narrowing, _get() helper for Job|None narrowing, monkeypatch.setattr for method replacement, fixed StubScanner param names
- **Files modified:** tests/test_web.py, tests/test_worker.py
- **Verification:** uv run ty check and uv run pyrefly check both pass clean
- **Committed in:** 3a2d7af (Task 2 commit)

**2. [Rule 3 - Blocking] Added ARG per-file-ignore for test files**
- **Found during:** Task 1 (pre-commit hook failure)
- **Issue:** Ruff ARG002 flagged unused params in ABC override stubs (required to match parent signatures)
- **Fix:** Added "ARG" to per-file-ignores for tests/**/*.py
- **Files modified:** pyproject.toml
- **Committed in:** c72bee5 (Task 1 commit)

---

**Total deviations:** 2 auto-fixed (2 blocking)
**Impact on plan:** Both fixes were necessary consequences of enabling type checkers on tests. No scope creep.

## Issues Encountered

None beyond the deviation-tracked items.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- Type checkers now cover tests/ -- ready for Phase 09 Plan 02 (annotations) and Plan 03 (docstrings/linting)
- ARG ignore established for test ABC stubs
- Patterns for type-safe test code established (_get, _app helpers)

---
*Phase: 09-enable-pytest-strict-mode*
*Completed: 2026-03-21*
