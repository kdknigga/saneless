---
phase: 28-exception-translation
plan: 01
subsystem: vocabulary
tags: [exceptions, exit-codes, job-state, templates, tdd]
requires: []
provides:
  - "saneless.exceptions.PdfError"
  - "saneless.exceptions.ScanCancelledError"
  - "saneless.exceptions.describe"
  - "saneless.vocabulary.JobState.CANCELLED"
  - "saneless.vocabulary.ErrorCategory.ASSEMBLY"
  - "saneless.vocabulary.ExitCode"
  - "saneless.vocabulary.exit_code_for"
affects: [28-02, 28-03, 28-04, 28-05, 28-06, 28-07, 28-08, 28-09]
tech-stack:
  added: []
  patterns:
    - "IntEnum for exit codes, dispatched by total match + assert_never (rejection_status_code shape)"
    - "classify_error converted to assign-once, return-once elif chain"
key-files:
  created:
    - tests/test_exceptions.py
  modified:
    - src/saneless/exceptions.py
    - src/saneless/vocabulary.py
    - src/saneless/web/templates/partials/status.html
    - src/saneless/web/templates/partials/history.html
    - tests/test_vocabulary.py
    - tests/test_web_state_rendering.py
decisions:
  - "ScanCancelledError and PdfError are direct SanelessError subclasses, never ScanError (D-01, D-04)"
  - "classify_error leaves ScanCancelledError and StorageError UNKNOWN; StorageError's exit 2 is assigned by type in the CLI guard (D-07 amendment)"
  - "ExitCode is the single exit-code definition: SUCCESS 0, SCAN 1, CONFIG 2, PAPERLESS 3, PDF 4, UNEXPECTED 5, CANCELLED 130 (D-07)"
  - "JobState.CANCELLED is terminal and renders without role=alert (D-01)"
metrics:
  duration: 6min
  completed: 2026-09-15
  tasks: 2
  files: 7
requirements: [EXC-01, EXC-02, EXC-04]
---

# Phase 28 Plan 01: Shared Exception and Exit-Code Vocabulary Summary

This plan adds the names the rest of Phase 28 builds on. `PdfError` and `ScanCancelledError` both subclass `SanelessError` directly, and neither is a `ScanError`. `describe()` returns an exception's text, or its class name when the text is empty. `ErrorCategory` gains `ASSEMBLY`. `ExitCode` (an IntEnum) plus a total `exit_code_for` match is now the only place exit codes are defined. `JobState.CANCELLED` is a new terminal state, and the status and history partials show it without an alert.

## What Was Built

### Task 1: exception types, describe(), ASSEMBLY, ExitCode
- **`src/saneless/exceptions.py`**
  - `PdfError` and `ScanCancelledError` have docstrings only, no custom constructors. `_preserving` rebuilds exceptions as `type(exc)(msg)`, so both take a single message.
  - `describe(exc: BaseException) -> str` returns `str(exc) or type(exc).__name__`.
  - `StorageError`'s docstring now says three things: the job database path is in every message, it counts as a setup problem, and it exits with 2.
  - `__all__` stays sorted.
- **`src/saneless/vocabulary.py`**
  - Imports `IntEnum` and `PdfError`.
  - Adds `ErrorCategory.ASSEMBLY` with a docstring paragraph and an `error_message` arm: "The scanned pages could not be assembled into a PDF."
  - `classify_error` is now an elif chain that assigns once and returns once, with a new `PdfError -> ASSEMBLY` branch. This avoids PLR0911 (too many return statements). The docstring explains why `ScanCancelledError` and `StorageError` stay `UNKNOWN`.
  - Adds `class ExitCode(IntEnum)` and `exit_code_for(category)`. Its docstring covers:
    - a cancel is resolved by type and maps to `CANCELLED`;
    - `StorageError` is mapped to `CONFIG` by type, so `classify_error` does not reclassify it;
    - a `STORAGE` category was considered and rejected;
    - `UNKNOWN -> UNEXPECTED` is reached only by exceptions that are not saneless types.
