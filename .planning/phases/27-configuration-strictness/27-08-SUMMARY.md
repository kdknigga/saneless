---
phase: 27-configuration-strictness
plan: 08
subsystem: docs
tags: [docs, configuration, environment-variables, cli, xdg, static-test]
requires:
  - "27-03 (strict loader, error line shapes, unknown SANELESS_* rejection, CFG-11 log line)"
  - "27-04 (auto-profiles merge, grouped output labels, EBUSY exit 2)"
  - "27-05 (tests/test_deployment_config.py structure, profile how-to anchor)"
provides:
  - "configuration.md: XDG search entry, Validation section, XDG_STATE_HOME defaults, ~ expansion, CRITICAL/warn levels, literal title"
  - "environment-variables.md: SANELESS_PROFILES__<NAME>__<FIELD>, unknown SANELESS_* rejection with exit 2, startup source log"
  - "cli-commands.md: -v scope, --help without config, optional --title, --force merge, EBUSY exit 2"
  - "12 new doc-truth tests in tests/test_deployment_config.py (20 total)"
affects:
  - "27-06 and 27-07 (docs describe behaviour they implement in parallel; cross-check list below)"
tech-stack:
  added: []
  patterns:
    - "plain-text doc assertions scoped to a Markdown table row or a ## section"
key-files:
  created: []
  modified:
    - tests/test_deployment_config.py
    - docs/reference/configuration.md
    - docs/reference/environment-variables.md
    - docs/explanation/architecture.md
    - docs/getting-started/first-cli-scan.md
    - docs/how-to/install-bare-metal.md
    - saneless.toml.example
    - docs/reference/cli-commands.md
    - docs/how-to/cli-scripting.md
    - docs/explanation/empty-page-detection.md
decisions:
  - "The env reference says a default profile must still exist when only SANELESS_PROFILES__ variables define profiles (verified: with no file, SANELESS_PROFILES__RECEIPT__TITLE alone fails the default-profile check)"
  - "cli-commands.md states once under Global Options that every command exits 2 on a config load failure, instead of adding exit-2 rows to devices/jobs/serve"
  - "auto-profiles output labels are repeated in cli-commands.md as a text block (matching the profile how-to) with a link for key ownership detail"
requirements: [CFG-01, CFG-02, CFG-03, CFG-04, CFG-05, CFG-06, CFG-07, CFG-08, CFG-10, CFG-11]
metrics:
  duration: "~25 min"
  completed: 2026-09-15
  tasks: 2
  files: 10
---

# Phase 27 Plan 08: Configuration, environment and CLI reference corrections Summary

Every remaining doc sentence Phase 27 made true or false is now corrected. That covers the configuration and environment-variable references, the CLI reference and scripting how-to, the empty-page tip, the per-user search-path lists, and `saneless.toml.example`. Twelve new static tests keep the corrections from drifting back.

## What was built

### Task 1: configuration and environment references, search paths, TOML example
- **docs/reference/configuration.md**
  - Search entry 3 is now `$XDG_CONFIG_HOME/saneless/config.toml`, falling back to `~/.config/...` when the variable is unset, empty or relative.
  - A path that is not a regular file is skipped. A missing or non-file `--config` exits 2, and `~` is expanded in `--config`.
  - New `## Validation` section: unknown keys are rejected in every section and at the top level. Each problem gets one line naming the file or env variable, the section and the key, with a close match and the valid keys (or "it belongs in"). Values are never printed, and the exit code is 2. It shows the two example lines from 27-03.
  - The token is never logged.
  - `~` is expanded in `tmp_dir`, `data_dir`, `log_file` and `consume_dir`, with no `$VAR` expansion.
  - `data_dir` and `log_file` defaults are under `$XDG_STATE_HOME/saneless`.
  - `log_level` accepts DEBUG/INFO/WARNING/ERROR/CRITICAL, case-insensitive, with `warn` read as WARNING; anything else is rejected.
  - Profile `title` is a literal default (typed title, then profile title, then `Scan <date time>`).
