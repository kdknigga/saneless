---
phase: 10-automatic-scanner-profile-creation
verified: 2026-03-21T00:00:00Z
status: passed
score: 14/14 must-haves verified
re_verification: false
---

# Phase 10: Automatic Scanner Profile Creation — Verification Report

**Phase Goal:** Automatically generate scan profiles from scanner capabilities so users don't have to manually write TOML config
**Verified:** 2026-03-21
**Status:** passed
**Re-verification:** No — initial verification

---

## Goal Achievement

### Observable Truths — Plan 01

| #  | Truth                                                                  | Status     | Evidence                                                                        |
|----|------------------------------------------------------------------------|------------|---------------------------------------------------------------------------------|
| 1  | generate_profiles() produces one ProfileConfig per scanner source      | VERIFIED   | `generate_profiles` in `auto_profiles.py` iterates `capabilities.sources`, creates one profile per source plus "default" for flatbed |
| 2  | source_to_slug() maps SANE source names to descriptive slugs case-insensitively | VERIFIED | Implementation uses `lower = source.lower()` and maps flatbed/duplex/adf/feeder patterns; 7 tests pass |
| 3  | pick_closest_resolution() returns 300 when available, nearest when not | VERIFIED   | Uses `min(resolutions, key=lambda r: abs(r - target))`; 3 tests pass           |
| 4  | pick_preferred_mode() returns Color when available, first mode when not | VERIFIED   | Case-insensitive match loop with fallback to `modes[0]`; 3 tests pass          |
| 5  | is_bare_default() returns True only for uncustomized single default profile | VERIFIED | Checks len==1, key=="default", all default field values, auto_generated==False; 4 tests pass |
| 6  | write_profiles_to_config() preserves existing TOML comments            | VERIFIED   | Uses tomlkit round-trip parsing; test_preserves_comments asserts "# SANE network host" survives write |
| 7  | auto_generated=True marker is set on every generated profile           | VERIFIED   | `generate_profiles` sets `auto_generated=True` on every ProfileConfig; `test_all_auto_generated` confirms |
| 8  | Scanner unreachable during generation raises no unhandled exception    | VERIFIED   | `_maybe_auto_generate` in worker has `except Exception` catch-all that logs warning and continues |

### Observable Truths — Plan 02

| #  | Truth                                                                              | Status   | Evidence                                                                                 |
|----|------------------------------------------------------------------------------------|----------|------------------------------------------------------------------------------------------|
| 9  | User can run `saneless auto-profiles` to generate profiles from scanner capabilities | VERIFIED | `@cli.command(name="auto-profiles")` in `cli.py` line 263; calls `generate_profiles(caps)` |
| 10 | CLI prints summary of generated profiles (names, settings, file path)              | VERIFIED | `click.echo(f"Generated {len(written)} profile(s) in {config_path}:")` + per-profile line; `test_auto_profiles_generates_profiles` confirms |
| 11 | CLI refuses to overwrite existing profiles without --force                         | VERIFIED | `write_profiles_to_config(config_path, profiles, force=force)` defaults force=False; `test_auto_profiles_no_force_skips_existing` passes |
| 12 | Worker auto-generates profiles on first scan when only bare default exists         | VERIFIED | `_maybe_auto_generate()` called at start of `_process_job`; checks `is_bare_default`; `test_lazy_auto_generate_on_bare_default` passes |
| 13 | Worker falls back to bare default if scanner is unreachable during lazy trigger    | VERIFIED | `except Exception` in `_maybe_auto_generate` logs warning and returns; `test_lazy_auto_generate_scanner_unreachable` passes |
| 14 | Worker does not re-trigger after profiles have been generated                      | VERIFIED | `_auto_generated` flag set True before attempt; `test_lazy_auto_generate_only_once` confirms `get_capabilities` called once for two jobs |

**Score:** 14/14 truths verified

---

## Required Artifacts

| Artifact                           | Expected                                    | Status     | Details                                                   |
|------------------------------------|---------------------------------------------|------------|-----------------------------------------------------------|
| `src/saneless/auto_profiles.py`    | Profile generation and TOML persistence     | VERIFIED   | 246 lines; all 7 functions present and exported in `__all__` |
| `src/saneless/config.py`           | ProfileConfig with auto_generated field     | VERIFIED   | `auto_generated: bool = False` at line 66                 |
| `tests/test_auto_profiles.py`      | Unit tests for profile generation           | VERIFIED   | 280 lines, 28 tests across 7 test classes, all pass       |
| `src/saneless/cli.py`              | auto-profiles CLI command                   | VERIFIED   | `@cli.command(name="auto-profiles")` at line 263 with --force option |
| `src/saneless/worker.py`           | Lazy trigger before first scan              | VERIFIED   | `_maybe_auto_generate()` at line 159, called at line 202 in `_process_job` |
| `tests/test_cli.py`                | CLI command tests                           | VERIFIED   | `TestAutoProfiles` class with 4 tests, all pass           |
| `tests/test_worker.py`             | Lazy trigger tests                          | VERIFIED   | `TestLazyAutoGenerate` class with 4 tests, all pass       |

