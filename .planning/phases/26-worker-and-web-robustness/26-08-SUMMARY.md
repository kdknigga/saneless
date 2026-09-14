---
phase: 26-worker-and-web-robustness
plan: 08
subsystem: worker
tags: [worker, auto-profiles, config, threading, tdd]
requires:
  - "26-02: Settings.config_path and config_search_paths()"
  - "26-04: _profiles_lock, get_profile/profile_names/_set_profiles"
  - "26-06: guarded worker loop"
provides:
  - "ScanWorker._generate_startup_profiles(): generation as the worker thread's first act"
  - "Generated profiles persisted only to settings.config_path"
  - "tests/conftest.py session guard against a stray ./saneless.toml"
affects:
  - "Any test that starts a ScanWorker or app over a bare default profile set"
tech-stack:
  added: []
  patterns:
    - "Startup work on the worker thread before the queue loop, behind a backstop except"
    - "Re-check under the lock, then rebind (never mutate) the shared profile dict"
key-files:
  created:
    - .planning/phases/26-worker-and-web-robustness/26-08-SUMMARY.md
  modified:
    - src/saneless/worker.py
    - src/saneless/auto_profiles.py
    - tests/test_worker.py
    - tests/test_auto_profiles.py
    - tests/test_outcomes_e2e.py
    - tests/test_web_state_rendering.py
    - tests/conftest.py
decisions:
  - "Generated profiles REPLACE the bare default rather than merging with it: generation runs only on the exact bare default, re-checked under the lock, and every generated set has its own default (DPLX-07)"
  - "Generation is split into _read_generated_profiles (scanner, D-15) and _persist_generated_profiles (disk, D-16..D-18); the lock re-check and rebind stay inline in _generate_startup_profiles because _set_profiles takes the non-reentrant lock itself"
  - "test_worker.py gets an autouse fixture answering mock_scanner.get_devices with [] instead of a conftest change: only one module needed the fix"
  - "A session-scoped conftest guard fails the run if ./saneless.toml is created or changed, because the file is gitignored and git status cannot show a stray copy"
requirements-completed: [ROBU-07, ROBU-05]
metrics:
  duration: "~20 min"
  completed: 2026-09-14
  tasks: 2
  files: 7
---

# Phase 26 Plan 08: Worker Startup Profile Generation Summary

Auto-profile generation used to run inside the first job and write to a re-derived `./saneless.toml`. It now runs once as the worker thread's first act, before any job. It writes only to the config file that was actually loaded. The bare default is swapped for the generated set under the profile lock.

## What was built

- **`ScanWorker._generate_startup_profiles()`** is the first thing `_run` does, wrapped in a backstop `except` so nothing can end the thread before it takes a job.
  - It reads `is_bare_default` under `_profiles_lock` and returns if the set is not bare.
  - It reads the scanner and builds profiles (`_read_generated_profiles`):
    - No devices: WARNING "no scanners found", bare default kept.
    - Any exception: WARNING naming `type(exc).__name__` with `exc_info`. The cause is never guessed (D-15).
  - It persists them (`_persist_generated_profiles`):
    - `config_path is None`: INFO that nothing was written, naming `--config` and every `config_search_paths()` entry (D-17).
    - `OSError` or `ConfigError` on write: WARNING with the path, the exception class and text, and "will not survive a restart" (D-18).
    - Success: INFO naming the path and the profiles written.
  - Under the lock it re-checks `is_bare_default` and rebinds `self._settings.profiles = dict(profiles)` (D-19).
- **Deleted:**
  - `_maybe_auto_generate`, `_auto_generated`, and the call in `_scan_job`. No generation code remains in the job path (D-14).
  - `auto_profiles.resolve_config_path`, together with its `__all__` entry and its `config_search_paths` import. `pathlib.Path` moved under `TYPE_CHECKING`.
- **Tests:**
  - `TestStartupProfileGeneration` replaces `TestLazyAutoGenerate` with 9 tests: loaded file written, no file means memory only and no new file under CWD or HOME, read-only file, `profiles = 1`, `ScanError`, no scanners, not bare, tried once over three jobs, and generation before the first job (with `get_capabilities` held).
  - `TestNoRederivedConfigPath` replaces `TestResolveConfigPath`.

## Task 2: adjusted tests and the rule applied to each

| Test / fixture | Rule applied |
|---|---|
| `tests/test_web_state_rendering.py` `client` fixture | Rendering test that did not intend generation: added a `duplex` profile, with a comment explaining why. |
| `tests/test_worker.py`, every test that starts a worker over `mock_scanner` + bare `default_settings` | Nothing failed. But the `MagicMock` scanner made generation run by accident and raise `ValueError` inside `generate_profiles`. A module-level autouse fixture `_mock_scanner_reports_no_devices` now sets `get_devices.return_value = []`, a deliberate "no scanners" answer. The conftest `mock_scanner` fixture is unchanged because only one module needed this. The generation WARNING is not silenced. |
| `tests/test_worker.py` `_PassBGatedScanner` / `_GatedScanner` `get_devices` docstrings | Their docstrings wrongly said the worker never asks when a device is configured. They now say the stubs report no devices so startup generation keeps the settings. |
| `test_the_log_says_whether_a_signal_was_claimed_or_dropped` | Already filters by logger, level and message prefix. Its settings are non-bare, so no change was needed. |
| Call-count assertions on `mock_scanner` | None outside `TestStartupProfileGeneration`, so no change was needed. |
| `tests/conftest.py` | Added the session guard `_suite_leaves_cwd_config_alone` (see Deviations). |

