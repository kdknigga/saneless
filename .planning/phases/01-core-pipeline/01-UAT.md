---
status: complete
phase: 01-core-pipeline
source: [01-01-SUMMARY.md, 01-02-SUMMARY.md, 01-03-SUMMARY.md, 01-04-SUMMARY.md]
started: 2026-03-21T15:00:00Z
updated: 2026-03-21T15:00:00Z
---

## Current Test

[testing complete]

## Tests

### 1. CLI Help Output
expected: Running `uv run saneless --help` shows a Click CLI group with at least "scan" and "devices" subcommands listed.
result: pass

### 2. Device Discovery Command
expected: Running `uv run saneless devices` either lists connected scanners or shows a clear error about SANE/libsane not being available. It should NOT crash with a PermissionError or unhandled exception.
result: pass

### 3. Device Discovery JSON Mode
expected: Running `uv run saneless devices --json` outputs valid JSON (a list), not mixed with status messages. If no scanner backend is available, a clear error is shown. No PermissionError crash.
result: pass

### 4. Config Loading from TOML
expected: Create a file `~/.config/saneless/config.toml` with `[default]` section containing `title = "Test Doc"`. Running `uv run saneless scan` should pick up that config (may fail at scan stage, but should not fail at config loading).
result: issue
reported: "Configuration error: 1 validation error for Settings default Extra inputs are not permitted [type=extra_forbidden, input_value={'title': 'Test Doc'}, input_type=dict]"
severity: major

### 5. Environment Variable Override
expected: Running `SANELESS_DEFAULT__TITLE="EnvTitle" uv run saneless scan` should use "EnvTitle" as the title (visible in output or error message). The SANELESS_ prefix with __ nested delimiter should work.
result: issue
reported: "Same extra_forbidden validation error — config.toml with title field causes Pydantic to reject it before env var override is reached"
severity: major

### 6. Log Path XDG Default
expected: The default log path is now `~/.local/state/saneless/saneless.log`, not `/var/log/saneless/saneless.log`. After running any CLI command, check if `~/.local/state/saneless/` was created.
result: pass

### 7. Graceful Logging Fallback
expected: If the log directory is unwritable, the CLI should still work (logging falls back to stderr). No crash on PermissionError.
result: pass

### 8. Test Suite Passes
expected: Running `uv run pytest` completes with all tests passing, 0 failures.
result: pass

## Summary

total: 8
passed: 6
issues: 2
pending: 0
skipped: 0

## Gaps

- truth: "Config loading accepts title field in [default] section of config.toml"
  status: failed
  reason: "User reported: Configuration error: 1 validation error for Settings default Extra inputs are not permitted [type=extra_forbidden, input_value={'title': 'Test Doc'}, input_type=dict]"
  severity: major
  test: 4
  root_cause: ""
  artifacts: []
  missing: []
  debug_session: ""

- truth: "Environment variable override with SANELESS_ prefix works for nested config"
  status: failed
  reason: "User reported: Same extra_forbidden validation error — config.toml with title field causes Pydantic to reject it before env var override is reached"
  severity: major
  test: 5
  root_cause: ""
  artifacts: []
  missing: []
  debug_session: ""
