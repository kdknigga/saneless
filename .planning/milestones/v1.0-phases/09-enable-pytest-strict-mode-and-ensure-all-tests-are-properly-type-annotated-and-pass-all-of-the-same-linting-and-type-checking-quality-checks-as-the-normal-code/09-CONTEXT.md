# Phase 9: Enable Pytest Strict Mode and Test Quality Parity — Context

**Gathered:** 2026-03-21
**Status:** Ready for planning

<domain>
## Phase Boundary

Bring all test files under the same linting, type-checking, and annotation quality bar as production code. Enable pytest strict mode with all strictness settings. Remove all non-test-inherent per-file-ignores and tool-level test exclusions. After this phase, tests/ and src/ are held to identical standards except for S101 (assert) and S104/S105/S106 (hardcoded test values).

</domain>

<decisions>
## Implementation Decisions

### Type annotation scope
- Full ANN compliance on all test files — same bar as production code
- Remove the ANN exemption from per-file-ignores entirely
- Use precise types where possible (actual types, not MagicMock/Any)
- Fixtures return concrete types (e.g., StubScanner, not ScannerBackend)

### Docstrings
- Enforce D rules on tests — same as production
- Every test function, class, and module gets a docstring

### Exemption cleanup (bundle approach)
- Remove ALL non-test-inherent per-file-ignores for tests/ in one pass
- Keep only: S101 (assert), S104/S105/S106 (hardcoded test values)
- Remove: ANN, PLC0415, and any others that aren't test-inherent
- Each remaining justified suppression gets per-line `# noqa:` with comment

### Type checker fixes — test_browser.py
- Rename `_device_id` → `device_id` and `_device_id`/`_settings` → `device_id`/`settings` in `_BrowserTestScanner` to match parent ABC signatures
- No ARG suppression — use the actual parameter names with no workaround

### Type checker fixes — test_scanner.py
- Replace mock + reassignment pattern with concrete stub/fake classes (same pattern as _BrowserTestScanner)
- Eliminate all 6 `type: ignore` comments

### Type checker fixes — test_job.py
- Add `assert fetched is not None` narrowing before accessing attributes on potentially-None rows
- Idiomatic in tests (S101 already allowed)

### Type checker fixes — test_logging.py
- Add explicit `import logging.handlers` at top of file
- Resolves implicit-import warnings from pyrefly

### Type checker scope
- Remove `exclude = ["tests/"]` from `[tool.ty.src]` in pyproject.toml
- Remove `project_excludes = ["tests/"]` from `[tool.pyrefly]` in pyproject.toml
- Both checkers run on tests/ alongside src/ — same quality bar everywhere

### Pytest strict configuration
- Enable `strict_markers = true` — unregistered markers fail
- Enable `strict_config = true` — unknown ini keys fail
- Enable `filterwarnings = ["error"]` — unhandled warnings fail tests, no allowlisting
- Enable `xfail_strict = true` — xfail tests that pass unexpectedly fail the suite
- Add `addopts = ["-ra", "--strict-markers", "--strict-config"]` to pyproject.toml

### PLC0415 lazy imports
- Move most of the 77 lazy imports to top-level
- Claude evaluates each case-by-case: keep only those with genuine justification (side effects, monkeypatch-dependent, etc.)
- Remove blanket PLC0415 exemption from per-file-ignores
- Any remaining justified lazy imports get individual `# noqa: PLC0415` with explanatory comment

### Pre-commit hooks
- No hook config changes needed — ty/pyrefly hooks already use `pass_filenames: false`
- Removing tool-level excludes automatically makes hooks cover tests/

### Plan structure
- 3 plans:
  1. Fix type checker errors, remove ty/pyrefly exclusions, fix test_browser/test_job/test_logging/test_scanner issues (concrete stubs)
  2. Add annotations + docstrings, remove ANN/PLC0415/other exemptions, refactor lazy imports
  3. Pytest strict config + full verification pass

### Test regression gate
- Every plan must pass `uv run pytest` with all 226 tests green
- No temporary xfail markers — every test passes or gets fixed in the same plan
- No regressions allowed between plans

### Claude's Discretion
- Which lazy imports are justified vs. should move to top-level (case-by-case evaluation)
- Exact stub/fake class designs for test_scanner.py replacements
- Order of annotation additions within a plan
- Docstring content for test functions

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project quality standards
- `CLAUDE.md` — Zero-tolerance policy for suppressions, lists ruff/ty/pyrefly as required checks
- `pyproject.toml` — Ruff rule selection, per-file-ignores (lines 127-136), ty/pyrefly config (lines 137-141), pytest config (lines 143-147)

### Phase 8 context (predecessor)
- `.planning/phases/08-audit-lint-and-type-checker-ignores-and-noqas-and-fix-them/08-CONTEXT.md` — Suppression triage policy, fix-vs-restructure threshold, established that Phase 9 owns test quality

### Current test violations (audit targets)
- `tests/test_browser.py:56,64` — invalid-method-override (underscore param names)
- `tests/test_job.py:114,145` — missing-attribute (NoneType narrowing)
- `tests/test_logging.py:30,70,100` — implicit-import (logging.handlers)
- `tests/test_scanner.py:225,471,612,658,705,706` — type: ignore (mock assignments)

### Codebase testing patterns
- `.planning/codebase/TESTING.md` — Test framework, organization, fixture patterns, mocking conventions

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `_BrowserTestScanner` in test_browser.py: Concrete stub pattern already established — extend this pattern to test_scanner.py
- `StubScanner` in conftest.py: Existing concrete stub for scanner tests
- conftest.py: Shared fixtures already typed in some cases

### Established Patterns
- Concrete stub classes preferred over MagicMock (Phase 3, Phase 7 decisions)
- Assert narrowing for None checks (idiomatic in pytest with S101 allowed)
- Per-line noqa with justification comments (Phase 8 established this)

### Integration Points
- `pyproject.toml [tool.ruff.lint.per-file-ignores]` — exemption removal
- `pyproject.toml [tool.ty.src]` — exclusion removal
- `pyproject.toml [tool.pyrefly]` — exclusion removal
- `pyproject.toml [tool.pytest.ini_options]` — strict mode additions
- All 14 test files need annotation + docstring additions
- Pre-commit hooks automatically pick up scope changes

</code_context>

<specifics>
## Specific Ideas

- Phase 8 explicitly deferred test quality to Phase 9: "Test annotation coverage is Phase 9 scope"
- The per-file-ignores comment `# test annotation coverage is Phase 9 scope` on the ANN line is the direct trigger for this phase
- Filterwarnings strict with zero exceptions — if a dependency emits warnings, fix usage or open upstream issue
- Same approach as Phase 8: fix properly, don't add new suppressions

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope

</deferred>

---

*Phase: 09-enable-pytest-strict-mode-and-ensure-all-tests-are-properly-type-annotated-and-pass-all-of-the-same-linting-and-type-checking-quality-checks-as-the-normal-code*
*Context gathered: 2026-03-21*
