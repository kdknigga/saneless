---
phase: 16-when-a-scanner-advertised-auto-mode-it-should-be-configurable-by-the-user-if-that-means-flatbed-mode-or-adf-mode
verified: 2026-03-22T23:30:00Z
status: passed
score: 11/11 must-haves verified
re_verification: false
---

# Phase 16: Configurable Auto Source Mode — Verification Report

**Phase Goal:** When a scanner advertised auto mode, it should be configurable by the user if that means flatbed mode or ADF mode
**Verified:** 2026-03-22T23:30:00Z
**Status:** passed
**Re-verification:** No — initial verification

---

## Goal Achievement

### Observable Truths (Plan 16-01)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | ProfileConfig accepts auto_source_mode with values 'flatbed' or 'adf', defaulting to 'flatbed' | VERIFIED | `config.py:71` — `auto_source_mode: Literal["flatbed", "adf"] = "flatbed"`; 4 tests pass in `TestAutoSourceMode` |
| 2 | ScanSettings carries auto_source_mode through to scan_pages() | VERIFIED | `base.py:50` — `auto_source_mode: str = "flatbed"`; `pipeline.py:346` — `auto_source_mode=profile.auto_source_mode` wires it through |
| 3 | scan_pages() routes Auto source to ADF path when auto_source_mode='adf' | VERIFIED | `sane_backend.py:427-428` — `if effective_source == "Auto": use_adf = settings.auto_source_mode == "adf"`; test `test_auto_source_adf_routes_to_adf_path` passes |
| 4 | scan_pages() routes Auto source to flatbed path when auto_source_mode='flatbed' | VERIFIED | Same block evaluates to `use_adf=False`; test `test_auto_source_flatbed_routes_to_flatbed_path` passes |
| 5 | Explicit sources (Flatbed, ADF, ADF Duplex) are unaffected by auto_source_mode | VERIFIED | Guard is `if effective_source == "Auto"` — explicit sources skip the block; tests `test_explicit_adf_ignores_auto_source_mode` and `test_explicit_flatbed_ignores_auto_source_mode` pass |
| 6 | _is_adf_source() remains unchanged | VERIFIED | `sane_backend.py:93-95` body is exactly `return "adf" in source.lower()` — untouched |

**Score: 6/6 truths verified**

### Observable Truths (Plan 16-02)

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 7 | source_to_slug('Auto') returns 'auto-scan' | VERIFIED | `auto_profiles.py:48-49` — `if lower == "auto": return "auto-scan"`; tests `test_auto_source` and `test_auto_source_case_insensitive` pass |
| 8 | generate_profiles() sets auto_source_mode='adf' for Auto source when no Flatbed source exists | VERIFIED | `auto_profiles.py:159-161` — `has_flatbed` check; tests `test_auto_with_adf_no_flatbed_sets_adf_mode` and `test_auto_only_sets_adf_mode` pass |
| 9 | generate_profiles() sets auto_source_mode='flatbed' for Auto source when Flatbed source exists | VERIFIED | Same block; test `test_auto_with_flatbed_sets_flatbed_mode` passes |
| 10 | write_profiles_to_config() persists auto_source_mode when non-default | VERIFIED | `auto_profiles.py:248-249` — `if profile.auto_source_mode != "flatbed": profile_table.add("auto_source_mode", ...)`; tests `test_writes_auto_source_mode_adf` and `test_omits_auto_source_mode_flatbed` both pass |
| 11 | Configuration reference docs list auto_source_mode field | VERIFIED | `docs/reference/configuration.md:63` — field row with description and "Ignored for explicit sources"; complete example at lines 123-127 with `[profiles.auto-scan]` |

**Score: 5/5 truths verified**

**Combined score: 11/11 truths verified**

