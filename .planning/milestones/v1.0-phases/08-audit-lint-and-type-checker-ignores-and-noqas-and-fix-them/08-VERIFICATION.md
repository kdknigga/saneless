---
phase: 08-audit-lint-and-type-checker-ignores-and-noqas-and-fix-them
verified: 2026-03-21T20:30:00Z
status: passed
score: 6/6 must-haves verified
re_verification: false
---

# Phase 08: Audit Lint and Type Checker Ignores Verification Report

**Phase Goal:** Audit every inline suppression (# noqa, # type: ignore), per-file-ignores in pyproject.toml, and tool-level excludes. Fix suppressions where possible; document any legitimately unfixable ones. Zero unjustified suppressions in production code.
**Verified:** 2026-03-21T20:30:00Z
**Status:** PASSED
**Re-verification:** No -- initial verification

## Goal Achievement

### Observable Truths

| #  | Truth | Status | Evidence |
|----|-------|--------|----------|
| 1  | Every inline # noqa in src/ has a justification comment explaining WHY it exists | VERIFIED | All 5 noqa instances carry inline -- comments: PLC0415 (lazy import/deferred C ext), PLW0603 (monkeypatching), ARG003 x2 (pydantic-settings signature) |
| 2  | The httpx multipart type: ignore in paperless.py is removed and replaced with proper typing | VERIFIED | `type: ignore[arg-type]` absent; `FileTypes` imported under TYPE_CHECKING guard; `multipart_files: list[tuple[str, FileTypes]]` on line 114 |
| 3  | The config.py type: ignore[call-arg] has a justification comment explaining WHY it is needed | VERIFIED | Line 153: `# type: ignore[call-arg] -- ty cannot see BaseSettings dynamic __init__ kwargs` |
| 4  | Per-file-ignores for tests are tightened to only rules that actually produce violations | VERIFIED | Reduced from 10 rules to 6; TCH, T201, PLR0915, PLR0912, PLR0913 removed; each retained rule has TOML comment |
| 5  | All linters and type checkers pass clean: ruff check, ty check, pyrefly check src tests | VERIFIED | `uv run ruff check .` exits 0; `uv run ty check` exits 0; `uv run pyrefly check src tests` exits 0 (0 errors) |
| 6  | All 226+ tests pass without regression | VERIFIED | `uv run pytest -x -q`: 226 passed in 24.17s |

**Score:** 6/6 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/saneless/paperless.py` | Fixed httpx multipart typing without type: ignore | VERIFIED | Contains `FileTypes` via TYPE_CHECKING guard; no type: ignore on post call |
| `src/saneless/config.py` | Documented justification for remaining suppressions | VERIFIED | Lines 109-110: ARG003 with pydantic justification; line 153: call-arg with ty limitation justification |
| `src/saneless/scanner/sane_backend.py` | Documented justification for lazy import suppressions | VERIFIED | Lines 45+47: PLW0603 and PLC0415; context block at lines 36-39 documents the monkeypatching pattern |
| `pyproject.toml` | Tightened per-file-ignores | VERIFIED | `tests/**/*.py` section: 6 rules with inline TOML comments; `src/saneless/__init__.py` PLC0415 retained; `src/saneless/config.py` S104 retained |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `src/saneless/paperless.py` | `httpx._types` | `FileTypes` import under TYPE_CHECKING | WIRED | `from httpx._types import FileTypes` on line 23; used in annotation on line 114 |
| `pyproject.toml` | `tests/**/*.py` | per-file-ignores rules | WIRED | `[tool.ruff.lint.per-file-ignores]` section present; `tests/**/*.py` key maps 6 rules |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|----------|
| AUDIT-01 | 08-01-PLAN.md | Zero unjustified suppressions in production code | SATISFIED | All 6 src/ suppressions carry justification comments; no bare noqa or type: ignore found |
| AUDIT-02 | 08-01-PLAN.md | httpx multipart type: ignore replaced with proper typing | SATISFIED | `FileTypes` annotation used; `type: ignore[arg-type]` absent from paperless.py |
| AUDIT-03 | 08-01-PLAN.md | Per-file-ignores tightened to only justified rules | SATISFIED | 4 rules removed (0 violations); 1 rule fixed by moving imports to TYPE_CHECKING blocks |
| AUDIT-04 | 08-01-PLAN.md | All linters, type checkers, and tests pass clean | SATISFIED | ruff: 0 errors; ty: 0 errors; pyrefly: 0 errors; pytest: 226/226 passed |

**Note on requirement IDs:** AUDIT-01 through AUDIT-04 are internal phase requirements referenced in the plan frontmatter. They do not appear in `.planning/REQUIREMENTS.md` (which tracks user-facing functional requirements). This is expected -- code quality audits are internal engineering concerns, not user-facing requirements.

### Anti-Patterns Found

No blocking or warning anti-patterns found in the modified files.

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `tests/test_scanner.py` | 225, 471, 612, 658, 705, 706 | `# type: ignore[abstract]` and `# type: ignore[assignment]` | Info | Test-only suppressions; ty excludes tests/ via `[tool.ty.src] exclude`; these are test-inherent patterns and out of scope for this phase |

### Human Verification Required

None. All observable truths are verifiable programmatically and confirmed.

### Gaps Summary

No gaps. All 6 must-have truths verified against the actual codebase.

Key outcomes confirmed:
1. `src/saneless/paperless.py` contains no `type: ignore` comments of any kind.
2. All 5 `# noqa` instances in `src/` carry `--` inline justification matching the PLAN's acceptance criteria text exactly.
3. The 1 remaining `# type: ignore` in `src/` (config.py line 153) carries the justification text `ty cannot see BaseSettings dynamic __init__ kwargs`.
4. `pyproject.toml` per-file-ignores for tests reduced from 10 to 6 rules; each rule commented.
5. Test files moved `Iterator` imports into `TYPE_CHECKING` blocks to fix the 3 TC003 violations that were revealed by removing `TCH` from per-file-ignores.
6. Commits `fb3beda` and `d7d6c88` confirmed to exist and contain expected file changes.

---
_Verified: 2026-03-21T20:30:00Z_
_Verifier: Claude (gsd-verifier)_
