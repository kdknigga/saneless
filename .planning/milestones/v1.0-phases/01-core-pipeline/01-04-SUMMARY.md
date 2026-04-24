---
phase: 01-core-pipeline
plan: 04
subsystem: logging
tags: [xdg, logging, permissionerror, graceful-degradation]

requires:
  - phase: 01-core-pipeline
    provides: config.py OutputConfig model, logging_config.py configure_logging
provides:
  - XDG-compliant default log path (~/.local/state/saneless)
  - Graceful mkdir error handling with stderr fallback
affects: [cli, web-server, scanning-pipeline]

tech-stack:
  added: []
  patterns: [OSError catch for filesystem fallback, XDG Base Directory compliance]

key-files:
  created: []
  modified: [src/saneless/config.py, src/saneless/logging_config.py, tests/test_logging.py]

key-decisions:
  - "Catch OSError alone instead of (PermissionError, OSError) since PermissionError is a subclass of OSError"
  - "Use Path.home() / .local/state/saneless for XDG state directory compliance"

patterns-established:
  - "Filesystem fallback: try file-based operation, except OSError fall back to stderr"

requirements-completed: [LOG-01, LOG-02]

duration: 2min
completed: 2026-03-21
---

# Phase 01 Plan 04: Log Path Fix Summary

**XDG-compliant log path default with graceful OSError fallback to stderr-only logging**

## Performance

- **Duration:** 2 min
- **Started:** 2026-03-21T14:03:04Z
- **Completed:** 2026-03-21T14:05:02Z
- **Tasks:** 1 (TDD: RED + GREEN)
- **Files modified:** 3

## Accomplishments
- Changed log_file default from /var/log/saneless/saneless.log to ~/.local/state/saneless/saneless.log
- Wrapped mkdir + RotatingFileHandler creation in try/except OSError with stderr fallback
- Added two new tests: unwritable directory fallback and XDG path default validation

## Task Commits

Each task was committed atomically:

1. **Task 1 RED: Failing tests** - `6b4b0e0` (test)
2. **Task 1 GREEN: Implementation** - `69c775b` (feat)

## Files Created/Modified
- `src/saneless/config.py` - Changed log_file default to XDG state directory
- `src/saneless/logging_config.py` - Added try/except OSError around mkdir + file handler, stderr fallback
- `tests/test_logging.py` - Added test_unwritable_directory_falls_back_to_stderr and test_default_log_file_is_xdg_compliant

## Decisions Made
- Catch OSError alone instead of (PermissionError, OSError) tuple -- PermissionError is a subclass of OSError, and ruff format was incorrectly removing parentheses from the except tuple
- Use Path.home() / ".local/state/saneless" for XDG Base Directory Specification compliance (state data, not cache or config)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Ruff format removes except clause parentheses**
- **Found during:** Task 1 GREEN phase
- **Issue:** `except (PermissionError, OSError):` was reformatted to invalid `except PermissionError, OSError:` by ruff format
- **Fix:** Changed to `except OSError:` since PermissionError is a subclass of OSError
- **Files modified:** src/saneless/logging_config.py
- **Verification:** ruff format --check passes, tests pass
- **Committed in:** 69c775b

---

**Total deviations:** 1 auto-fixed (1 bug)
**Impact on plan:** Cleaner code -- catching OSError alone is more idiomatic.

## Issues Encountered
None beyond the ruff format deviation noted above.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- CLI commands now work without root privileges
- Logging gracefully degrades when directory is not writable
- All gap closure items for log path PermissionError are resolved

---
*Phase: 01-core-pipeline*
*Completed: 2026-03-21*
