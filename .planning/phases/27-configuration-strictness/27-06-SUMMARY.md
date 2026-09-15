---
phase: 27-configuration-strictness
plan: 06
subsystem: cli
tags: [click, logging, config, cli, tdd]
requires:
  - phase: 27-01
    provides: resolve_job_title, validated LogLevel
  - phase: 27-03
    provides: ConfigError text with its own "Configuration error in <file>:" header, log_config_sources
  - phase: 27-04
    provides: ProfileWriteResult / _echo_write_result in cli.py
provides:
  - "_load_cli_settings(ctx): lazy, memoised settings load + validate + configure_logging under one error handler"
  - "subcommand --help works with a broken or missing config and creates no log directory"
  - "-v sets only the saneless logger hierarchy to DEBUG; root, httpx and uvicorn keep log_level"
  - "CFG-11 startup record emitted once from the CLI after logging is configured"
  - "scan --title optional through resolve_job_title"
affects: [27-08 docs (cli-commands synopsis, -v wording)]
tech-stack:
  added: []
  patterns:
    - "Group callback only records options; each command calls _load_cli_settings(ctx) on first need"
    - "Tests restore the logging tree through a _restored_logging() context manager"
key-files:
  created: []
  modified:
    - src/saneless/logging_config.py
    - src/saneless/cli.py
    - src/saneless/auto_profiles.py
    - tests/test_logging.py
    - tests/test_cli.py
decisions:
  - "ConfigError is echoed verbatim; any other exception (TOML syntax ValueError, configure_logging OSError) keeps the 'Configuration error: ' prefix and exit 2"
  - "uvicorn log_level follows the configured log_level, never -v"
  - "Non-verbose configure_logging resets the saneless logger to NOTSET so repeated calls do not leak DEBUG"
metrics:
  duration: ~35m
  completed: 2026-09-15
  tasks: 3
  files: 5
---

# Phase 27 Plan 06: Lazy CLI settings, -v DEBUG scope, optional scan title Summary

The CLI group callback no longer loads anything: every command loads, validates
and configures logging once through a memoised `_load_cli_settings(ctx)`, so
`saneless <cmd> --help` works with a broken config, a failure anywhere in setup
is one message plus exit 2, `-v` means DEBUG for saneless's own loggers only,
and `scan --title` falls back to the profile title via `resolve_job_title`.

## What changed

- **logging_config.py** — level resolved via `logging.getLevelNamesMapping()`;
  after handlers are attached, `logging.getLogger("saneless").setLevel(DEBUG if
  verbose else NOTSET)`. Root keeps the configured level, so httpx (whose DEBUG
  output can carry the Authorization header, T-27-23) stays quiet.
- **cli.py**
  - `cli()` only does `ensure_object`, stores `config_path` and `verbose`, with a
    comment on Click's callback-before-`--help` order.
  - `_load_cli_settings(ctx)`: returns cached `Settings`; else one `try` around
    `load_settings`, `validate_settings_dirs`, `configure_logging` (all by
    module-global name). `except ConfigError` echoes `str(exc)` (D-10, no double
    header); `except Exception` echoes `Configuration error: {exc}`; both exit 2.
    Then `warn_on_legacy_duplex_sources(settings)` and `log_config_sources(settings)`.
  - `scan`, `devices` (`_settings`), `jobs`, `serve`, `auto_profiles` all call it.
  - `serve` comment: uvicorn follows the configured level, not `-v`.
  - `scan --title` defaults to `""`; `resolved_title = resolve_job_title(title,
    settings.profiles[profile], now=now)` after the unknown-profile check, used in
    `PipelineRequest` and `Done:`.
  - `-v` help: "Log saneless's own debug detail to the log file and stderr."
