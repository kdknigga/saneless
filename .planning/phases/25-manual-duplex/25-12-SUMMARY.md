---
phase: 25-manual-duplex
plan: 12
subsystem: config, cli
tags: [manual-duplex, config, logging, gap-closure, WR-01, WR-05, IN-04]
gap_closure: true
requires:
  - 25-01 (ProfileConfig legacy translation, Settings legacy warning validator, flip_timeout_seconds)
  - 25-10 (test_outcomes_e2e.py flip-timeout case and worker-release test)
provides:
  - "OutputConfig.flip_timeout_seconds = Field(default=600, ge=1, le=86_400)"
  - "config.warn_on_legacy_duplex_sources(settings) -> None, exported in __all__"
  - "cli() emits the legacy manual-duplex warning after configure_logging, so it reaches log_file"
affects:
  - 25-15 (docs: flip_timeout_seconds range 1..86400; warning now also covers a legacy source with explicit non-manual duplex)
tech-stack:
  added: []
  patterns:
    - "Load-time warnings that an operator must see are emitted by a function the CLI calls after logging is configured, not by a pydantic validator"
    - "Tests that invoke the real cli() snapshot root-logger handlers and level and restore them in finally"
key-files:
  created: []
  modified:
    - src/saneless/config.py
    - src/saneless/cli.py
    - tests/test_config.py
    - tests/test_cli.py
    - tests/test_outcomes_e2e.py
decisions:
  - "WR-01 bounds are 1..86400 seconds; '0 means no timeout' is deliberately not adopted"
  - "D-03 as amended: translation stays in ProfileConfig's before-validator; naming moves from a Settings validator to warn_on_legacy_duplex_sources, called by cli() after configure_logging. Programmatic Settings(...) no longer logs"
  - "IN-04: explicit duplex still wins over the legacy inference, but a legacy-looking source with duplex none/hardware now gets its own WARNING"
requirements: [DPLX-02, DPLX-05]
metrics:
  duration: "~20 min"
  completed: 2026-09-14
  tasks: 2
  commits: 4
---

# Phase 25 Plan 12: Config-load fixes for flip timeout and legacy duplex warning (WR-01, WR-05, IN-04) Summary

`flip_timeout_seconds` now only accepts 1 to 86400 seconds, checked when the config loads. The legacy manual-duplex deprecation warning moved out of a `Settings` validator into `warn_on_legacy_duplex_sources(settings)`, which `cli()` calls after `configure_logging`, so the warning now reaches `log_file`. It also warns about a legacy-looking source that has an explicit non-manual `duplex`.

## What changed

### Task 1: Bound flip_timeout_seconds (WR-01)
- `src/saneless/config.py`: `flip_timeout_seconds: int = Field(default=600, ge=1, le=86_400)`. The D-10 comment was extended with the reasons: zero or a negative value would fail every job right after pass A, and a value above `threading.TIMEOUT_MAX` makes `Event.wait` raise `OverflowError`.
- `tests/test_config.py`: `test_flip_timeout_seconds_accepts_zero` was replaced. New tests: parametrised rejection of 0, -5 and 86_401; acceptance of 1 and 86_400; and a TOML `= 0` that must fail through `load_settings`.
- `tests/test_cli.py`: `test_unanswered_prompt_times_out_before_pass_b` still runs the whole command but now uses `flip_timeout_seconds=1`. New `test_the_coordinator_times_out_at_a_zero_timeout` checks that `ClickFlipCoordinator().wait_for_flip(0)` returns `TIMED_OUT`, which costs no wall-clock time.
- `tests/test_outcomes_e2e.py`: `_FLIP_TIMEOUT_BUDGET = 1`. Terminal waits on runs that wait out the flip timeout now use a budget of `case.flip_timeout + 2.0`, both in the parametrised runner when `awaits_flip and not operator_flips` and in the first wait of `TestFlipTimeoutReleasesTheWorker`. The module docstring's speed notes were updated.

### Task 2: Warning after logging, plus the non-manual case (WR-05, IN-04)
- `src/saneless/config.py`: the `Settings.warn_on_legacy_duplex_source` field validator was deleted. The new module-level `warn_on_legacy_duplex_sources(settings)` logs the existing message unchanged for `duplex == "manual"`, and the IN-04 message ("...has not read it as manual duplex...") for `none`/`hardware`. Messages use %-style args on `saneless.config`. `ProfileConfig._translate_legacy_manual_duplex` is untouched.
- `src/saneless/cli.py`: the function is imported and called right after `configure_logging(...)`, with a WR-05 comment.
- `tests/test_config.py`: the warning tests now call `load_settings` and then `warn_on_legacy_duplex_sources`. New tests cover: loading alone emits nothing, IN-04 for `none` and `hardware`, and an `ADF`/`manual` profile staying silent.
- `tests/test_cli.py`: new `TestLegacyDuplexWarningReachesLogFile` builds a real TOML config under `tmp_path` with tomlkit and runs the real `cli` with `--config ... jobs --limit 1`. It checks that the log file contains `'legacy'` and `duplex = "manual"`, and restores the root logger's handlers and level in `finally`.

## TDD Gate Compliance

| Task | RED | GREEN |
|------|-----|-------|
| 1 | 4b3e7b0 `test(25-12)`: 4 failed (DID NOT RAISE) | e2feb1a `feat(25-12)` |
| 2 | 24c9c4a `test(25-12)`: ImportError on the new function; log-file test failed (`'legacy'` not in empty file) | 7d407dd `feat(25-12)` |

No refactor commits were needed.

## Verification

- `uv run pytest -q`: 1077 passed
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, `uv run pyrefly check src tests`: clean. pyrefly reports 4 warnings, all in unrelated files (`pipeline.py`, `sane_backend.py`, `tests/fake_sane.py`).
- `uv run prek run --all-files` and `uv run prek run --stage pre-push --all-files`: all hooks pass.
- All acceptance greps from both tasks pass. `warn_on_legacy_duplex_sources(settings)` is on cli.py line 206, after `configure_logging(` on line 198.

## Deviations from Plan

**1. [Rule 1 - Bug] The CLI log-file test also restores the root logger level**
- **Found during:** Task 2
- **Issue:** `configure_logging` also calls `root_logger.setLevel(...)`, so removing only the added handlers would leave the process-wide root level changed for later tests.
- **Fix:** the test's `finally` saves and restores `logging.getLogger().level` as well as the handlers.
- **Files modified:** tests/test_cli.py
- **Commit:** 24c9c4a

**2. [Rule 1 - Accuracy] Updated the e2e module docstring's speed claim**
- **Found during:** Task 1
- **Issue:** the docstring said all cases "sleep for well under a second" and that the flip wait "resolves in microseconds". Both became false once the flip budget is 1 s.
- **Fix:** reworded both bullets.
- **Files modified:** tests/test_outcomes_e2e.py
- **Commit:** e2feb1a

Otherwise the plan was executed as written. The IN-04 message format string matches the plan's text; only the Python string-literal line breaks differ, and they were placed so the grep for "has not read it as manual duplex" still matches on one line.

## Known Stubs

None.

## Self-Check: PASSED

- FOUND: src/saneless/config.py, src/saneless/cli.py, tests/test_config.py, tests/test_cli.py, tests/test_outcomes_e2e.py
- FOUND commits: 4b3e7b0, e2feb1a, 24c9c4a, 7d407dd