- **docs/reference/environment-variables.md**
  - The false "Profile fields cannot be set" note is replaced with `SANELESS_PROFILES__<NAME>__<FIELD>`.
  - New "Unknown variables are rejected" note: the four valid first segments, exit 2, and the `SANELESS_PAPERLES__TOKEN` and `SANELESS_OUTPUT_WEB_PORT` suggestions. An unknown field is reported naming the variable, and values are never printed.
  - New startup-source-log note.
  - The `data_dir` default is `$XDG_STATE_HOME/saneless`. The "Docker deployments commonly use..." bullet is left alone (DOCS-06).
- architecture.md, first-cli-scan.md and install-bare-metal.md show `$XDG_CONFIG_HOME/saneless/...` with the `~/.config/saneless/` default.
- saneless.toml.example: the `log_level` comment lists the five levels, and the `title` comment says "literal default title, used when the title is left blank".

### Task 2: CLI reference, scripting how-to, empty-page tip
- **docs/reference/cli-commands.md**
  - The `-v` row is saneless's own DEBUG output, written to the log file and mirrored to stderr; libraries and the web server keep `log_level`. The `--config` row mentions exit 2.
  - A Global Options paragraph says `--help` needs no valid config, every command exits 2 on a config load failure, and one INFO startup line names the file and the env-sourced setting names.
  - Scan synopsis is `scan [--title TEXT] [--profile NAME]`. The `--title` default is the profile's `title`, else `Scan <date time>`, and a blank title counts as omitted.
  - The scan exit 2 row adds a missing `--config`, an unknown config key and an unknown `SANELESS_*` variable.
  - The `auto-profiles --force` row now describes the merge. Exit 2 adds the rewrite failure and EBUSY on a single-file mount.
  - The overwrite sentence is replaced by a merge paragraph covering `./saneless.toml` in the current directory, mode 0600, the absolute path output and the grouped output labels, with a link to the profile how-to.
- **docs/how-to/cli-scripting.md**
  - Exit-2 examples are updated.
  - A sentence says `--title` may be omitted.
  - The "Regenerating profiles" paragraph was rewritten (see Deviations).
- **docs/explanation/empty-page-detection.md**: the tip now suggests `saneless -v scan ...` or `log_level = "DEBUG"`. The mean and stddev lines are `logger.debug` calls on `saneless.pages`, so `-v` shows them.

## Doc sentences that depend on 27-06 / 27-07 wording (cross-check after merge)

These describe behaviour being implemented in parallel. They were written to the 27-06 and 27-07 PLAN.md text and are not yet backed by merged code in this worktree:

| Doc | Sentence (abridged) | Depends on |
|-----|---------------------|------------|
| cli-commands.md `-v` row | "Log saneless's own debug detail (DEBUG) to the log file and mirror it to stderr; other libraries and the web server keep the configured `log_level`" | 27-06 Task 1 (`saneless` logger DEBUG, root unchanged) and Task 2 (uvicorn follows `log_level`, not `-v`) |
| cli-commands.md Global Options | "`--help` on any command works without a valid config file; settings are loaded only when a command runs." | 27-06 Task 2 lazy `_load_cli_settings` |
| cli-commands.md Global Options | "prints one line per problem" / "logs at INFO which config file it loaded and which setting names came from environment variables" | 27-06 Task 2 (ConfigError printed as-is, `log_config_sources` called from the CLI). `log_config_sources` itself landed in 27-03 |
| cli-commands.md scan synopsis and `--title` row | `scan [--title TEXT]`; default "the profile's `title`, else `Scan <date time>`"; "a blank title counts as omitted" | 27-06 Task 3 (`--title` default `""`, `resolve_job_title`) |
| cli-scripting.md | "`--title` may be omitted, in which case the profile's `title` (else `Scan <date time>`) is used" | 27-06 Task 3 |
| empty-page-detection.md tip | "run the scan with `saneless -v scan ...`" to see the per-page mean and stddev | 27-06 Task 1 (without it, `-v` only mirrors to stderr at `log_level`) |
| configuration.md search entry 3; architecture.md; first-cli-scan.md; install-bare-metal.md | `$XDG_CONFIG_HOME/saneless/config.toml`, default `~/.config/...` when unset, empty or relative | 27-07 Task 1 (`xdg_config_home`, `config_search_paths`) |
| configuration.md `data_dir` / `log_file` rows; environment-variables.md `data_dir` note | defaults `$XDG_STATE_HOME/saneless` and `.../saneless.log` | 27-07 Task 1 (`xdg_state_home`, `default_factory`) |
| configuration.md `[output]` intro and path rows; `consume_dir` row | "a leading `~` is expanded ... `$HOME` inside a value is not expanded" | 27-07 Task 2 (`_expand_user` validators) |

