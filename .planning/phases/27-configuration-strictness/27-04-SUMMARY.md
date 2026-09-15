---
phase: 27-configuration-strictness
plan: 04
subsystem: config-write
tags: [auto-profiles, tomlkit, atomic-write, cli, worker]
requires:
  - 27-02 (replace_file_atomically)
provides:
  - ProfileWriteResult (added/refreshed/skipped_not_generated/skipped_existing/removed, persisted, groups(), describe())
  - merge-aware write_profiles_to_config (_OWNED_KEYS, D-01..D-03)
  - durable write path (UTF-8 bytes, CRLF kept, tomllib guard, atomic replace, symlink log)
  - auto-profiles grouped output and exit 2 on ConfigError/OSError
affects:
  - src/saneless/worker.py startup auto-generation log and in-memory profile choice
  - plan 27-06 (lazy CLI settings) rebases over the auto_profiles command body
tech-stack:
  added: []
  patterns:
    - frozen slots dataclass result with a shared describe() vocabulary for CLI and worker
    - in-place tomlkit table merge of an owned key set
    - dump -> CRLF normalise -> tomllib re-parse guard -> replace_file_atomically
key-files:
  created: []
  modified:
    - src/saneless/auto_profiles.py
    - src/saneless/worker.py
    - src/saneless/cli.py
    - tests/test_auto_profiles.py
    - tests/test_cli.py
    - tests/test_worker.py
decisions:
  - "ProfileWriteResult.groups() returns (line, written_names) pairs; describe() is built on it so the CLI can print per-profile detail lines under Added/Refreshed without a second vocabulary"
  - "The D-01 reason is appended to the Skipped (not auto-generated) line after ' -- '"
  - "Unflagged or non-mapping same-name entries are both reported as skipped_not_generated"
  - "A merge with no added, refreshed or removed names does not call replace_file_atomically; result.path is then config_path.resolve()"
  - "The merge loop and the read/render steps live in private helpers (_merge_profile, _read_config, _render_checked) to stay under PLR0912"
metrics:
  duration: ~40min
  completed: 2026-09-15
  tasks: 2
  files: 6
---

# Phase 27 Plan 04: auto-profiles --force merge and durable config write Summary

`auto-profiles --force` now merges: it rewrites only the six generated keys, and only in profiles marked `auto_generated = true`. Hand-written profiles are left alone and reported. The new `ProfileWriteResult` is printed with the same group wording by the CLI and by the worker's startup log. Every rewrite reads the file as UTF-8 bytes, keeps CRLF line endings, re-parses the output with tomllib, and goes through `replace_file_atomically`.

## What was built

### Task 1: Merge semantics and the grouped result (CFG-07)
- `_OWNED_KEYS` holds the six keys the tool owns: source, resolution, mode, auto_source_mode, duplex, auto_generated. `_merge_profile` puts each generated name in one group:
  - **added:** the file has no table with that name, so a new one is written in the same key order as before.
  - **skipped_not_generated (D-01):** the table has no truthy flag. It is not touched, with or without `--force`.
  - **skipped_existing:** the table is flagged but `--force` was not given.
  - **refreshed (D-02, D-03):** flagged and forced. The owned keys are written onto the existing table, and any owned key the new generation leaves out is deleted.
- `ProfileWriteResult` is a frozen, slotted dataclass that also has `persisted`, `groups()` and `describe()`. It is exported in `__all__`.
- `_generated_values` keeps the Phase 25 D-06 rule: `auto_source_mode` and `duplex` are written only when they differ from their defaults.
- Worker: `_profiles_after_persist(loaded, generated, result)` uses `result.persisted`. The success log is `Auto-profiles: <path>: <groups joined by '; '>`, or `no changes`.
- CLI: prints `Profiles in <resolved path>:`, then each group line, with detail lines under Added and Refreshed. Output goes through `_echo_write_result`. The `--force` help text is updated, and the old "XDG placement is CFG-03" comment is replaced.