---

## Key Link Verification

| From                              | To                                | Via                                              | Status   | Details                                                        |
|-----------------------------------|-----------------------------------|--------------------------------------------------|----------|----------------------------------------------------------------|
| `src/saneless/auto_profiles.py`   | `src/saneless/scanner/base.py`    | DeviceCapabilities input type                    | VERIFIED | `from saneless.scanner.base import DeviceCapabilities` in TYPE_CHECKING block |
| `src/saneless/auto_profiles.py`   | `src/saneless/config.py`          | ProfileConfig output type and Settings input     | VERIFIED | `from saneless.config import ProfileConfig, Settings` at top-level |
| `src/saneless/cli.py`             | `src/saneless/auto_profiles.py`   | import generate_profiles, write_profiles_to_config, resolve_config_path | VERIFIED | `from .auto_profiles import (generate_profiles, resolve_config_path, write_profiles_to_config)` line 19 |
| `src/saneless/worker.py`          | `src/saneless/auto_profiles.py`   | import is_bare_default, generate_profiles, write_profiles_to_config, resolve_config_path | VERIFIED | `from .auto_profiles import (generate_profiles, is_bare_default, resolve_config_path, write_profiles_to_config)` line 16 |
| `src/saneless/cli.py`             | `src/saneless/scanner/sane_backend.py` | SaneBackend().get_capabilities(device_id)   | VERIFIED | `scanner.get_capabilities(device_id)` called in `auto_profiles` command body |

---

## Requirements Coverage

All 9 AP requirements are defined in `10-RESEARCH.md`. Plans claim and summaries confirm completion of all 9.

| Requirement | Source Plan | Description                           | Status    | Evidence                                                          |
|-------------|-------------|---------------------------------------|-----------|-------------------------------------------------------------------|
| AP-01       | 10-01       | Generate profiles from capabilities   | SATISFIED | `generate_profiles()` produces ProfileConfig dict; 3 tests pass   |
| AP-02       | 10-01       | Source-to-slug mapping                | SATISFIED | `source_to_slug()` with 7 case-insensitive patterns; 7 tests pass |
| AP-03       | 10-01       | Resolution/mode selection             | SATISFIED | `pick_closest_resolution()` and `pick_preferred_mode()`; 6 tests pass |
| AP-04       | 10-01       | Bare default detection                | SATISFIED | `is_bare_default()`; 4 tests pass                                 |
| AP-05       | 10-01       | TOML writing preserves comments       | SATISFIED | tomlkit round-trip; `test_preserves_comments` passes              |
| AP-06       | 10-02       | CLI auto-profiles command             | SATISFIED | `@cli.command(name="auto-profiles")`; 4 CLI tests pass            |
| AP-07       | 10-02       | Lazy trigger on first scan            | SATISFIED | `_maybe_auto_generate()` in worker; 4 worker tests pass           |
| AP-08       | 10-01       | Force flag behavior                   | SATISFIED | `force=` kwarg in `write_profiles_to_config`; force/no-force tests pass |
| AP-09       | 10-01       | Scanner unreachable fallback          | SATISFIED | `except Exception` in `_maybe_auto_generate`; `test_lazy_auto_generate_scanner_unreachable` passes |

No orphaned requirements — all 9 AP IDs appear in plan frontmatter (10-01 claims AP-01 through AP-05, AP-08, AP-09; 10-02 claims AP-06, AP-07).

---

## Anti-Patterns Found

No blockers or warnings found.

Reviewed files: `src/saneless/auto_profiles.py`, `src/saneless/config.py`, `src/saneless/cli.py`, `src/saneless/worker.py`, `tests/test_auto_profiles.py`, `tests/test_cli.py`, `tests/test_worker.py`.

- No TODO/FIXME/placeholder comments
- No stub return values (`return null`, `return {}`, empty handlers)
- No console.log-only implementations
- No `# noqa` or `# type: ignore` suppressions in phase 10 files

---

## Code Quality

| Check           | Result       |
|-----------------|--------------|
| ruff check .    | 0 errors     |
| ruff format .   | clean        |
| ty check        | 0 errors     |
| pyrefly check   | 0 errors     |
| pytest (full)   | 262 passed   |
| pytest (phase)  | 36 passed (28 + 4 + 4) |

---

## Human Verification Required

None. All verification automated. The only scenario requiring physical hardware (real SANE scanner) is explicitly out of scope for unit testing — the worker and CLI are tested with mocked `SaneBackend`.

---

## Summary

Phase 10 fully achieves its goal. The `auto_profiles` module delivers 7 pure functions covering the complete profile generation pipeline. The `ProfileConfig.auto_generated` field is backward-compatible. TOML writing via tomlkit preserves comments. The CLI command and worker lazy trigger are both wired to the module with correct imports and substantive implementations. All 9 AP requirements are satisfied. 262 tests pass, all linters and both type checkers are clean.

---

_Verified: 2026-03-21_
_Verifier: Claude (gsd-verifier)_
