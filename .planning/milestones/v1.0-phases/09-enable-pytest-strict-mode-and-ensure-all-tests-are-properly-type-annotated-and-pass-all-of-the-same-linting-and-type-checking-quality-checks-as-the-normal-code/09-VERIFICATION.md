---
phase: 09-enable-pytest-strict-mode
verified: 2026-03-21T22:00:00Z
status: passed
score: 13/13 must-haves verified
re_verification: null
gaps: []
human_verification: []
---

# Phase 9: Enable Pytest Strict Mode and Test Quality Parity — Verification Report

**Phase Goal:** Full test quality parity with production code -- type annotations, docstrings, and lint compliance on all test files, with pytest strict mode enforcing warnings-as-errors and strict markers
**Verified:** 2026-03-21T22:00:00Z
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| #  | Truth                                                                 | Status     | Evidence                                                                            |
|----|-----------------------------------------------------------------------|------------|-------------------------------------------------------------------------------------|
| 1  | ty check runs on tests/ and passes clean                              | VERIFIED   | `uv run ty check` exits 0; ty section in pyproject.toml has no exclude             |
| 2  | pyrefly check src tests runs on tests/ and passes clean                         | VERIFIED   | `uv run pyrefly check src tests` reports 0 errors; pyrefly section has no project_excludes   |
| 3  | All type: ignore comments in test files are eliminated                | VERIFIED   | grep for "type: ignore" across all tests/*.py returns 0 matches                    |
| 4  | All 226 existing tests pass                                           | VERIFIED   | `uv run pytest -x -q` exits 0 with "226 passed"                                   |
| 5  | Every test function and fixture has a return type annotation          | VERIFIED   | ruff check passes with ANN in select and no ANN exemption for tests               |
| 6  | Every test function parameter has a type annotation                   | VERIFIED   | ruff check passes clean; ANN removed from per-file-ignores                         |
| 7  | Every public test function, class, and module has a docstring         | VERIFIED   | ruff check passes clean with D rules enabled; all module/class/function docstrings |
| 8  | Lazy imports moved to top-level; PLC0415 removed from test ignores    | VERIFIED   | PLC0415 absent from tests/**/*.py per-file-ignores; ruff passes clean              |
| 9  | ANN removed from test per-file-ignores                                | VERIFIED   | pyproject.toml tests per-file-ignores contains only S101, ARG, S104, S105, S106   |
| 10 | pytest runs in strict mode with all strictness settings enabled       | VERIFIED   | pyproject.toml has strict_markers=true, strict_config=true, xfail_strict=true     |
| 11 | filterwarnings = error catches all warnings as failures               | VERIFIED   | pyproject.toml has filterwarnings = ["error"]                                      |
| 12 | No ResourceWarning from unclosed SQLite connections                   | VERIFIED   | 226 tests pass under filterwarnings=error; store.close() in test_job.py (7x) and _capture_uvicorn in test_cli.py |
| 13 | Full quality gate: ruff + ty + pyrefly + pytest all green             | VERIFIED   | All four tools confirmed passing at 0 errors/violations                             |

**Score:** 13/13 truths verified

### Required Artifacts

| Artifact                | Expected                                            | Status     | Details                                                                         |
|-------------------------|-----------------------------------------------------|------------|---------------------------------------------------------------------------------|
| `pyproject.toml`        | ty/pyrefly exclusion removal                        | VERIFIED   | [tool.ty.src] and [tool.pyrefly] sections are empty (no exclusions)            |
| `pyproject.toml`        | Strict pytest config (strict_markers etc.)          | VERIFIED   | strict_markers=true, strict_config=true, xfail_strict=true, filterwarnings=["error"] |
| `pyproject.toml`        | Cleaned per-file-ignores (only S101/ARG/S104-106)   | VERIFIED   | ANN and PLC0415 absent from tests section; confirmed with literal text search   |
| `tests/test_scanner.py` | _FakeSaneDevice concrete stub                       | VERIFIED   | class _FakeSaneDevice present; 0 type: ignore comments                          |
| `tests/test_browser.py` | Fixed ABC override signatures                       | VERIFIED   | `def get_capabilities(self, device_id: str)` present (no underscore prefix)    |
| `tests/test_job.py`     | None-narrowing assertions + JobStore cleanup        | VERIFIED   | `assert fetched is not None` present; store.close() called 7 times              |
| `tests/test_logging.py` | Explicit logging.handlers import                    | VERIFIED   | `import logging.handlers` present                                               |
| `tests/conftest.py`     | Fully annotated shared fixtures                     | VERIFIED   | All defs have return type annotations; 0 unannotated defs                       |
| `tests/test_cli.py`     | Fully annotated (53+ functions)                     | VERIFIED   | 39+ `-> None` annotations; _capture_uvicorn helper for resource cleanup         |

