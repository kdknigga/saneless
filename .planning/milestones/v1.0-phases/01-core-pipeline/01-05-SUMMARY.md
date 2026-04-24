---
phase: 01-core-pipeline
plan: 05
subsystem: config
tags: [pydantic, toml, validation, field-alias, error-handling]

requires:
  - phase: 01-core-pipeline
    provides: config loading with pydantic-settings and TOML
provides:
  - User-friendly TOML structure error messages with section guidance
  - "title" field alias for default_title_template in ProfileConfig
affects: [config, cli, web-ui]

tech-stack:
  added: []
  patterns: [pydantic-field-alias, validation-error-interception]

key-files:
  created: []
  modified:
    - src/saneless/config.py
    - tests/test_config.py

key-decisions:
  - "Field(alias='title') with populate_by_name=True for TOML and code access"
  - "Intercept ValidationError extra_forbidden to provide ConfigError with section hints"
  - "Keep extra=forbid on Settings -- catch and re-raise with user guidance instead of relaxing validation"

patterns-established:
  - "Validation error interception: catch pydantic ValidationError, inspect error types, raise domain-specific ConfigError"

requirements-completed: [CONF-01, CONF-02, CONF-03]

duration: 3min
completed: 2026-03-21
---

# Phase 01 Plan 05: TOML Error UX and Title Alias Summary

**User-friendly TOML validation errors with section guidance and 'title' shorthand alias for default_title_template**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-21T15:01:55Z
- **Completed:** 2026-03-21T15:04:28Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- TOML `[default]` section now raises ConfigError with "Did you mean [profiles.default]?" guidance
- Unknown top-level sections produce helpful error listing valid sections
- `title` accepted as alias for `default_title_template` in profile config
- Env var `SANELESS_PROFILES__DEFAULT__TITLE` correctly sets default_title_template
- All 225 tests pass, ruff clean, pyrefly clean

## Task Commits

Each task was committed atomically:

1. **Task 1 (RED): Failing tests for TOML error UX** - `feb98e4` (test)
2. **Task 1 (GREEN): Title alias and error handling** - `9e1e6e6` (feat)
3. **Task 2: Type checker and full suite verification** - no code changes needed (all passed)

_TDD task had RED and GREEN commits._

## Files Created/Modified
- `src/saneless/config.py` - Added title alias on ProfileConfig, _build_settings helper with ValidationError interception
- `tests/test_config.py` - Added TestTomlStructureErrors class with 4 tests

## Decisions Made
- Used `Field(alias="title")` with `ConfigDict(populate_by_name=True)` so both `title` and `default_title_template` work in TOML and Python code
- Kept `extra="forbid"` on Settings (pydantic-settings default) and intercept the error rather than relaxing validation -- typos still caught, but the error message guides users
- Extracted `_build_settings` helper to encapsulate the try/except pattern, keeping `load_settings` clean

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
- Pre-existing ty warning in `sane_backend.py` (unused type: ignore comment) -- logged to deferred-items.md, not in scope for this plan

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- All UAT gaps from phase 01 are now closed
- Config loading provides clear guidance for common TOML mistakes
- Phase 01 fully complete

---
*Phase: 01-core-pipeline*
*Completed: 2026-03-21*