- **auto_profiles.py** — `_UNPRUNABLE` comment reworded ("every command loads
  and validates settings before it runs (only `--help` skips that)"); no code change.

## Tests added

- `tests/test_logging.py`: verbose effective levels (saneless.pipeline DEBUG,
  httpx INFO, root INFO), DEBUG record reaches file, non-verbose call resets to
  NOTSET, WARNING/CRITICAL by name; `_cleanup_handlers` resets the saneless logger.
- `tests/test_cli.py`:
  - `TestLazySettingsLoading`: `--help` for all 5 commands with a raising loader
    and a recording `configure_logging` (never called); real broken config
    `serve --help` exits 0 with no `logs/` dir; real config error printed once
    starting `Configuration error in`; missing `--config` exits 2 naming the
    path; `configure_logging` raising `OSError` exits 2 with `SystemExit`, no
    traceback; memoised loader (1 load, 1 logging setup for 2 calls).
  - `TestStartupConfigLog`: one `Configuration:` record naming the file and
    `paperless.token`, with `tok-SECRET-51aa` absent from records, output and the
    log file; mistyped `tokne` value never echoed (output and stderr).
  - `TestServeCommand.test_serve_log_level_ignores_verbose`.
  - Title: `test_scan_requires_title` replaced by a `Scan <time>` fallback test;
    blank/empty/omitted `--title` -> "Receipt"; typed title kept; unknown profile
    exits 2 with no pipeline run.
  - `_restored_logging()` context manager (also used by
    `TestLegacyDuplexWarningReachesLogFile`) removes added root handlers and
    resets root and saneless levels.

## Commits

| Task | Gate | Commit | Message |
|------|------|--------|---------|
| 1 | RED | af8fe03 | test(27-06): add failing tests for -v DEBUG on saneless loggers only |
| 1 | GREEN | e523a12 | feat(27-06): -v logs saneless's own loggers at DEBUG, root keeps log_level |
| 2 | RED | 6cdf388 | test(27-06): add failing tests for lazy CLI settings, --help without config, CFG-11 line |
| 2 | GREEN | 56bdcdc | feat(27-06): load CLI settings lazily so --help needs no config |
| 3 | RED | 569838f | test(27-06): add failing tests for optional scan --title via the shared rule |
| 3 | GREEN | 71181c3 | feat(27-06): make scan --title optional through resolve_job_title |
| 1 | REFACTOR | 85518d7 | refactor(27-06): reword level-lookup comment in configure_logging |

## Verification

- `uv run pytest -m "not browser and not sane_hardware" -q`: 1572 passed
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`,
  `uv run pyrefly check src tests`: clean (0 errors; pyrefly's 4 pre-existing
  warnings unchanged)
- Acceptance greps: no `getattr(logging` in logging_config.py; one
  `getLevelNamesMapping()`; one `getLogger("saneless").setLevel`; 5
  `_load_cli_settings(ctx)` calls; `ctx.obj["settings"]` only inside the loader;
  one `log_config_sources(settings)`; group callback source contains neither
  `load_settings` nor `configure_logging(`; `--title` has no `required=True`;
  `-k help` selects 11 tests, `-k title` 6, logging `-k "verbose or log_level"` 7.

## Deviations from Plan

1. **[Rule 3 - Blocking] `_patch_cli` has no `run_pipeline` patch.** The plan
   said to capture the `PipelineRequest` "through the existing `run_pipeline`
   patch in `_patch_cli`"; none exists. The title tests patch
   `saneless.cli.run_pipeline` themselves (as `test_scan_with_profile` already
   did) with a recorder that also fires `PipelineEvent.DONE` so `Done:` is printed.
2. **[Rule 1 - Quality] Comment text tripped an acceptance grep.** The first
   GREEN comment in logging_config.py quoted `getattr(logging, ...)`; reworded in
   85518d7 so `grep "getattr(logging"` returns nothing.
3. **Test helper instead of repeated `finally` blocks.** The plan asked to extend
   each real-load test's `finally`; a single `_restored_logging()` context
   manager does the same restore (root handlers, root level, saneless NOTSET) for
   every real-load test. Annotated `-> Generator[None]` because pyrefly flags the
   `Iterator` form as deprecated.
4. The memoisation test calls `saneless.cli._load_cli_settings` via the module
   (`cli_module._load_cli_settings`) so the RED commit failed per-test instead of
   breaking collection of the whole module.
5. The `scan` call is `now = datetime.now(tz=UTC)` then a one-line
   `resolve_job_title(title, settings.profiles[profile], now=now)`, so it fits the
   88-char limit and matches the plan's grep.

## TDD Gate Compliance

RED (`test(...)`) precedes GREEN (`feat(...)`) for all three tasks; each RED run
failed on the target behaviour before implementation.

## Known Stubs

None.

## Self-Check: PASSED

- FOUND: src/saneless/logging_config.py, src/saneless/cli.py, src/saneless/auto_profiles.py, tests/test_logging.py, tests/test_cli.py
- FOUND commits: af8fe03, e523a12, 6cdf388, 56bdcdc, 569838f, 71181c3, 85518d7