### Key Link Verification

| From                           | To            | Via                                 | Status   | Details                                                              |
|--------------------------------|---------------|-------------------------------------|----------|----------------------------------------------------------------------|
| pyproject.toml [tool.ty.src]   | tests/        | ty now includes tests               | WIRED    | Section is empty — no exclude directive present                      |
| pyproject.toml [tool.pyrefly]  | tests/        | pyrefly now includes tests          | WIRED    | Section is empty — no project_excludes directive present             |
| pyproject.toml filterwarnings  | tests/        | pytest warning promotion to errors  | WIRED    | filterwarnings = ["error"] confirmed; 226 tests pass under it        |
| pyproject.toml per-file-ignores | tests/**/*.py | ruff enforcement scope              | WIRED    | Only S101, ARG, S104, S105, S106 remain — ANN and PLC0415 removed   |

### Requirements Coverage

TQUAL requirements (TQUAL-01 through TQUAL-07) are referenced in ROADMAP.md Phase 9 and claimed complete in each plan's SUMMARY.md. However, **these requirement IDs do not exist in REQUIREMENTS.md** — the traceability table ends at CLI-03 and contains no TQUAL entries. This is a documentation gap: the TQUAL requirement definitions were used in planning artifacts but never added to the central REQUIREMENTS.md.

Despite this documentation gap, the functional goals described by each requirement ID are demonstrably achieved in code:

| Requirement | Source Plan | Functional Description (from ROADMAP/PLAN context)    | Status   | Evidence                                                       |
|-------------|-------------|-------------------------------------------------------|----------|----------------------------------------------------------------|
| TQUAL-01    | 09-01       | ty check covers tests/ with zero errors               | VERIFIED | uv run ty check exits 0; no exclude in [tool.ty.src]          |
| TQUAL-02    | 09-01       | pyrefly check src tests covers tests/ with zero errors          | VERIFIED | uv run pyrefly check src tests reports 0 errors; no project_excludes    |
| TQUAL-03    | 09-02       | All test functions have return type annotations       | VERIFIED | ruff passes with ANN; no ANN in tests per-file-ignores        |
| TQUAL-04    | 09-02       | All test parameters have type annotations             | VERIFIED | ruff passes with ANN; 0 ANN violations                        |
| TQUAL-05    | 09-02       | All public test code has docstrings; lazy imports moved | VERIFIED | ruff passes with D; PLC0415 absent from tests per-file-ignores |
| TQUAL-06    | 09-03       | pytest strict mode enabled (markers, config, xfail)  | VERIFIED | strict_markers/strict_config/xfail_strict all true in config  |
| TQUAL-07    | 09-03       | filterwarnings=error; all ResourceWarnings fixed      | VERIFIED | filterwarnings=["error"]; 226 tests pass clean                |

**ORPHANED IDs:** TQUAL-01 through TQUAL-07 appear in ROADMAP.md and all three PLAN frontmatter files but are absent from REQUIREMENTS.md. They are defined nowhere in REQUIREMENTS.md. This is a traceability documentation gap — the requirements exist functionally but are not formally registered. The gap does not affect phase goal achievement but should be noted for completeness.

### Anti-Patterns Found

No blocking anti-patterns found.

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| None | —    | —       | —        | —      |

Additional checks performed:
- `grep "type: ignore" tests/*.py` returned 0 matches across all 15 test files
- `uv run ruff check .` — 0 violations (includes ANN, D, PLC rules)
- No `noqa` suppressions in test files (beyond justified `# noqa: PLC0415` for sane module monkeypatching — none found in final codebase, all lazy imports were moved to top-level)

### Human Verification Required

None. All verification performed programmatically. Per CLAUDE.md, Playwright MCP is available for browser validation but this phase has no browser-facing changes. All quality tool checks, test execution, and code pattern verification are fully automated.

The only items that could theoretically require human attention are physical hardware (scanner devices not present in CI), which are already skipped in the test suite via the `browser` marker.

### Gaps Summary

No gaps. All phase goals are achieved and verified.

The TQUAL requirement IDs appearing in ROADMAP.md and plan frontmatter but not in REQUIREMENTS.md is a pre-existing documentation inconsistency that does not affect code quality or phase goal achievement. The phase goal — "Full test quality parity with production code" — is fully realized:

1. Both type checkers (ty and pyrefly) cover tests/ with zero errors
2. All test code has ANN-compliant type annotations (return types, parameter types)
3. All public test code has docstrings
4. All lazy imports moved to top-level
5. ruff check passes with no test exemptions beyond S101/ARG/S104-106
6. pytest runs under full strict mode with filterwarnings=error
7. All 226 tests pass under maximum strictness with zero warnings

---

_Verified: 2026-03-21T22:00:00Z_
_Verifier: Claude (gsd-verifier)_
