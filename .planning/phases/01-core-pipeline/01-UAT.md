---
status: resolved
phase: 01-core-pipeline
source: [01-01-SUMMARY.md, 01-02-SUMMARY.md, 01-03-SUMMARY.md]
started: 2026-03-21T00:00:00Z
updated: 2026-03-21T00:00:00Z
---

## Current Test

[testing complete]

## Tests

### 1. CLI Help Output
expected: Running `uv run saneless --help` shows a Click CLI group with at least "scan" and "devices" subcommands listed.
result: pass

### 2. Device Discovery Command
expected: Running `uv run saneless devices` either lists connected scanners or shows a clear error about SANE/libsane not being available. It should not crash with an unhandled exception.
result: issue
reported: "PermissionError: [Errno 13] Permission denied: '/var/log/saneless' — configure_logging tries to mkdir /var/log/saneless which requires root"
severity: blocker

### 3. Device Discovery JSON Mode
expected: Running `uv run saneless devices --json` outputs valid JSON (a list), not mixed with status messages. If no scanner backend is available, a clear error is shown.
result: issue
reported: "Same PermissionError crash as test 2 — configure_logging tries to mkdir /var/log/saneless before any command logic runs"
severity: blocker

### 4. Config Loading from TOML
expected: Create a file `~/.config/saneless/config.toml` with `[default]` section containing `title = "Test Doc"`. Running `uv run saneless scan` should pick up that config (may fail at scan stage, but should not fail at config loading).
result: skipped
reason: Same logging crash blocks all CLI commands before config loading is reached

### 5. Environment Variable Override
expected: Running `SANELESS_DEFAULT__TITLE="EnvTitle" uv run saneless scan` should use "EnvTitle" as the title (visible in output or error message). The SANELESS_ prefix with __ nested delimiter should work.
result: skipped
reason: Same logging crash blocks all CLI commands before env var override is reached

### 6. Test Suite Passes
expected: Running `uv run pytest` completes with 85+ tests passing, 0 failures.
result: pass

## Summary

total: 6
passed: 2
issues: 2
pending: 0
skipped: 2

## Gaps

- truth: "CLI commands should work without root privileges"
  status: resolved
  reason: "User reported: PermissionError: [Errno 13] Permission denied: '/var/log/saneless' — configure_logging tries to mkdir /var/log/saneless which requires root"
  severity: blocker
  test: 2
  root_cause: "OutputConfig.log_file defaults to /var/log/saneless/saneless.log (root-owned path). configure_logging unconditionally calls mkdir with no error handling. Runs in CLI group callback before any subcommand."
  artifacts:
    - path: "src/saneless/config.py"
      issue: "line 68: log_file defaults to /var/log/saneless/saneless.log"
    - path: "src/saneless/logging_config.py"
      issue: "line 40: mkdir call has no try/except for PermissionError"
  missing:
    - "Change default log path to XDG-compliant ~/.local/state/saneless/saneless.log"
    - "Add error handling in configure_logging for directory creation failures"
  debug_session: ".planning/debug/cli-permission-error-log-path.md"
