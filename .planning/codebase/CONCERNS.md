# Codebase Concerns

**Analysis Date:** 2026-03-20

## Project Maturity

**Early-stage project with minimal implementation:**
- Issue: Project is just initialized with only a stub `main()` function in `src/saneless/__init__.py`
- Files: `src/saneless/__init__.py`
- Impact: No core functionality exists yet; entire feature set needs to be built
- Fix approach: Follow GSD patterns to build features in phases

## Missing Critical Documentation

**Empty or minimal README:**
- Issue: `README.md` file is empty (0 content lines)
- Files: `README.md`
- Impact: Users cannot understand project purpose, installation, or usage
- Fix approach: Write comprehensive README with project overview, requirements, installation steps, and basic examples

## Missing Project Description

**Placeholder project metadata:**
- Issue: `pyproject.toml` has "Add your description here" as the project description
- Files: `pyproject.toml` (line 4)
- Impact: Package metadata is incomplete; unclear what saneless does
- Fix approach: Write clear, concise project description explaining the tool's purpose

## No Test Coverage

**Test framework configured but no tests implemented:**
- Issue: `pytest` is configured in `pyproject.toml` (dependency-groups.dev, line 110) but no tests directory or test files exist
- Files: `pyproject.toml` (lines 109-110)
- Impact: Code changes have zero safety net; bugs cannot be caught early; regressions are undetectable
- Fix approach: Create `tests/` directory and establish testing patterns before adding features; add unit tests for each function
- Priority: **High** - must establish before significant feature development

## No Implementation Code

**Missing core module structure:**
- Issue: Only `src/saneless/__init__.py` exists with a single stub function; no actual implementation
- Files: `src/saneless/`
- Impact: Cannot evaluate architecture, code organization, or dependency usage patterns
- Fix approach: Build feature modules following architecture patterns defined in ARCHITECTURE.md

## Missing Type Annotations

**Type checking configured but no annotations present:**
- Issue: `pyproject.toml` includes `ty` (line 116) and `pyrefly` (line 116) type checkers, and `.pre-commit-config.yaml` runs them (lines 48-62), but the only code file has no type annotations
- Files: `src/saneless/__init__.py`
- Impact: Type checker configuration is wasted; type safety benefits not realized
- Fix approach: Add return type annotation to `main()` function; follow typing conventions in all new code; consider using `from __future__ import annotations`

## Overly Strict Linting Configuration

**Ruff configured with too many enabled rules:**
- Issue: `pyproject.toml` enables 30+ Ruff rules (line 61: E, F, B, G, W, YTT, ANN, ASYNC, S, FBT, A, COM, C4, EM, ICN, LOG, PIE, Q, RSE, RET, SIM, TID, TCH, ARG, PTH, FLY, PERF, FURB, RUF, UP, D, I, PL, PT, DTZ, T20) with very few ignores
- Files: `pyproject.toml` (lines 57-62)
- Impact: Type annotations required (ANN), docstrings for everything (D), log message formatting (LOG), etc. will slow development and cause friction
- Fix approach: Reduce rule set to essentials (E, F, W, I) and gradually add rules; review ignore list (line 62) and selectively disable overly aggressive rules
- Priority: **Medium** - consider disabling before major development phase to reduce friction

## Unresolved Module Path Issues

**Naming mismatch in pyproject.toml:**
- Issue: `pyproject.toml` line 100-101 references `src/scanless/` but actual code is in `src/saneless/` (note: "scanless" vs "saneless")
- Files: `pyproject.toml` (lines 100-101)
- Impact: Per-file linting exemptions may not apply correctly; future developers will be confused
- Fix approach: Update per-file-ignores to use correct module paths: `src/saneless/__init__.py` and `src/saneless/app.py` (if app.py exists)

## Pre-commit Hook Brittleness

**Type checkers always run on all Python files:**
- Issue: `.pre-commit-config.yaml` (lines 48-62) runs `ty check` and `pyrefly check src tests` with `pass_filenames=false` and `always_run=true`
- Files: `.pre-commit-config.yaml` (lines 46-62)
- Impact: Every commit runs full type checks even for trivial changes; commits will fail if any file has type issues; developers may bypass hooks with `--no-verify`
- Fix approach: Consider setting `pass_filenames=true` for better performance; add safety to catch type regressions before they happen
- Priority: **Medium** - document to developers that `--no-verify` should not be used

## Python Version Constraint Unclear

**Very new Python version requirement:**
- Issue: `pyproject.toml` line 9 requires `>=3.14` which is extremely new (future Python version)
- Files: `pyproject.toml` (line 9)
- Impact: May be unrealistic constraint; limits who can use the package; target-version is also 3.14 (line 55)
- Fix approach: Clarify if this is intentional (using latest features) or should be adjusted to `>=3.11` or `>=3.12` for wider compatibility

## Missing Executable Declaration

**Command entry point without implementation:**
- Issue: `pyproject.toml` line 13 defines `saneless = "saneless:main"` script entry but only prints "Hello from saneless!"
- Files: `src/saneless/__init__.py`, `pyproject.toml` (line 13)
- Impact: Executable is not useful; unclear what the tool should do
- Fix approach: Implement actual command-line interface with argument parsing once functionality is defined

## Incomplete Dependency Management

**No production dependencies declared:**
- Issue: `pyproject.toml` line 10 has `dependencies = []` - empty production dependency list
- Files: `pyproject.toml` (line 10)
- Impact: Unclear what external libraries will be needed; may indicate incomplete design
- Fix approach: Add dependencies as features are implemented; document dependency rationale

## Security Scanning in Place but Early Stage

**Good: AWS credential detection enabled:**
- Positive: `.pre-commit-config.yaml` includes `detect-aws-credentials` and `detect-private-key` (lines 19-20)
- Status: Appropriate for security posture; ensure developers understand they should not commit credentials

## Development Dependency on Playwright

**Playwright included in dev dependencies:**
- Observation: `pyproject.toml` includes `playwright>=1.58.0` (line 114) suggesting browser automation planned
- Files: `pyproject.toml` (line 114)
- Impact: Development environment includes heavy dependency; unclear if needed for current stage
- Note: Examine actual usage patterns once implementation code exists

---

*Concerns audit: 2026-03-20*
