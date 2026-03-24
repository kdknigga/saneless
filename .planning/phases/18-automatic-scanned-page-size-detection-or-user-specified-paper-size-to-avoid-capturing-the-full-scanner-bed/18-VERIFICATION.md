---
phase: 18-automatic-scanned-page-size-detection-or-user-specified-paper-size-to-avoid-capturing-the-full-scanner-bed
verified: 2026-03-24T12:00:00Z
status: passed
score: 7/7 must-haves verified
---

# Phase 18: Paper Size Configuration Verification Report

**Phase Goal:** Users can constrain the scan area to standard paper dimensions (A4, Letter, Legal, etc.) per profile via a `paper_size` setting, using SANE geometry options at the hardware level with a Pillow crop fallback when geometry is unavailable
**Verified:** 2026-03-24
**Status:** passed
**Re-verification:** No — initial verification

---

## Goal Achievement

### Observable Truths (from ROADMAP.md Success Criteria)

| #  | Truth                                                                                                     | Status     | Evidence                                                                                                                       |
|----|-----------------------------------------------------------------------------------------------------------|------------|--------------------------------------------------------------------------------------------------------------------------------|
| 1  | `ProfileConfig(paper_size="a4")` validates successfully; invalid values rejected                          | VERIFIED   | Field `paper_size: Literal["full","a3","a4","a5","letter","legal"] = "full"` at config.py:72; all validation tests pass       |
| 2  | Default `paper_size` is `"full"` -- zero-change upgrade path                                              | VERIFIED   | `ProfileConfig().paper_size` returns `"full"` (smoke test); test_default_is_full passes                                       |
| 3  | `scan_pages()` sets SANE `br_x`/`br_y`/`tl_x`/`tl_y` when `paper_size` is not `"full"`                  | VERIFIED   | `_set_geometry()` helper at sane_backend.py:94 sets all four attributes; TestPaperSizeGeometry (7 tests) pass                 |
| 4  | When scanner geometry options are unavailable, images are cropped with Pillow to target paper dimensions  | VERIFIED   | `_maybe_crop()` at sane_backend.py:133 calls `crop_to_paper_size()`; TestPaperSizeCropFallback tests pass for flatbed and ADF |
| 5  | `paper_size` flows from `ProfileConfig` through `ScanSettings` to `scan_pages()`                         | VERIFIED   | pipeline.py:347 `paper_size=profile.paper_size`; ScanSettings.paper_size at base.py:51; consumed at sane_backend.py:494       |
| 6  | Auto-generated profiles default to `paper_size = "full"` and do not write it to TOML                     | VERIFIED   | `test_auto_generated_profiles_omit_paper_size` passes; `"paper_size" not in content` assertion                                |
| 7  | Configuration reference documentation lists `paper_size` field with all preset values                    | VERIFIED   | docs/reference/configuration.md:64 lists all six values; example at line 112; mkdocs strict build passes                      |

**Score:** 7/7 truths verified

---

## Required Artifacts

| Artifact                                    | Expected                                          | Status      | Details                                                                                  |
|---------------------------------------------|---------------------------------------------------|-------------|------------------------------------------------------------------------------------------|
| `src/saneless/paper_sizes.py`               | PaperSize Literal, PAPER_SIZES_MM, crop utility   | VERIFIED    | 60 lines; exports all three symbols; substantive implementation                          |
| `src/saneless/config.py`                    | `paper_size` Literal field on ProfileConfig       | VERIFIED    | Inline `Literal["full","a3","a4","a5","letter","legal"] = "full"` at line 72             |
| `src/saneless/scanner/base.py`              | `paper_size: str = "full"` on ScanSettings        | VERIFIED    | Field present at line 51                                                                 |
| `src/saneless/pipeline.py`                  | `paper_size=profile.paper_size` in ScanSettings   | VERIFIED    | Present at line 347                                                                      |
| `src/saneless/scanner/sane_backend.py`      | Geometry option setting and crop fallback          | VERIFIED    | `_set_geometry()` + `_maybe_crop()` helpers; both ADF and flatbed paths covered          |
| `tests/test_paper_sizes.py`                 | Unit tests for paper size logic                   | VERIFIED    | 121 lines; 17 tests in 5 test classes; all pass                                          |
| `tests/test_scanner.py`                     | Geometry and fallback tests                        | VERIFIED    | TestPaperSizeGeometry (4 tests) + TestPaperSizeCropFallback (3 tests); all pass          |
| `docs/reference/configuration.md`           | `paper_size` field documentation with all presets | VERIFIED    | Row added at line 64; example usage at line 112; strict docs build passes                |
| `tests/test_auto_profiles.py`               | Test for paper_size omission from TOML output     | VERIFIED    | `test_auto_generated_profiles_omit_paper_size` passes                                    |

