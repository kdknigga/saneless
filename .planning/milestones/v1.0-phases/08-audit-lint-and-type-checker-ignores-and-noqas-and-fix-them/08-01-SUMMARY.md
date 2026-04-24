---
phase: 08-audit-lint-and-type-checker-ignores-and-noqas-and-fix-them
plan: 01
subsystem: code-quality
tags: [ruff, ty, pyrefly, httpx, type-checking, noqa, lint]

# Dependency graph
requires:
  - phase: 07-tech-debt-cleanup
    provides: Clean codebase with all linters passing
provides:
  - Zero unjustified suppressions in production code
  - Tightened per-file-ignores for test files
  - Proper httpx multipart typing without type: ignore
affects: [09-enable-pytest-strict-mode]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "TYPE_CHECKING-guarded imports for httpx internal types"
    - "Inline justification comments on all remaining noqa/type: ignore"

key-files:
  created: []
  modified:
    - src/saneless/paperless.py
    - src/saneless/config.py
    - pyproject.toml
    - tests/test_browser.py
    - tests/test_scanner.py
    - tests/test_web.py

key-decisions:
  - "httpx._types.FileTypes via TYPE_CHECKING guard for multipart upload typing"
  - "PLC0415 retained for tests (77 lazy imports, test-inherent pattern)"
  - "TCH, T201, PLR0915, PLR0912, PLR0913 removed from per-file-ignores (0 violations each)"

patterns-established:
  - "Every noqa/type: ignore in src/ must have inline justification comment"
  - "Per-file-ignores audited with violation counts before adding/keeping rules"

requirements-completed: [AUDIT-01, AUDIT-02, AUDIT-03, AUDIT-04]

# Metrics
duration: 3min
completed: 2026-03-21
---

# Phase 08 Plan 01: Audit Lint and Type Checker Ignores Summary

**Replaced httpx type: ignore with proper FileTypes annotation, documented all remaining suppressions, and tightened test per-file-ignores from 10 rules to 6**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-21T19:48:49Z
- **Completed:** 2026-03-21T19:52:10Z
- **Tasks:** 2
- **Files modified:** 6

## Accomplishments

- Removed `type: ignore[arg-type]` from httpx multipart upload by using `FileTypes` from `httpx._types` under TYPE_CHECKING guard
- Added inline justification comments to all 6 remaining suppressions in src/ (2x ARG003, 1x call-arg, 2x PLC0415, 1x PLW0603)
- Removed 4 unnecessary rules from test per-file-ignores (TCH, T201, PLR0915, PLR0912, PLR0913 all had 0 violations)
- Fixed 3 TC003 violations in test files by moving Iterator imports into TYPE_CHECKING blocks
- All 226 tests pass, all linters and type checkers clean

## Task Commits

Each task was committed atomically:

1. **Task 1: Fix httpx type: ignore and document production suppressions** - `fb3beda` (fix)
2. **Task 2: Audit and tighten per-file-ignores** - `d7d6c88` (chore)

## Files Created/Modified

- `src/saneless/paperless.py` - Replaced `list[tuple[str, object]]` with `list[tuple[str, FileTypes]]`, removed type: ignore
- `src/saneless/config.py` - Added justification comments to ARG003 noqas and type: ignore[call-arg]
- `pyproject.toml` - Tightened per-file-ignores from 10 rules to 6, added inline TOML comments
- `tests/test_browser.py` - Moved Iterator import into TYPE_CHECKING block
- `tests/test_scanner.py` - Added TYPE_CHECKING guard for Iterator import
- `tests/test_web.py` - Moved Iterator import into TYPE_CHECKING block

## Decisions Made

- Used `httpx._types.FileTypes` under TYPE_CHECKING guard (private module but stable internal type, avoids runtime import)
- Kept PLC0415 for tests: 77 violations from test-inherent lazy import pattern (monkeypatching, isolation)
- Removed TCH from per-file-ignores after fixing 3 violations (moved Iterator to TYPE_CHECKING blocks)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] TCH violations surfaced after removing TCH from per-file-ignores**
- **Found during:** Task 2 (per-file-ignores audit)
- **Issue:** Removing TCH from per-file-ignores revealed 3 TC003 violations for Iterator imports
- **Fix:** Moved Iterator imports into TYPE_CHECKING blocks in 3 test files
- **Files modified:** tests/test_browser.py, tests/test_scanner.py, tests/test_web.py
- **Verification:** `uv run ruff check .` exits 0
- **Committed in:** d7d6c88 (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** Expected follow-on from tightening per-file-ignores. No scope creep.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required.

## Remaining Suppressions Inventory

All 6 remaining suppressions in src/ with justifications:

| File | Suppression | Justification |
|------|-------------|---------------|
| scanner/__init__.py:23 | `# noqa: PLC0415` | Lazy import to avoid python-sane at import time |
| scanner/sane_backend.py:45 | `# noqa: PLW0603` | Module-level sane global for monkeypatching |
| scanner/sane_backend.py:47 | `# noqa: PLC0415` | Deferred C extension import |
| config.py:109 | `# noqa: ARG003` | pydantic-settings requires this signature param |
| config.py:110 | `# noqa: ARG003` | pydantic-settings requires this signature param |
| config.py:153 | `# type: ignore[call-arg]` | ty cannot see BaseSettings dynamic __init__ kwargs |

## Next Phase Readiness

- All production code has zero unjustified suppressions
- Per-file-ignores are tightened and documented
- Ready for Phase 09 (pytest strict mode and test quality checks)

---
*Phase: 08-audit-lint-and-type-checker-ignores-and-noqas-and-fix-them*
*Completed: 2026-03-21*
