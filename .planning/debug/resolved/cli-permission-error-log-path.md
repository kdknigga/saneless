---
status: resolved
trigger: "All CLI commands crash with PermissionError on /var/log/saneless"
created: 2026-03-21T00:00:00Z
updated: 2026-03-21T00:00:00Z
---

## Current Focus

hypothesis: OutputConfig.log_file defaults to /var/log/saneless/saneless.log which requires root
test: Read config.py default value
expecting: Hardcoded /var/log path
next_action: Report root cause

## Symptoms

expected: CLI commands run as normal user without errors
actual: All CLI commands crash with PermissionError: [Errno 13] Permission denied: '/var/log/saneless'
errors: PermissionError at logging_config.py line 40 - Path(log_file).parent.mkdir(parents=True, exist_ok=True)
reproduction: Run any CLI command as non-root user
started: Since OutputConfig was written with /var/log default

## Eliminated

## Evidence

- timestamp: 2026-03-21T00:00:00Z
  checked: src/saneless/config.py OutputConfig class
  found: log_file default is hardcoded to "/var/log/saneless/saneless.log" (line 68)
  implication: This is a root-owned path, normal users cannot create directories there

- timestamp: 2026-03-21T00:00:00Z
  checked: src/saneless/logging_config.py configure_logging function
  found: Line 40 calls Path(log_file).parent.mkdir(parents=True, exist_ok=True) with no error handling
  implication: If mkdir fails due to permissions, the entire CLI crashes with no fallback

- timestamp: 2026-03-21T00:00:00Z
  checked: src/saneless/cli.py cli() group function
  found: configure_logging is called at lines 56-62 in the CLI group callback, before any subcommand runs
  implication: Every single CLI command triggers configure_logging, so all commands crash

## Resolution

root_cause: OutputConfig.log_file defaults to "/var/log/saneless/saneless.log" (config.py line 68), a root-owned system path. configure_logging (logging_config.py line 40) unconditionally tries to mkdir that path with no error handling. Since this runs in the CLI group callback before any subcommand, every CLI invocation crashes for non-root users.
fix: (not applied - diagnosis only)
verification: (not applied - diagnosis only)
files_changed: []