---

## Key Link Verification

| From                                    | To                              | Via                                           | Status   | Details                                                          |
|-----------------------------------------|---------------------------------|-----------------------------------------------|----------|------------------------------------------------------------------|
| `src/saneless/config.py`                | `src/saneless/paper_sizes.py`  | PaperSize Literal type                        | NOTE     | Inline Literal used instead of import; TC001 compliance decision |
| `src/saneless/pipeline.py`              | `src/saneless/scanner/base.py` | `paper_size=profile.paper_size` in ScanSettings | WIRED  | Present at pipeline.py:347                                       |
| `src/saneless/scanner/sane_backend.py`  | `src/saneless/paper_sizes.py`  | `PAPER_SIZES_MM` + `crop_to_paper_size`       | WIRED    | Import at sane_backend.py:27; both symbols used in helpers        |

**Key link note:** Plan 01 specified `from saneless.paper_sizes import PaperSize` in config.py, but the implementation deliberately used an inline `Literal` to satisfy the ruff TC001 rule (TYPE_CHECKING-only imports must not be used at runtime). Pydantic requires the type at model-creation time, making TC001 incompatible with a PaperSize import. The inline approach achieves the same validation contract — this is correct and expected per the SUMMARY decisions.

---

## Data-Flow Trace (Level 4)

| Artifact                           | Data Variable    | Source                                      | Produces Real Data | Status    |
|------------------------------------|-----------------|---------------------------------------------|---------------------|-----------|
| `sane_backend.py._set_geometry`    | `paper_size`    | `ScanSettings.paper_size` from ProfileConfig | Yes (from TOML/config) | FLOWING |
| `sane_backend.py._maybe_crop`      | `settings.paper_size` + `geometry_set` | `_set_geometry` return + ScanSettings | Yes | FLOWING |
| `crop_to_paper_size` in paper_sizes.py | `paper_size`, `dpi` | Passed from `_maybe_crop` caller | Yes | FLOWING |

Data originates from user TOML configuration, flows through Pydantic ProfileConfig validation, bridges to ScanSettings in pipeline.py, and reaches the hardware/crop logic in sane_backend.py. No static/empty fallbacks on the happy path.

---

## Behavioral Spot-Checks

| Behavior                                     | Command                                                                                                        | Result    | Status |
|----------------------------------------------|----------------------------------------------------------------------------------------------------------------|-----------|--------|
| ProfileConfig accepts paper_size="a4"        | `uv run python -c "from saneless.config import ProfileConfig; p = ProfileConfig(paper_size='a4'); print(p.paper_size)"` | `a4`    | PASS   |
| Default paper_size is "full"                 | `uv run python -c "from saneless.config import ProfileConfig; print(ProfileConfig().paper_size)"`              | `full`    | PASS   |
| Full test suite passes (340 tests)           | `uv run pytest`                                                                                                | 340 passed | PASS  |
| Ruff linting passes                          | `uv run ruff check .`                                                                                          | All checks passed | PASS |
| Ruff format check passes                     | `uv run ruff format --check .`                                                                                 | 37 files already formatted | PASS |
| ty type checking passes                      | `uv run ty check`                                                                                              | All checks passed | PASS |
| pyrefly type checking passes                 | `uv run pyrefly check`                                                                                         | 0 errors  | PASS   |
| Docs strict build passes                     | `uv run mkdocs build --strict`                                                                                 | Built in 0.25s, no errors | PASS |

---

