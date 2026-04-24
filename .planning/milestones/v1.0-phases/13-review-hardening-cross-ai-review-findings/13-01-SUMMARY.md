---
phase: 13-review-hardening-cross-ai-review-findings
plan: 01
subsystem: config, web, pipeline
tags: [pydantic, fastapi, security, validation, docker]

requires:
  - phase: 01-core-pipeline
    provides: config.py, pipeline.py, routes.py foundations
provides:
  - enable_empty_page_detection toggle on ProfileConfig
  - min_free_space_mb field on OutputConfig
  - validate_settings_dirs() startup writability check
  - sanitized 502 error responses on paperless test endpoint
  - docker-compose.yml config.toml existence warning
affects: [13-02-PLAN, pipeline, config]

tech-stack:
  added: []
  patterns:
    - standalone validation function for cross-cutting config checks (not Pydantic validator)

key-files:
  created: []
  modified:
    - src/saneless/config.py
    - src/saneless/pipeline.py
    - src/saneless/web/routes.py
    - src/saneless/web/app.py
    - src/saneless/cli.py
    - tests/test_config.py
    - tests/test_pipeline.py
    - tests/test_web.py
    - tests/test_browser.py
    - docker-compose.yml

key-decisions:
  - "Standalone validate_settings_dirs() instead of Pydantic model_validator -- ConfigError required per D-13, not ValidationError"
  - "RH-08 not applicable -- current uvicorn already skips signal handlers in non-main threads"

patterns-established:
  - "Standalone config validation functions for checks that must raise domain-specific errors"

requirements-completed: [RH-04, RH-05, RH-07, RH-08, RH-09]

duration: 4min
completed: 2026-03-22
---

# Phase 13 Plan 01: Independent Hardening Fixes Summary

**Sanitized 502 errors, config writability validation with ConfigError, empty page detection toggle, min_free_space_mb config, Docker Compose docs**

## Performance

- **Duration:** 4 min
- **Started:** 2026-03-22T14:34:36Z
- **Completed:** 2026-03-22T14:39:29Z
- **Tasks:** 2
- **Files modified:** 10

## Accomplishments
- Paperless test 502 response now returns exception class name only, never raw messages with tokens/IPs
- Config writability validation fails fast at startup with ConfigError if tmp_dir or consume_dir not writable
- ProfileConfig has enable_empty_page_detection toggle (default True), pipeline gates filter accordingly
- OutputConfig has min_free_space_mb (default 500) for future disk space pre-flight
- docker-compose.yml warns that config.toml must exist on host before first run

## Task Commits

Each task was committed atomically:

1. **Task 1: Config hardening** - `5f50abf` (feat)
2. **Task 2: Exception sanitization, signal handler, Docker docs** - `77aea73` (feat)

## Files Created/Modified
- `src/saneless/config.py` - Added enable_empty_page_detection, min_free_space_mb, validate_settings_dirs()
- `src/saneless/pipeline.py` - Gated empty page filter on profile.enable_empty_page_detection
- `src/saneless/web/routes.py` - Sanitized 502 to return type(exc).__name__
- `src/saneless/web/app.py` - Call validate_settings_dirs in lifespan
- `src/saneless/cli.py` - Call validate_settings_dirs after load_settings
- `tests/test_config.py` - Tests for new fields and writability validation
- `tests/test_pipeline.py` - Test for empty page detection toggle
- `tests/test_web.py` - Test for sanitized 502 response
- `tests/test_browser.py` - Comment documenting uvicorn thread-safe signal handling
- `docker-compose.yml` - Config.toml existence warning comments

## Decisions Made
- Used standalone `validate_settings_dirs()` function instead of Pydantic `model_validator` because D-13 requires `ConfigError`, not `ValidationError`
- RH-08 (uvicorn signal handler fix) is already resolved by current uvicorn version which skips signal handlers when running in non-main threads; added explanatory comment instead of deprecated parameter

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Updated existing test_paperless_test_error assertion**
- **Found during:** Task 2 (Exception sanitization)
- **Issue:** Existing test asserted `"boom" in data["detail"]` which expected raw exception message
- **Fix:** Updated assertion to `data["detail"] == "RuntimeError"` to match sanitized response
- **Files modified:** tests/test_web.py
- **Verification:** All 29 web tests pass
- **Committed in:** 77aea73 (Task 2 commit)

**2. [Rule 1 - Bug] RH-08 install_signal_handlers not available in current uvicorn**
- **Found during:** Task 2 (Signal handler fix)
- **Issue:** `install_signal_handlers` parameter does not exist in installed uvicorn version; uvicorn already handles non-main threads safely via `capture_signals()` thread check
- **Fix:** Added explanatory comment instead of non-existent parameter
- **Files modified:** tests/test_browser.py
- **Verification:** ty and pyrefly type checkers pass clean
- **Committed in:** 77aea73 (Task 2 commit)

---

**Total deviations:** 2 auto-fixed (2 bugs)
**Impact on plan:** Both fixes necessary for correctness. RH-08's intent is already satisfied by uvicorn's built-in behavior. No scope creep.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Config hardening fields (min_free_space_mb, enable_empty_page_detection) ready for plan 02 pipeline changes
- validate_settings_dirs() wired into both CLI and web startup paths

---
*Phase: 13-review-hardening-cross-ai-review-findings*
*Completed: 2026-03-22*
