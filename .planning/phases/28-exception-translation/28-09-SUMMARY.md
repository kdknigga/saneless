---
phase: 28-exception-translation
plan: 09
subsystem: cli
tags: [exceptions, exit-codes, click, logging, sqlite, tdd]
requires:
  - "28-01: ExitCode, exit_code_for, classify_error, describe, ScanCancelledError, PdfError"
  - "28-02: load_settings raises only ConfigError for file-level failures"
provides:
  - "saneless.cli._GuardedGroup: one last-resort handler around every CLI command"
  - "configure_logging(...) -> bool (whether the file handler attached)"
  - "JobStore open-time sqlite3.Error -> StorageError('Could not open the job database at <path>: ...')"
affects:
  - "28-11 (flip-prompt cancel now reaches the guard's ScanCancelledError clause, exit 130)"
  - "28-12 (require_sane / serve exit 2 build on the guard)"
tech-stack:
  added: []
  patterns:
    - "click.Group subclass overriding invoke() as the single CLI exception boundary"
    - "Module-level exception tuple for a multi-type except, so pre-commit's Python 3.12 AST hooks can parse it"
key-files:
  created: []
  modified:
    - src/saneless/cli.py
    - src/saneless/logging_config.py
    - src/saneless/job.py
    - tests/test_cli.py
    - tests/test_logging.py
    - tests/test_job.py
decisions:
  - "StorageError is mapped to exit 2 by type in the guard, before the SanelessError clause; classify_error keeps it UNKNOWN (D-07 amendment)"
  - "The guard logs only once _load_cli_settings has marked ctx.obj['logging_configured']; before that it never logs (Pitfall 3) and -v prints the traceback to stderr instead"
  - "'Full details in <log_file>' is printed only when configure_logging returned True (T-28-35)"
  - "JobStore translates sqlite3.Error from connect, the WAL pragma and the migration ladder; runtime store methods keep raw sqlite3 errors"
  - "Click control-flow exceptions are held in a module tuple _CLICK_CONTROL_FLOW because ruff format rewrites a parenthesised multi-type except to PEP 758 form, which the check-ast hook (Python 3.12) rejects"
metrics:
  duration: "~45 min"
  completed: 2026-09-15
  tasks: 3
  files: 6
requirements: [EXC-01, EXC-02]
---

# Phase 28 Plan 09: Guarded CLI Group and Exit Codes Summary

Every CLI command now runs inside `_GuardedGroup.invoke`, which turns each failure into one stderr message and its exit code:

| Failure | Exit |
|---|---|
| Bad config | 2 |
| Job database saneless cannot use | 2 |
| Broken scanner | 1 |
| Unreachable Paperless | 3 |
| PDF cannot be assembled | 4 |
| Cancel or Ctrl-C | 130 |
| Exception that is not a saneless type | 5, traceback in the log |

click's `--help`, usage errors and `ClickException` behave exactly as before. `configure_logging` now returns whether the file handler attached. `JobStore` reports a database it cannot open as a `StorageError` naming the path.

## Tasks

| Task | Name | RED | GREEN | Files |
|---|---|---|---|---|
| 1 | configure_logging reports whether the log file handler attached | 029fcea | 6629fe4 | src/saneless/logging_config.py, tests/test_logging.py |
| 2 | JobStore reports an unusable job database as StorageError naming the path | b83d488 | 5fdf564 | src/saneless/job.py, tests/test_job.py |
| 3 | The guarded group | 3067754 | dc59c91 | src/saneless/cli.py, tests/test_cli.py |

## What Was Built

### Task 1: logging_config.py
- `configure_logging(...)` now has the return type `-> bool`. It sets a local `attached` flag on both paths and returns it once at the end, so `-v` and the saneless logger level are still applied either way.
- The docstring has a Returns section.
- New tests, selected with `-k attached`:
  - `test_returns_true_when_the_file_handler_attached`
  - `test_returns_false_when_the_file_handler_is_not_attached`. The log path sits under a regular file, so the test also works when run as root. It checks the stderr handler and the existing WARNING.

### Task 2: job.py
- New `_open_connection(db_path)` covers connect, `row_factory`, the WAL pragma and `autocommit = False`. It turns `sqlite3.Error` into `StorageError` via `_open_failure()` and closes the connection if it had been opened.
- `JobStore.__init__` keeps its migrate try/except: rollback and close on any failure. A `sqlite3.Error` from the ladder is translated the same way. The `_migrate_v2` StorageError is not a `sqlite3.Error`, so it passes through unchanged.
- The `__init__` docstring has a Raises section.
- New tests:
  - `test_unusable_non_sqlite_file_is_a_storage_error`: cause is `DatabaseError`, no newline, file bytes untouched.
  - `test_unusable_directory_path_is_a_storage_error`: cause is `sqlite3.Error`.
  - `test_unsupported_schema_error_is_not_rewrapped`: added as a separate test so `test_migration_guard_rejects_an_s2_database` stays unchanged.