Behaviour that had already landed and was checked against this worktree's code:
- The error line shapes and the `Configuration error in <file>:` header (config.py).
- Unknown `SANELESS_*` suggestions, which were run live: `SANELESS_PAPERLES__TOKEN` suggests `SANELESS_PAPERLESS__TOKEN`, `SANELESS_OUTPUT_WEB_PORT` suggests `SANELESS_OUTPUT__WEB_PORT`, and `SANELESS_SCANNER__HOSTNAME` is reported with the variable name.
- `warn` is read as `WARNING`, and profile names from env are lower-cased.
- `~` is expanded in an explicit `--config` path (27-03).
- `auto-profiles` group labels, EBUSY giving exit 2, and a new file getting mode 0600 (`atomic_write.py`).

## Commits

| Task | Gate | Commit | Message |
|------|------|--------|---------|
| 1 | RED | f8578f2 | test(27-08): add failing doc-truth tests for config and env references |
| 1 | GREEN | c71ac78 | docs(27-08): correct config and env references for strict, XDG-aware loading |
| 2 | RED | 4c5e31e | test(27-08): add failing doc-truth tests for CLI reference and scripting how-to |
| 2 | GREEN | 9cbbd1a | docs(27-08): correct CLI reference, scripting how-to and empty-page tip |

## Verification

- `uv run pytest tests/test_deployment_config.py -q`: 20 passed. RED runs failed first: 6 failed / 8 passed, then 6 failed / 14 passed.
- `uv run pytest -m "not browser and not sane_hardware" -q`: 1562 passed.
- `ruff check .`, `ruff format --check .`, `ty check` and `pyrefly check src tests` are all clean (pyrefly: 0 errors).
- `uv run mkdocs build --strict` exits 0. The anchors `#validation`, `#notes` and `#auto-generated-profiles` are present in the built HTML.
- Acceptance greps:
  - No `--log-level` anywhere under docs.
  - No `(required)` on the `--title` row.
  - No `Overwrite existing profiles`.
  - No `Profile fields cannot be set via environment variables`.
  - `XDG_STATE_HOME` and `CRITICAL` are in configuration.md.
  - `title template` appears only in `docs/PRD.md:110`, the historical PRD, which the plan's verification grep excludes and which was left unchanged.

## Deviations from Plan

**1. [Rule 1 - Bug] Stale "Regenerating profiles" paragraph in cli-scripting.md**
- **Found during:** Task 2
- **Issue:** The paragraph said `--force` "overwrites existing auto-generated profiles" and that hand-written profiles are affected "unless they have the same name". D-01 and 27-04 made both false: a same-name unflagged profile is never touched.
- **Fix:** Rewrote it to describe the merge and the skip, with a link to the profile how-to. The plan said to leave the scripting *examples* unchanged, and they are; this paragraph is prose.
- **Commit:** 9cbbd1a

**2. [Rule 2 - Accuracy] Default-profile caveat in environment-variables.md**
- A live check showed that `SANELESS_PROFILES__RECEIPT__TITLE` with no config file fails the default-profile check. The profile-fields note says so, so an operator following the new guidance does not hit an unexplained exit 2.
- **Commit:** c71ac78

## Known Stubs

None.

## Threat Flags

None. T-27-29 is covered by the `-v` row, which says libraries and the web server keep `log_level`. T-27-30 is covered by the "Unknown variables are rejected" note (exit code and suggestion format). T-27-31: no example contains a real token.

## Self-Check: PASSED

- FOUND: all 10 modified files
- FOUND commits: f8578f2, c71ac78, 4c5e31e, 9cbbd1a
