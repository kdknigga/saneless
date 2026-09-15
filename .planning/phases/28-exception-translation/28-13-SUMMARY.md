---
phase: 28-exception-translation
plan: 13
subsystem: docs
tags: [docs, exit-codes, troubleshooting, doc-truth, cancel]
requires:
  - "28-01: ExitCode (0, 1, 2, 3, 4, 5, 130)"
  - "28-09: guarded CLI group, StorageError -> exit 2, unexpected -> exit 5"
  - "28-11: flip-prompt abort -> exit 130, broken prompt / timeout -> exit 1"
  - "28-12: python-sane missing -> exit 2; serve bind/startup failure -> exit 2"
provides:
  - "docs/how-to/troubleshoot-a-failed-scan.md: symptom-organised troubleshooting how-to, in the How-To nav"
  - "docs/reference/cli-commands.md: global ## Exit codes section; every command table lists the codes it can return"
  - "docs/how-to/cli-scripting.md: seven-row exit-code table, flip abort as 130, CANCELLED state"
  - "tests/test_deployment_config.py: six doc-truth tests pinning the tables and page to ExitCode"
affects: []
tech-stack:
  added: []
  patterns:
    - "Doc-truth test compares the set of documented first-cell codes to {int(code) for code in ExitCode}"
key-files:
  created:
    - docs/how-to/troubleshoot-a-failed-scan.md
  modified:
    - tests/test_deployment_config.py
    - docs/how-to/cli-scripting.md
    - docs/reference/cli-commands.md
    - docs/how-to/install-bare-metal.md
    - docs/how-to/scanner-host-discovery.md
    - mkdocs.yml
decisions:
  - "Per-command tables follow the code: devices {0,1,2,5,130} (get_devices/get_capabilities raise ScanError), jobs {0,2,5,130}, serve {0,2,3,5}, auto-profiles {0,1,2,5,130}"
  - "The troubleshooting test also requires the unexpected-error section to mention the token, pinning the T-28-51 mitigation (never share the config, check DEBUG logs for the token)"
metrics:
  duration: "~35 min"
  completed: 2026-09-15
  tasks: 2
  files: 7
requirements: [EXC-02, EXC-04]
---

# Phase 28 Plan 13: Exit-Code Docs and Troubleshooting How-To Summary

Every exit-code table in the docs now matches `ExitCode` and is pinned to it by doc-truth tests. A new "Troubleshoot a Failed Scan" how-to starts from the exit code and walks through each kind of failure. An abort at the flip prompt is documented as exit 130.

## Tasks

| Task | Name | Commits | Files |
|---|---|---|---|
| 1 | Doc-truth tests for the exit-code tables, then the tables and prompt wording | bf279d6 (test), c7b5bfe (docs) | tests/test_deployment_config.py, docs/how-to/cli-scripting.md, docs/reference/cli-commands.md |
| 2 | Troubleshooting how-to, nav entry and cross-links | ba59877 | docs/how-to/troubleshoot-a-failed-scan.md, mkdocs.yml, docs/how-to/install-bare-metal.md, docs/how-to/scanner-host-discovery.md, tests/test_deployment_config.py |

## What Changed

### Tests (tests/test_deployment_config.py)
- **Helpers:**
  - `_documented_codes`: collects the integer first cells of table rows.
  - `_section`: returns a whole-line heading's body.
  - `_command_exit_tables`: returns the `**Exit codes:**` table of each `saneless <cmd>` section, stopping at the first non-table line.
  - `_heading_section` and `_first_table`: used by the troubleshooting test.
- **Exit-code tables:**
  - `test_scripting_exit_code_table_matches_exit_code_enum` and `test_cli_reference_global_exit_code_table_matches_exit_code_enum`: each table lists exactly the ExitCode values.
  - `test_cli_reference_command_exit_codes_are_real`: every command lists only real codes. `scan` is {0,1,2,3,4,5,130}, `serve` is {0,2,3,5}, and `jobs` includes 5 and 130. Scan's exit-1 row no longer mentions an abort.
- **Job database and flip prompt:**
  - `test_job_database_documented_under_exit_code_two`: the exit-2 row mentions the job database and the exit-5 row does not. Checked in both global tables and in the `jobs` and `serve` tables.
  - `test_flip_prompt_abort_documented_as_cancelled`: the old exit-1 sentences are gone, and a sentence about the flip prompt says 130.
- **Troubleshooting page:** `test_troubleshooting_page_is_linked_and_covers_every_exit_code` checks:
  - its first table lists exactly the ExitCode values
  - it has a nav entry
  - it has headings for the seven kinds of failure
  - the Unexpected section mentions the log file, bug and token, but not the job database
  - the Configuration section mentions the job database
  - install-bare-metal.md and scanner-host-discovery.md link to it