### Task 2: Durable write path (CFG-08)
- `_read_config` decodes the file's bytes as UTF-8. A `UnicodeDecodeError` becomes `ConfigError("<path> is not valid UTF-8; refusing to rewrite it")`.
- When the `profiles` section is an inline table, new profiles are added as `tomlkit.inline_table()`. A standard table there would dump invalid TOML.
- `_render_checked` dumps the document. If the original had CRLF, it turns lone `\n` into `\r\n`. It then runs `tomllib.loads` on the result and raises `ConfigError` (file left intact) if parsing fails.
- A merge that adds, refreshes and removes nothing does not replace the file.
- `replace_file_atomically` returns the real target, which becomes `result.path`. If that differs from `config_path.absolute()`, an INFO line names both the link and the target (D-07).
- CLI: `except ConfigError` echoes the message and exits 2. `except OSError` prints `Cannot write <path>: <strerror>` and exits 2.
- Worker: no source change. Its existing `(OSError, ConfigError)` branch already handles EBUSY and non-UTF-8 files, and the new tests confirm it.

## Commits

| Task | Gate | Commit | Message |
|------|------|--------|---------|
| 1 | RED | 5ea59ce | test(27-04): add failing tests for --force merge and grouped write result |
| 1 | GREEN | 897445a | feat(27-04): merge --force into auto-generated profiles with grouped result |
| 2 | RED | 4860d96 | test(27-04): add failing tests for the durable config write path |
| 2 | GREEN | 9915f07 | feat(27-04): durable UTF-8, CRLF-preserving, guarded atomic profile write |

## Verification

- `uv run pytest -m "not browser and not sane_hardware"`: 1505 passed, 53 deselected
- `ruff check .`, `ruff format --check .`, `ty check` and `pyrefly check src tests` are all clean
- `-k "force or merge"` in tests/test_auto_profiles.py selects 14 tests, all passing (at least 6 required)
- `-k "ebusy or crlf or utf8 or inline or symlink"` across the three test modules selects 10 tests, all passing (at least 8 required)
- Acceptance greps:
  - no `write_text` or `read_text` in auto_profiles.py
  - one `replace_file_atomically(` and one `tomllib.loads(`
  - `inline_table()` is present
  - `except ConfigError` is in `auto_profiles`
  - no `set(written)` in worker.py
  - no `No new profiles written` anywhere
- One-off check (not a test): a split super-table (`[profiles.a]`, `[output]`, `[profiles.b]`) under `--force` merged in place and re-parsed correctly.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Worker test expected UnicodeDecodeError for a non-UTF-8 config**
- **Found during:** Task 2
- **Issue:** `test_startup_generation_keeps_profiles_when_the_write_raises_anything_else` had a case asserting that `UnicodeDecodeError` shows up in the worker WARNING. D-05 now turns that error into a `ConfigError`, so the old expectation no longer holds.
- **Fix:** Removed that case from the parametrization and updated the test's docstring. Added `test_startup_generation_non_utf8_config_is_a_config_error_utf8`, which asserts ConfigError, "UTF-8", an unchanged file, and profiles kept in memory.
- **Files modified:** tests/test_worker.py
- **Commit:** 4860d96

**2. [Rule 3 - Blocking] PLR0912 branch limit in write_profiles_to_config**
- **Found during:** Task 1 and Task 2
- **Fix:** Moved the per-name merge into `_merge_profile` and the read and render steps into `_read_config` and `_render_checked`. The helper does the added-branch assignment as `section[name] = table`. The acceptance grep for `profiles_section[name] = ` therefore finds nothing, but the rule it checks still holds: a fresh table is only created for a name that is not already there.
- **Files modified:** src/saneless/auto_profiles.py
- **Commits:** 897445a, 9915f07

**3. [Rule 3 - Blocking] Type checkers rejected `dict(tomlkit item)` in CLI tests**
- **Fix:** The CLI tests now read the file back with `tomllib.loads` instead of `tomlkit.parse`.
- **Files modified:** tests/test_cli.py
- **Commit:** 897445a

## Known Stubs

None.

## TDD Gate Compliance

Both tasks have a `test(27-04)` RED commit followed by a `feat(27-04)` GREEN commit. No REFACTOR commits were needed.

## Self-Check: PASSED

- FOUND: src/saneless/auto_profiles.py, src/saneless/worker.py, src/saneless/cli.py, tests/test_auto_profiles.py, tests/test_cli.py, tests/test_worker.py
- FOUND commits: 5ea59ce, 897445a, 4860d96, 9915f07