### Task 3: cli.py
- **`_GuardedGroup(click.Group)`**, with the clause order explained in its docstring:
  1. click control flow is re-raised.
  2. `KeyboardInterrupt` prints `Cancelled (interrupted)` and exits 130.
  3. `ScanCancelledError` prints its message and exits 130.
  4. `StorageError` prints `Job database error: ...` and exits 2.
  5. Any other `SanelessError` exits with `exit_code_for(classify_error(exc))` and prints the line from `_failure_line`.
  6. `Exception` goes to `_report_unexpected` and exits 5.
- **Helpers:**
  - `_logging_ready`
  - `_unexpected_line`
  - `_failure_line`: a total match with `assert_never`; the documented prefixes are unchanged.
  - `_log_failure`
  - `_report_unexpected`
- **`@click.group(cls=_GuardedGroup)`**
- **`_load_cli_settings`:** the try/except is gone. After `configure_logging` it records `logging_configured` and sets `log_file` only when the handler attached.
- **`scan`:** the `ScanError`/`PaperlessError` handlers are gone; `finally: paperless.close()` stays.
- **`auto-profiles`:** the `ConfigError` branch is gone; the `OSError` message stays.
- Every former `sys.exit(<literal>)` is now `ctx.exit(ExitCode.<member>)`, so `grep "sys.exit("` finds nothing. `import sys` stays because `_stdin_is_interactive` uses it.
- **Tests:**
  - New `TestExitCodes` class with 19 tests, 3 of them `job_database`.
  - `test_scan_config_error` now raises `ConfigError` and expects exit 2.
  - New `test_scan_config_value_error_exits_5` covers the bare `ValueError` case.
  - The `TestLazySettingsLoading` docstring no longer claims an unexpected setup failure exits 2.

## Verification

- `uv run pytest -m "not browser and not sane_hardware" -q`: 1748 passed.
- `uv run ruff check .`, `uv run ruff format --check .` and `uv run ty check` are clean. `uv run pyrefly check src tests` reports 0 errors and the same 4 warnings as before this plan; one new warning (unneeded `str()`) was fixed.
- `uv run prek run --stage pre-push --all-files`: all hooks pass.
- Selections: `-k attached` 2 passed; `-k unusable` 2 passed; `-k TestExitCodes` 19 passed; `-k "TestExitCodes and job_database"` 3 passed.
- Acceptance greps:
  - `sys.exit(` and `"Configuration error: ` do not appear in cli.py.
  - `@click.group(cls=_GuardedGroup)` appears once.
  - `except StorageError as exc:` is on the line before `except SanelessError as exc:`.
  - `except Exception as exc:` appears only in `_GuardedGroup.invoke`; `ClickFlipCoordinator._prompt` keeps its bare `except Exception:`.
  - `Could not open the job database at` appears once in job.py.
  - The Task 2 commits touch only job.py and test_job.py.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] PEP 758 except clause rejected by the check-ast hook**
- **Found during:** Task 3 GREEN commit
- **Issue:** `ruff format` rewrote `except (Exit, Abort, ClickException):` to the bracketless PEP 758 form. The `check-ast` and `debug-statements` hooks parse with CPython 3.12 and failed with "multiple exception types must be parenthesized".
- **Fix:** Moved the three types into a documented module constant, `_CLICK_CONTROL_FLOW`, used as `except _CLICK_CONTROL_FLOW: raise`. Behaviour is the same and nothing is suppressed.
- **Files modified:** src/saneless/cli.py
- **Commit:** dc59c91

**2. [Rule 1 - Bug] pyrefly unnecessary-type-conversion warning**
- `str(settings.output.log_file)` added a pyrefly warning, because `log_file` is already a `str`. Changed to `settings.output.log_file`. Commit dc59c91.

### Notes
- **Separate pass-through test:** The unsupported-schema pass-through check is its own test, not extra asserts in `test_migration_guard_rejects_an_s2_database`, which the plan says must stay unchanged.
- **Log message wording:** Setup failures that the guard logs (`StorageError` and classified `SanelessError`) use the message `saneless <command> failed`. Non-saneless exceptions use `Unexpected error in saneless <command>`, as the plan specifies.

## TDD Gate Compliance

Each task has a `test(28-09)` commit followed by a `feat(28-09)` commit, as listed in the Tasks table. Task 1 and Task 2 RED runs failed as expected: they returned None, and raw sqlite3 errors escaped.

Task 3 RED: 14 of the 22 selected tests failed. The 8 that passed pin behaviour that already existed and had to survive the rewrite:
- bad-config header
- ScanError exit 1
- PaperlessError exit 3
- `--help`
- unknown option
- ClickException
- `test_scan_config_error`
- `test_real_config_error_printed_once_with_its_own_header`

This is intentional regression coverage, not a test that measures nothing.

## Known Stubs

None.

## Threat Flags

None. Everything touched is covered by T-28-32..39 in the plan's threat model.

## Self-Check: PASSED

- FOUND: src/saneless/cli.py (`class _GuardedGroup(click.Group)`, `@click.group(cls=_GuardedGroup)`)
- FOUND: src/saneless/logging_config.py (`-> bool`)
- FOUND: src/saneless/job.py (`Could not open the job database at`)
- FOUND commits: 029fcea, 6629fe4, b83d488, 5fdf564, 3067754, dc59c91