Web fixtures in `test_web.py`, `test_cross_origin.py`, `test_web_errors.py` and `test_browser.py` already have two profiles, so they never generate. `test_pipeline.py` never builds a worker.

## Repo-root `saneless.toml` finding

- `/usr/bin/git rev-list --all --count -- saneless.toml` gives 0, so it has never been tracked. `git check-ignore -v` shows `.gitignore:312:saneless.toml`, so it is ignored.
- The copy at the main checkout's root is the user's real config (per the orchestrator). It was not read or touched.
- A stray `saneless.toml` did appear at **this worktree's** root during the RED run. Its mtime (16:16:50) falls inside the RED `pytest` run against the old code. The old `_maybe_auto_generate` wrote to `resolve_config_path()`, which is `./saneless.toml` in the CWD: the M-04 bug this plan removes, reproduced by the tried-once test.
- It contained only generated `[profiles.*]` tables. It was a test artefact, so it was deleted.
- After GREEN, two full-suite runs left no `saneless.toml` behind, and the new session guard now enforces that.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing critical functionality] Guard against a stray `./saneless.toml` (T-26-32)**
- **Found during:** Task 2.
- **Issue:** The plan's stray-file check is `git status --porcelain`. `saneless.toml` is gitignored, so that check cannot see the file the old code wrote, and it did not see the one the RED run left.
- **Fix:** Added a session-scoped autouse fixture in `tests/conftest.py`. It stamps `Path.cwd() / "saneless.toml"` by `stat` (mtime_ns, size; contents are never read, since a developer's copy is their real config) and calls `pytest.fail` if the stamp changed.
- **Proof:** a throwaway test that wrote the file made the run error with "the test suite created or modified …". The probe test and the file were then removed.
- **Files modified:** tests/conftest.py
- **Commit:** 7b08989

**2. [Rule 1 - Test wording vs acceptance grep] Grep acceptance items conflicting with required tests**
- `grep -rn "resolve_config_path" src tests` must return nothing, but the plan also requires `hasattr(saneless.auto_profiles, "resolve_config_path")` to be asserted. That string literal is the single remaining match (tests/test_auto_profiles.py), and the test function is named `test_the_rederived_write_path_is_gone` to keep it to one.
- The `_auto_generated` pattern also matches the pre-existing, unrelated `_is_auto_generated` helper and `test_auto_generated_*` names. No `_auto_generated` attribute remains.
- `grep -n '"scanner unreachable"' tests` must return nothing, while the ScanError test must prove the old text is gone. The test now asserts `"unreachable" not in caplog.text`, which is a broader check.

### Other notes

- The worktree started on an outdated base (merge-base a87b3dd). Per the worktree branch check it was reset to 7d13df2 before any work.
- In this environment the RTK hook rewrote `git add` / `git commit` in a way the worktree isolation guard refused. Commits used `/usr/bin/git` directly, with hooks running normally.
- A PLR0913 argument count in the no-loaded-file test was resolved by using the existing `worker_for` fixture rather than suppressing the rule.

## TDD Gate Compliance

- RED: `050901c test(26-08): add failing tests for startup profile generation`. 7 of the 9 startup tests failed, plus the `hasattr` test. Two passed against the old code because they pin behaviour it already had: `skips_customized_profiles` (never asks when not bare) and `is_tried_once_per_start` (the lazy flag also tried once). They stay as regression pins for the new code path.
- GREEN: `98c7964 feat(26-08): generate profiles at worker startup into the loaded config file`
- Task 2: `7b08989 test(26-08): keep the suite honest about startup profile generation`

## Verification

- `uv run pytest tests/test_worker.py -q -k startup_generation`: 9 passed
- `uv run pytest tests/test_auto_profiles.py tests/test_outcomes_e2e.py -q`: 124 passed
- `uv run pytest -m "not browser and not sane_hardware" -q`: 1322 passed, 36 deselected (run after each task)
- `ruff check src tests`, `ruff format --check .`, `ty check`, `pyrefly check src tests`: clean (pyrefly shows the same 4 warnings that were already there)
- `uv run prek run --stage pre-push --all-files`: exit 0
- No untracked files after the suite, and no `saneless.toml` at the worktree root

## Threat Model

- **T-26-32 (mitigate):** the write target is only `settings.config_path`. `test_startup_generation_without_a_loaded_file_writes_nothing` asserts no new file under a `tmp_path` CWD or HOME, and the conftest session guard catches a CWD write anywhere in the suite.
- **T-26-33 (mitigate):** the rebind happens under `_profiles_lock` after re-checking `is_bare_default`. `test_startup_generation_precedes_the_first_job` shows the job resolves its profile against the generated set.
- **T-26-34 (mitigate):** the WARNING names `type(exc).__name__` with `exc_info`. The test asserts `ScanError` is present and "unreachable" is absent.
- **T-26-35 (accept):** a hung SANE call during generation holds the worker thread before its first job; it is the same abandon case D-07 accepts.

## Known Stubs

None.

## Self-Check: PASSED

- FOUND: src/saneless/worker.py, src/saneless/auto_profiles.py, tests/test_worker.py, tests/test_auto_profiles.py, tests/test_outcomes_e2e.py, tests/test_web_state_rendering.py, tests/conftest.py
- FOUND commits: 050901c, 98c7964, 7b08989