## Requirements Coverage

PS-01 through PS-06 are phase-specific requirement IDs referenced in ROADMAP.md and the PLANs. They do not appear in `.planning/REQUIREMENTS.md` (which uses a different ID scheme for cross-cutting system requirements). The Success Criteria in ROADMAP.md serve as the functional specification for these IDs. All 7 success criteria are verified above.

| Requirement | Source Plan | Description (inferred from Success Criteria)                                | Status    | Evidence                                        |
|-------------|------------|-----------------------------------------------------------------------------|-----------|--------------------------------------------------|
| PS-01       | 18-01      | ProfileConfig accepts paper_size Literal; invalid values rejected            | SATISFIED | config.py:72; test_invalid_raises_validation_error passes |
| PS-02       | 18-01      | Default paper_size is "full"; zero-change upgrade path                       | SATISFIED | config.py default="full"; test_default_is_full passes |
| PS-03       | 18-01      | scan_pages() sets SANE geometry (tl_x/tl_y/br_x/br_y) when not "full"      | SATISFIED | _set_geometry() in sane_backend.py; TestPaperSizeGeometry passes |
| PS-04       | 18-01      | Pillow crop fallback when geometry options unavailable                        | SATISFIED | _maybe_crop() + crop_to_paper_size(); TestPaperSizeCropFallback passes |
| PS-05       | 18-01/02   | paper_size flows ProfileConfig -> ScanSettings -> scan_pages()               | SATISFIED | pipeline.py:347; base.py:51; sane_backend.py:494 |
| PS-06       | 18-02      | Auto-generated profiles omit paper_size from TOML; docs updated              | SATISFIED | test_auto_generated_profiles_omit_paper_size passes; docs/reference/configuration.md updated |

**Note:** PS-01 through PS-06 are ORPHANED from `.planning/REQUIREMENTS.md` — these IDs exist only in ROADMAP.md and phase documents. This is a documentation gap, not an implementation gap. The global requirements file was not updated to include paper size requirements. This has no impact on functionality but means the system-level requirements document is incomplete.

---

## Anti-Patterns Found

No anti-patterns detected in phase-modified files:

- No TODO/FIXME/PLACEHOLDER comments in modified source files
- No stub implementations (empty returns, hardcoded empty arrays)
- No orphaned artifacts (all new files are imported and used)
- No disconnected data flows

---

## Human Verification Required

The following items require physical hardware and cannot be automated:

### 1. SANE Geometry with Real Scanner

**Test:** Configure a profile with `paper_size = "a4"`, scan a Letter-sized document on a real flatbed scanner that supports tl_x/tl_y/br_x/br_y geometry options.
**Expected:** The scanner returns an image sized approximately 2480 x 3507 pixels at 300 DPI (A4 dimensions), not the full bed size.
**Why human:** Requires physical SANE-compatible scanner with geometry option support; cannot be simulated by unit tests.

### 2. Pillow Crop Fallback with Geometry-Unsupported Scanner

**Test:** Configure a profile with `paper_size = "letter"`, scan using a scanner that does NOT support geometry options (e.g., older flatbed with limited SANE driver).
**Expected:** Scan completes; log shows "Scanner does not support geometry options, will crop after scanning"; output image is cropped to Letter dimensions (215.9 x 279.4 mm at configured DPI).
**Why human:** Requires physical scanner that rejects SANE geometry attribute assignment.

---

## Gaps Summary

No gaps. All 7 success criteria are verified by code evidence and passing tests. Both plan waves (18-01 and 18-02) are fully implemented despite the ROADMAP.md plan counter showing "1/2 plans executed" — this is a stale metadata issue in ROADMAP.md (commit `d3b1df0` and `0075433` prove both plans were completed). The code, tests, and documentation are all in the expected state.

**PS- requirement IDs not in REQUIREMENTS.md:** PS-01 through PS-06 appear only in ROADMAP.md and phase documents, not in the global `.planning/REQUIREMENTS.md`. This is a documentation gap to address in a future cleanup — no functional impact.

---

_Verified: 2026-03-24T12:00:00Z_
_Verifier: Claude (gsd-verifier)_