- The module docstring now covers the Phase 28 tables and the page.

### cli-scripting.md
- **Exit-code table:** seven rows.
  - Row 2 adds TOML syntax errors, python-sane not installed, and a job database saneless cannot use.
  - Row 3 adds a malformed `paperless.url`.
  - New rows for 4, 5 and 130.
  - A sentence after the table links to the new page.
- **Manual duplex warning:** no, Ctrl-D or Ctrl-C at the flip prompt exits 130. A timeout or a terminal failure exits 1.
- **`state` list:** includes `CANCELLED` and the "Cancelled" label.
- **Scripting example:** the `case` statement handles 4, 5 and 130.

### cli-commands.md
- **Global options note:** a TOML syntax error names its line and column.
- **New `## Exit codes` section:** a seven-row table. Every command exits 5 on an unexpected error and 130 on Ctrl-C, except `serve`, whose Ctrl-C exits 0. Links to the new page.
- **Per-command tables:**
  - `scan`: 0-5 and 130. Row 1 covers timeouts and terminal failures, not an abort. Row 2 adds "no scanner found" and python-sane missing.
  - `devices`: 0, 1, 2, 5, 130.
  - `jobs`: 0, 2 (including the job database), 5, 130, plus a note that it needs no python-sane.
  - `serve`: 0, 2 (port in use, startup failure, python-sane, config, job database), 3 (malformed URL), 5. The "Port bind error" row is gone.
  - `auto-profiles`: 0, 1, 2, 5, 130.
- **Flip-prompt paragraph:** no, Ctrl-D or Ctrl-C cancels, prints one line and exits 130. A terminal read error exits 1 and is logged with its traceback.

### troubleshoot-a-failed-scan.md (new)
- Opens with the exit-code table, then these sections:
  - Scanner errors (exit 1)
  - Configuration errors (exit 2)
  - python-sane is not installed (exit 2)
  - Paperless errors (exit 3)
  - PDF assembly errors (exit 4)
  - Cancelled scans (exit 130)
  - Unexpected errors (exit 5)
- The page describes symptoms and quotes only the documented prefixes. It does not quote full messages, because Phase 30 rewords them.
- The job database entry names what the error reports and suggests moving the file aside. It does not guess at a cause.
- The Paperless section summarises the retry rules and links to consume-directory-fallback.md for the details.
- The Unexpected section says to attach the log file, never the config, and to check a DEBUG log for the token first (T-28-51).

### Other pages
- **mkdocs.yml:** nav entry.
- **install-bare-metal.md:** a "python-sane cannot be imported" entry (exit 2 with an install hint) and a closing link to the new page.
- **scanner-host-discovery.md:** a closing link to the new page.

## Verification

- `uv run pytest tests/test_deployment_config.py -q`: 28 passed. The `-k "exit_code or flip_prompt"` selection has 6 tests and `-k job_database` has 1.
- `uv run pytest -m "not browser and not sane_hardware" -q`: 1891 passed.
- `uv run mkdocs build --strict`: built with no warnings and no anchor messages.
- `ruff check`, `ruff format --check` and `ty check` are clean. `pyrefly check src tests` reports 0 errors, and its 3 warnings were already there.
- `uv run prek run --stage pre-push --all-files`: all hooks pass.
- **Greps:**
  - `| 130 |` matches in both reference files.
  - `Port bind error`, `aborted at the flip prompt` and `detected as empty` have no match under docs/.
  - `CANCELLED` matches in cli-scripting.md.
  - `troubleshoot-a-failed-scan.md` is linked from mkdocs.yml and all four pages.
- **RED runs:**
  - Task 1: the 5 new tests failed before the docs were changed.
  - Task 2: the troubleshooting test failed ("does not exist") before the page was written.

## Deviations from Plan

- **[Rule 2 - Threat mitigation] The troubleshooting test also checks for `token`.** T-28-51 requires the page to warn about the Paperless token in DEBUG logs, so the test now pins that warning.
- **The cli-scripting.md example script handles 4, 5 and 130.** Its `case` statement listed only 1-3, which would have left the scripting guide incomplete.
- **Commits went through `/usr/bin/git`.** The rtk hook rewrites `git`, and the worktree guard refuses the rewritten form. Hooks still ran on every commit and nothing was bypassed.

## Known Stubs

None.

## Threat Flags

None. Only documentation and tests were changed.

## Self-Check: PASSED

- FOUND: docs/how-to/troubleshoot-a-failed-scan.md
- FOUND: tests/test_deployment_config.py (`from saneless.vocabulary import ExitCode`)
- FOUND commits: bf279d6, c7b5bfe, ba59877
