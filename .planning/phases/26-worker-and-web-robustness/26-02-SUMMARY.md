---
phase: 26-worker-and-web-robustness
plan: 02
subsystem: config
tags: [config, pydantic-settings, auto-profiles, cli, tdd]
requires: []
provides:
  - "saneless.config.config_search_paths() -- the single config search list"
  - "Settings.config_path -- the TOML file settings were loaded from (None when none)"
  - "auto-profiles CLI writes to settings.config_path, else ./saneless.toml"
affects:
  - "26-08 (worker startup generation consumes settings.config_path; deletes resolve_config_path and TestResolveConfigPath)"
tech-stack:
  added: []
  patterns:
    - "pydantic PrivateAttr + read-only property for load-time facts that env/TOML must not set"
key-files:
  created:
    - .planning/phases/26-worker-and-web-robustness/26-02-SUMMARY.md
  modified:
    - src/saneless/config.py
    - src/saneless/auto_profiles.py
    - src/saneless/cli.py
    - tests/test_config.py
    - tests/test_auto_profiles.py
    - tests/test_cli.py
decisions:
  - "load_settings assigns settings._config_path directly; ty, pyrefly and ruff all accept it, so the fallback _record_config_path helper was not needed"
  - "The CLI test loader stub records --config the way load_settings does, so CLI tests exercise the real write target"
requirements-completed: [ROBU-07]
metrics:
  duration: "~25 min"
  completed: 2026-09-14
  tasks: 2
  files: 6
---

# Phase 26 Plan 02: Loaded config path on Settings Summary

`Settings` now records which TOML file it was loaded from in a private attribute that `SANELESS_CONFIG_PATH` and TOML keys cannot set. There is now one config search list, `config_search_paths()`, and `auto-profiles` writes to the file that was loaded. A parametrised test covers every untouched shape of the default profile for `is_bare_default` (D-16, M-04, ROBU-07 clause 2).

## What was built

- **`config_search_paths()`** (`src/saneless/config.py`, exported): returns `./saneless.toml`, `~/.config/saneless/config.toml` and `/etc/saneless/config.toml` in that order. It is a function, so `Path.home()` is read when called.
- **`Settings._config_path` / `Settings.config_path`**: a `PrivateAttr(default=None)` plus a read-only property. `load_settings` records:
  - the explicit path as given, even if the file is missing (CFG-02 in Phase 27 will make that an error);
  - otherwise the first search path that exists;
  - otherwise `None`.
- **`auto_profiles.resolve_config_path`** loops over `config_search_paths()`, and `Path("/etc/saneless/config.toml")` now appears only in `config.py`. Its docstring says its last production caller is the worker's lazy generation, which 26-08 removes.
- **CLI `auto-profiles`**: `config_path_str` and the `resolve_config_path` import are gone. The command now uses `settings.config_path or Path("./saneless.toml")`, with a comment pointing XDG placement to CFG-03.
- **Tests:**
  - `TestLoadedConfigPath` (10 tests): explicit, missing-explicit, found in CWD, found under HOME, none found, env var cannot forge the path, TOML `config_path` key rejected, `Settings()` gives `None`, search-list order, HOME read at call time.
  - `TestIsBareDefault.test_untouched_default_shapes` (6 cases).
  - Two CLI tests: writes go to the loaded file, and fall back to `./saneless.toml` when no file was loaded.

## Assumption A1 (research Pattern 8)

It held. With no config file, setting `SANELESS_PROFILES__DEFAULT__RESOLUTION` to `DEFAULT_RESOLUTION` gives a profile equal to `ProfileConfig()`, so `is_bare_default` returns True. All six shapes passed on the first run, so `is_bare_default` did not need changing.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] The CLI test loader stub dropped `--config`, so `auto-profiles` wrote into the suite's working directory**
- **Found during:** Task 2 full-suite run (Task 1's targeted gate had passed on a clean tree).
- **Issue:** `_patch_cli` in `tests/test_cli.py` replaced `load_settings` with `lambda *_args, **_kwargs: settings`, which throws the path away. Once `auto-profiles` switched to `settings.config_path`, `test_auto_profiles_generates_profiles` wrote `./saneless.toml` (gitignored) into the worktree root. The next run found that file and printed "No new profiles written".
- **Fix:**
  - Removed the stray file.
  - The stub now sets `settings._config_path` from its argument, the same way `load_settings` does.
  - Added `test_auto_profiles_writes_to_loaded_config_file`, which fails against the old stub, and `test_auto_profiles_without_loaded_file_writes_cwd_default`, both run inside `tmp_path`.
  - Ran the full suite twice in a row; no stray file appeared.
- **Files modified:** tests/test_cli.py (not in the plan's `files_modified`)
- **Commit:** c8b86c3

### Other notes

- The worktree started on an outdated base (merge-base a87b3dd). Per the worktree branch check it was reset to 44ef78d before any work.
- The planned RED test for the TOML `config_path` key already passed before the change. It pins the existing `extra_forbidden` to `ConfigError` path, as the plan said it would.

## TDD Gate Compliance

- RED: `2928810 test(26-02): add failing tests for Settings.config_path and one search list` (9 of 10 failed before the change)
- GREEN: `20f702b feat(26-02): record the loaded config file on Settings and keep one search list`
- Fix: `c8b86c3 fix(26-02): make the CLI test loader stub record --config`
- Task 2: `a588c20 test(26-02): pin is_bare_default untouched shapes`. This test checks existing behaviour, so it passed on its first run as the plan expected.

## Verification

- `uv run pytest -m "not browser and not sane_hardware" -q`: 1085 passed (run twice)
- `uv run pytest tests/test_config.py -q -k "LoadedConfigPath or config_path"`: 10 passed
- `uv run pytest tests/test_auto_profiles.py -q -k untouched_default_shapes`: 6 passed
- `ruff check`, `ruff format --check`, `ty check`, `pyrefly check src tests`: clean. pyrefly shows only 4 warnings that were already there, in other files.
- `uv run prek run --stage pre-push --all-files`: exit 0

## Threat Model

- T-26-05 (mitigate): `config_path` is a `PrivateAttr` behind a read-only property. `test_env_var_cannot_set_config_path` points `SANELESS_CONFIG_PATH` at a real file and asserts `None`. `test_toml_config_path_key_is_rejected` asserts that a TOML key raises `ConfigError`.
- T-26-06: the CLI fallback to `./saneless.toml` is unchanged and documented in a comment. The daemon side is 26-08.

## Known Stubs

None.

## Self-Check: PASSED

- FOUND: src/saneless/config.py, src/saneless/auto_profiles.py, src/saneless/cli.py, tests/test_config.py, tests/test_auto_profiles.py, tests/test_cli.py
- FOUND commits: 2928810, 20f702b, c8b86c3, a588c20