---

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/saneless/config.py` | ProfileConfig with auto_source_mode Literal field | VERIFIED | Line 71: `auto_source_mode: Literal["flatbed", "adf"] = "flatbed"` |
| `src/saneless/scanner/base.py` | ScanSettings with auto_source_mode field | VERIFIED | Line 50: `auto_source_mode: str = "flatbed"` |
| `src/saneless/pipeline.py` | ScanSettings construction passing auto_source_mode | VERIFIED | Line 346: `auto_source_mode=profile.auto_source_mode` |
| `src/saneless/scanner/sane_backend.py` | Auto source conditional routing | VERIFIED | Lines 427-433: `if effective_source == "Auto":` block with routing and logger.info |
| `src/saneless/auto_profiles.py` | Auto source slug and smart auto_source_mode defaulting | VERIFIED | `source_to_slug` handles "auto" case; `generate_profiles` sets auto_source_mode; `write_profiles_to_config` persists non-default |
| `docs/reference/configuration.md` | auto_source_mode field documentation | VERIFIED | Field row in profiles table plus complete `[profiles.auto-scan]` example |

---

### Key Link Verification

| From | To | Via | Status | Details |
|------|-----|-----|--------|---------|
| `src/saneless/config.py` | `src/saneless/pipeline.py` | profile.auto_source_mode passed to ScanSettings constructor | WIRED | `pipeline.py:346` — `auto_source_mode=profile.auto_source_mode` |
| `src/saneless/pipeline.py` | `src/saneless/scanner/sane_backend.py` | ScanSettings.auto_source_mode read in scan_pages routing | WIRED | `sane_backend.py:428` — `settings.auto_source_mode == "adf"` |
| `src/saneless/auto_profiles.py` | `src/saneless/config.py` | ProfileConfig constructed with auto_source_mode kwarg | WIRED | `auto_profiles.py:167` — `auto_source_mode=auto_source_mode` in ProfileConfig constructor |
| `src/saneless/auto_profiles.py` | TOML config file | write_profiles_to_config writes auto_source_mode field | WIRED | `auto_profiles.py:248-249` — conditional `profile_table.add("auto_source_mode", ...)` |

---

### Requirements Coverage

The D-xx IDs are phase-internal design decisions defined in `16-CONTEXT.md`, not entries in `REQUIREMENTS.md`. No D-xx IDs appear in the global REQUIREMENTS.md traceability table. All 10 design decisions are implemented and tested:

| ID | Description | Plan | Status | Evidence |
|----|-------------|------|--------|----------|
| D-01 | auto_source_mode field on ProfileConfig with "flatbed"/"adf" values | 16-01 | SATISFIED | `config.py:71` |
| D-02 | Default value is "flatbed" | 16-01 | SATISFIED | `config.py:71` — `= "flatbed"` default |
| D-03 | Field only affects routing when effective_source is "Auto" | 16-01 | SATISFIED | `sane_backend.py:427` — `if effective_source == "Auto":` guard |
| D-04 | In scan_pages(), override use_adf for Auto source via settings.auto_source_mode | 16-01 | SATISFIED | `sane_backend.py:426-433` |
| D-05 | _is_adf_source() remains unchanged | 16-01 | SATISFIED | Function body is exactly `return "adf" in source.lower()` |
| D-06 | ScanSettings gets auto_source_mode field with "flatbed" default | 16-01 | SATISFIED | `base.py:50` |
| D-07 | generate_profiles() sets auto_source_mode based on other sources | 16-02 | SATISFIED | `auto_profiles.py:158-161` |
| D-08 | source_to_slug("Auto") returns "auto-scan" | 16-02 | SATISFIED | `auto_profiles.py:48-49` |
| D-09 | Users configure via TOML profile section | 16-02 | SATISFIED | `write_profiles_to_config` persists non-default values; docs show example |
| D-10 | Document the field in reference docs | 16-02 | SATISFIED | `docs/reference/configuration.md:63` |

**Orphaned requirements check:** No D-xx IDs appear in REQUIREMENTS.md traceability table mapping to Phase 16, so there are no orphaned requirements. The D-xx IDs are design decisions, not tracked v1 requirements.

---

### Anti-Patterns Found

No anti-patterns found. Scanned all 6 modified files:

- No TODO/FIXME/HACK/PLACEHOLDER comments in modified code paths
- No stub return values (return null / return {}) in implementation
- No hardcoded empty data flowing to rendering
- No form handlers that only call preventDefault
- All state variables populated from real logic (not empty defaults persisted to output)

---

### Human Verification Required

None. All behavioral assertions are covered by automated tests. The only real-hardware behavior (actual scanner with Auto source) cannot be tested without physical hardware, but the routing logic is fully unit-tested with mock scanner devices.

---

### Code Quality

| Check | Result |
|-------|--------|
| `uv run ruff check` (all 5 modified source files) | PASSED — zero errors |
| `uv run ruff format --check` (all 5 modified source files) | PASSED — all formatted |
| `uv run ty check` | PASSED — all checks passed |
| `uv run pytest` (full suite) | PASSED — 314 tests, 0 failures |
| Commit hashes from summaries exist in git | VERIFIED — 1477686, 55c09eb, d7bfa2d, 12ded9c, bf6a5f8 all present |

---

### Summary

Phase 16 fully achieves its goal. The complete data flow is verified at every layer:

1. **Config layer:** ProfileConfig accepts and validates `auto_source_mode` as `Literal["flatbed", "adf"]`
2. **Pipeline bridge:** ScanSettings carries the value from config to the scanner backend
3. **Scan routing:** scan_pages() uses the value to decide ADF vs flatbed path, but only for "Auto" sources — explicit sources are completely unaffected
4. **Profile generation:** Auto-generated profiles for "Auto" sources intelligently default based on other available sources
5. **TOML persistence:** Non-default values are written to config; default "flatbed" is omitted for clean TOML
6. **Documentation:** Reference docs have the field in the profiles table with description and a complete example profile

All 314 existing tests pass, demonstrating zero regressions.

---

_Verified: 2026-03-22T23:30:00Z_
_Verifier: Claude (gsd-verifier)_