- **Tests**
  - New `tests/test_exceptions.py`: hierarchy, single-message rebuild, and `describe()` with `ValueError("boom")`, `httpx.ReadTimeout("")` and `RuntimeError()`.
  - `tests/test_vocabulary.py`: the category name set now includes ASSEMBLY, plus the ASSEMBLY message and classify pins for PdfError, ScanCancelledError and StorageError (`test_classify_error_storage_error_is_unknown`).
  - New `TestExitCode`: membership, table coverage, a parametrised mapping, totality, and the unrecognised value raising AssertionError. `-k exit_code` selects 17 tests.

### Task 2: JobState.CANCELLED
- **`vocabulary.py`**
  - Adds `CANCELLED = "CANCELLED"` after FALLBACK and to `TERMINAL_STATES`.
  - `state_label` and `progress_label` both return "Cancelled", and the `progress_label` docstring now names four terminal states.
  - No other `match` on JobState exists: ty and pyrefly both pass clean.
- **`status.html`**: a new `JobState.CANCELLED` branch renders `<p class="status-cancelled">&#8856; Cancelled: {{ job.title }}</p>` plus the hidden history-refresh div. There is no `role="alert"`; the grep count is still 1. `job.title` is autoescaped (T-28-01).
- **`history.html`**: the cell class chain gains `status-cancelled`.
- **Tests**
  - The member-count guard is now ten, and CANCELLED is in the terminal set.
  - New `test_cancelled_is_not_busy`.
  - CANCELLED rows added to the state_label table, the terminal progress prose and the `Job.is_active/is_busy` table.
  - The rendering tests assert the `status-cancelled` history cell, the exact status line, and that there is no alert.

## Schema / Rollback Note (T-28-04)

In `src/saneless/job.py`, `CREATE TABLE jobs` declares `state TEXT NOT NULL` and `error_category TEXT`, and neither column has a CHECK constraint. `grep CHECK` finds only `TYPE_CHECKING`, so no migration was written.

**Rollback caveat for the changelog:** an older build that reads a row with `state = 'CANCELLED'` or `error_category = 'ASSEMBLY'` will raise `ValueError` in `JobState(...)` or `ErrorCategory(...)`.

## Verification

- `uv run pytest tests/test_exceptions.py tests/test_vocabulary.py tests/test_web_state_rendering.py tests/test_job.py tests/test_web.py -q`: all pass.
- `uv run pytest -m "not browser and not sane_hardware" -q`: 1709 passed.
- Other test selections:
  - `-k CANCELLED` in `test_web_state_rendering.py`: 8 passed
  - `-k exit_code`: 17 passed
  - `-k "classify and storage"`: 1 passed
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check` and `uv run pyrefly check src tests` all report 0 errors.
- Git log shows each `test(28-01):` commit before its `feat(28-01):` commit, for both tasks.

## Deviations from Plan

None; the plan was executed as written. Small notes:
- **Extra tests beyond the plan's list:** `test_cancelled_is_not_busy`, and CANCELLED added to the `Job.is_active/is_busy` table. Both cover things the plan's behaviour list already required.
- **Template comment rewording:** the Jinja comment on the new CANCELLED branch avoids the literal `role="alert"` string. Otherwise the acceptance check that `grep -c 'role="alert"'` stays at 1 would fail.
- **ExitCode docstring:** it also explains why CANCELLED is 130 (the shell's SIGINT convention).

## TDD Gate Compliance

| Task | RED commit | GREEN commit |
|------|------------|--------------|
| 1 | 01a875c | 8ef8959 |
| 2 | d033df4 | 2f62bad |

Both RED runs failed before any implementation existed: ImportError for Task 1, AttributeError on `JobState.CANCELLED` for Task 2. No refactor commits were needed.

## Known Stubs

- `.status-cancelled` has no CSS rule yet; plan 28-08 adds it. Until then it uses the body text colour and is never red.
- Nothing produces a CANCELLED job or a PdfError yet; wave 2 plans do that wiring.
- `exit_code_for` has no production caller until the CLI guard lands in plan 28-09.

## Self-Check: PASSED

- FOUND: tests/test_exceptions.py
- FOUND: src/saneless/exceptions.py (PdfError, ScanCancelledError, describe)
- FOUND: src/saneless/vocabulary.py (ExitCode, exit_code_for, CANCELLED, ASSEMBLY)
- FOUND commits: 01a875c, 8ef8959, d033df4, 2f62bad
