---
phase: 28-exception-translation
plan: 02
subsystem: config
tags: [exceptions, config, tomllib, tomlkit, security]
requires: []
provides:
  - "load_settings raises only ConfigError for file-level failures (syntax, encoding, read)"
  - "write_profiles_to_config raises ConfigError for tomlkit parse failures"
affects:
  - "28-09 (cli._load_cli_settings generic catch can be removed without turning a bad file into exit 5)"
  - "worker._persist_generated_profiles (parse failures now take the ConfigError branch)"
tech-stack:
  added: []
  patterns:
    - "Chain to the cause only when the cause's str() holds no file content; ValidationError stays `from None`"
key-files:
  created: []
  modified:
    - src/saneless/config.py
    - src/saneless/auto_profiles.py
    - src/saneless/worker.py
    - tests/test_config.py
    - tests/test_auto_profiles.py
    - tests/test_cli.py
    - tests/test_worker.py
decisions:
  - "TOMLDecodeError / UnicodeDecodeError / OSError ConfigErrors are chained (`from cause`); ValidationError ones stay `from None` (D-12, Phase 27 D-14)"
  - "Token-absence tests check str(cause), not repr(cause): repr(UnicodeDecodeError) includes its object bytes, and tracebacks render str()"
metrics:
  duration: "~25 min"
  completed: 2026-09-15
  tasks: 2
  files: 7
requirements: [EXC-01, EXC-02]
---

# Phase 28 Plan 02: Config file read/parse failures become ConfigError Summary

`_build_settings` now turns tomllib syntax errors, non-UTF-8 files and unreadable files into one `line N, column M: ...` / `not valid UTF-8` / `cannot read the file:` row under the Phase 27 `Configuration error in <file>:` header, chained to the cause. `auto_profiles._read_config` turns tomlkit `ParseError` into a `Cannot update <file>: it is not valid TOML at line N, column M (...)` ConfigError before any write happens.

## Tasks

| Task | Name | Commits | Files |
| ---- | ---- | ------- | ----- |
| 1 | TOML syntax, unreadable and non-UTF-8 config files become ConfigError | 32ee1c6 (RED), 48cb002 (GREEN) | src/saneless/config.py, tests/test_config.py, tests/test_cli.py |
| 2 | tomlkit parse failures on the auto-profiles write path become ConfigError | cf31b0b (RED), 1e8f0f5 (GREEN) | src/saneless/auto_profiles.py, tests/test_auto_profiles.py |
| - | Follow-up: worker WR-04 test fix (Rule 1) | 71316c3 | tests/test_worker.py, src/saneless/worker.py |

## What changed

- `config.py`: `import tomllib`; `_build_settings` has a `cause` local and three new `except` clauses after `ValidationError`. Only `lineno`, `colno`, `msg` (through `_escape_name`), `start`, `reason` and `strerror` are rendered; `exc.doc` and `exc.object` never are (T-28-05). The raise is `from cause` when set, else `from None`, with the comment explaining why. The "propagates unchanged (Phase 28)" docstring sentence is gone and `load_settings`'s Raises section names the new cases.
- `auto_profiles.py`: `from tomlkit.exceptions import ParseError`; `_read_config` wraps `tomlkit.parse` and raises `ConfigError(msg) from exc`. tomlkit uses `repr` for the characters it names, so control characters come out escaped.
- `TestInvalidToml` now has 7 tests: plain ConfigError, header plus line/column, token absence for a syntax error, control-character escaping, non-UTF-8, token absence for non-UTF-8, and unreadable (0o000, skipped when running as root).
- `TestDurableConfigWrite.test_invalid_toml_config_is_config_error` checks the `Cannot update` prefix, `line 1, column 4`, `Unexpected character`, that `__cause__` is a `ParseError`, and that the file bytes are unchanged.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] CLI test depended on the old behaviour**
- **Found during:** Task 1 verify (`tests/test_cli.py`)
- **Issue:** `TestLazySettingsLoading.test_toml_syntax_error_is_one_line_exit_2` expected the generic `Configuration error: ` prefix. Its docstring said this would hold only "until Phase 28". The loader's header replaces that prefix now.
- **Fix:** The test now expects `Configuration error in <file>:` followed by `  line 1, column ...`, still exit 2. cli.py is unchanged, as the plan requires.
- **Files modified:** tests/test_cli.py
- **Commit:** 48cb002

**2. [Rule 1 - Bug] Worker WR-04 test used an unparseable file to reach the generic handler**
- **Found during:** Overall verification (full non-browser suite)
- **Issue:** `test_startup_generation_keeps_profiles_when_the_write_raises_anything_else` used `[profiles\n` to get an `UnexpectedCharError` into the `except Exception` branch. After Task 2 that file raises a ConfigError instead, so the test no longer reached the generic handler.
- **Fix:** Split into two tests. `test_startup_generation_invalid_toml_config_is_a_config_error` checks the new ConfigError path. The anything-else test now patches `saneless.worker.write_profiles_to_config` to raise `TypeError`, so WR-04 is still covered. Also updated the stale worker.py comment that gave ParseError and UnicodeDecodeError as examples of "anything else" (comment only, no code change).
- **Files modified:** tests/test_worker.py, src/saneless/worker.py
- **Commit:** 71316c3

**3. [Plan clarification] Token-absence check covers str(cause) only**
- A first draft also asserted the token was absent from `repr(err.__cause__)`. That fails for `UnicodeDecodeError`, whose repr includes the `object` bytes. The plan specifies `str(cause)`, which is what a traceback prints, so the helper follows the plan and its docstring explains why. See Threat Flags.

## Threat Flags

| Flag | File | Description |
|------|------|-------------|
| threat_flag: info-disclosure | src/saneless/config.py | A chained `UnicodeDecodeError` cause keeps the file bytes in `.object`, and `repr(cause)` shows them, token included. Standard tracebacks and `logging` use `str()` and are safe. Anything that prints exception reprs or frame locals, such as rich tracebacks with `show_locals`, would expose them. The syntax-error cause (`TOMLDecodeError`) is safe: its `args` and `repr` hold only the message. |

## Verification

- `uv run pytest -m "not browser and not sane_hardware" -q`: 1671 passed
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run ty check`, `uv run pyrefly check src tests`: all clean (pyrefly: 0 errors)
- Acceptance greps: `except tomllib.TOMLDecodeError as exc` appears once; `exc.doc` and `propagates unchanged` do not appear; `except ParseError as exc` and `raise ConfigError(msg) from exc` each appear once in auto_profiles.py
- `-k toml` selects 26 tests in test_config.py and 11 in test_auto_profiles.py, all passing
- TDD gates: each task has a `test(28-02)` RED commit followed by a `feat(28-02)` GREEN commit

## Self-Check: PASSED

- FOUND: src/saneless/config.py, src/saneless/auto_profiles.py, tests/test_config.py, tests/test_auto_profiles.py
- FOUND commits: 32ee1c6, 48cb002, cf31b0b, 1e8f0f5, 71316c3
