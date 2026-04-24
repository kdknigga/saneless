---
phase: 01-core-pipeline
plan: 01
subsystem: config
tags: [pydantic-settings, toml, logging, exceptions]

requires: []
provides:
  - "Settings model with TOML + env var loading (SANELESS_ prefix)"
  - "Exception hierarchy: SanelessError -> ConfigError, ScanError, PaperlessError"
  - "configure_logging() with RotatingFileHandler"
  - "Shared test fixtures in conftest.py"
affects: [01-02, 01-03, 02-web-ui]

tech-stack:
  added: [pydantic-settings, click, httpx, img2pdf, Pillow]
  patterns: [pydantic-settings-custom-sources, toml-config-with-env-override, rotating-file-logging]

key-files:
  created:
    - src/saneless/config.py
    - src/saneless/exceptions.py
    - src/saneless/logging_config.py
    - tests/conftest.py
    - tests/test_config.py
    - tests/test_logging.py
  modified:
    - pyproject.toml
    - src/saneless/__init__.py

key-decisions:
  - "Used settings_customise_sources hook for runtime TOML file path instead of static toml_file in model_config"
  - "Deferred python-sane installation (requires libsane-dev system headers not available in build environment)"
  - "Fixed per-file-ignores typo: scanless -> saneless"

patterns-established:
  - "Pattern: pydantic-settings with custom TomlConfigSettingsSource via settings_customise_sources classmethod"
  - "Pattern: env var override with SANELESS_ prefix and __ nested delimiter"
  - "Pattern: Google-style docstrings with D213 (summary on second line) per ruff config"

requirements-completed: [CONF-01, CONF-02, CONF-03, PROF-01, PROF-02, LOG-01, LOG-02]

duration: 5min
completed: 2026-03-20
---

# Phase 01 Plan 01: Config, Exceptions, and Logging Summary

**Pydantic-settings config with TOML file search + env var overrides, exception hierarchy, and RotatingFileHandler logging**

## Performance

- **Duration:** 5 min
- **Started:** 2026-03-20T16:45:48Z
- **Completed:** 2026-03-20T16:51:20Z
- **Tasks:** 2
- **Files modified:** 8

## Accomplishments
- Settings loads from TOML files with fallback search path and SANELESS_ env var overrides
- Default profile validation ensures a "default" profile is always present
- Exception hierarchy established for all error types (ConfigError, ScanError, PaperlessError)
- Logging setup with RotatingFileHandler, configurable level, and optional verbose stderr output
- 20 passing tests covering config loading, validation, env overrides, logging

## Task Commits

Each task was committed atomically:

1. **Task 1: Install dependencies and create exception hierarchy + config module** - `90b6827` (feat)
2. **Task 2: Create logging configuration module with tests** - `f4556b3` (feat)

_Note: TDD tasks combined RED+GREEN into single commits after tests passed_

## Files Created/Modified
- `src/saneless/config.py` - Pydantic Settings with TOML + env var loading, load_settings() search function
- `src/saneless/exceptions.py` - SanelessError base class with ConfigError, ScanError, PaperlessError
- `src/saneless/logging_config.py` - configure_logging() with RotatingFileHandler and verbose stderr
- `tests/conftest.py` - Shared fixtures: tmp_config_dir, sample_toml, clean_env (autouse)
- `tests/test_config.py` - 14 tests for config loading, env overrides, validation, defaults
- `tests/test_logging.py` - 6 tests for file handler, levels, rotation, format, verbose mode
- `pyproject.toml` - Added 5 runtime dependencies, fixed per-file-ignores path typo
- `src/saneless/__init__.py` - Re-exports Settings for convenience

## Decisions Made
- Used `settings_customise_sources` hook to inject TOML file at runtime via `_toml_file` init kwarg, because pydantic-settings 2.13.1 does not auto-configure TomlConfigSettingsSource from model_config
- Deferred python-sane dependency: requires libsane-dev system headers (sane/sane.h) which are not available in current build environment; will be added when scanner module is implemented
- Fixed existing typo in pyproject.toml per-file-ignores (scanless -> saneless)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] python-sane build failure due to missing system headers**
- **Found during:** Task 1 (dependency installation)
- **Issue:** python-sane 2.9.2 requires libsane-dev (sane/sane.h) which is not installed and cannot be installed without sudo
- **Fix:** Removed python-sane from dependencies for now; it is not needed until Phase 1 Plan 2 (scanner abstraction)
- **Files modified:** pyproject.toml
- **Verification:** All other dependencies install and sync correctly
- **Committed in:** 90b6827 (Task 1 commit)

**2. [Rule 1 - Bug] Fixed per-file-ignores path typo**
- **Found during:** Task 1 (reading pyproject.toml)
- **Issue:** per-file-ignores referenced `src/scanless/` instead of `src/saneless/`
- **Fix:** Corrected path and removed reference to nonexistent `app.py`
- **Files modified:** pyproject.toml
- **Committed in:** 90b6827 (Task 1 commit)

---

**Total deviations:** 2 auto-fixed (1 blocking, 1 bug)
**Impact on plan:** python-sane deferred to scanner plan where it is actually needed. Typo fix necessary for correct linting. No scope creep.

## Issues Encountered
- pydantic-settings 2.13.1 does not auto-configure TomlConfigSettingsSource from `toml_file` in `model_config`; required custom `settings_customise_sources` hook instead of the simpler `_toml_file` constructor kwarg pattern from research docs

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Config foundation complete: all subsequent plans can import Settings, load_settings, and exception classes
- Logging is ready for use in all modules via configure_logging()
- python-sane needs libsane-dev installed before scanner module work begins

---
*Phase: 01-core-pipeline*
*Completed: 2026-03-20*
