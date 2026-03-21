# Phase 8: Audit Lint and Type Checker Ignores — Context

**Gathered:** 2026-03-21
**Status:** Ready for planning
**Source:** Auto-selected defaults

<domain>
## Phase Boundary

Audit every inline suppression (`# noqa`, `# type: ignore`), per-file-ignores in pyproject.toml, and tool-level excludes (ty/pyrefly test exclusions). Fix suppressions where possible; document any that are legitimately unfixable. Goal: zero unjustified suppressions in production code.

Test files are out of scope for ty/pyrefly coverage (that's Phase 9). Test-inherent ruff exemptions (S101, ANN) stay.

</domain>

<decisions>
## Implementation Decisions

### Suppression triage policy
- Audit each inline `# noqa` and `# type: ignore` individually
- Fix the underlying issue if possible (preferred)
- If legitimately unfixable (e.g., python-sane's C extension types, pydantic dynamic args), document WHY in a brief inline comment and keep the suppression
- Never add new suppressions to fix existing ones

### Per-file-ignores scope
- Keep test-inherent exemptions: S101 (assert), ANN (annotations), S104/S105/S106 (hardcoded bind/password in test fixtures)
- Audit and potentially remove: PLC0415 (import not at top), PLR0915/PLR0912/PLR0913 (complexity), T201 (print), TCH (type checking imports)
- Each removal must be validated — if removing an exemption causes failures, fix the underlying code

### Type checker coverage
- Keep tests excluded from ty and pyrefly for this phase (Phase 9 scope)
- Focus only on src/ type: ignore comments
- Verify that production code passes both ty and pyrefly clean after fixes

### Fix vs. restructure threshold
- Inline suppressions should be fixed with minimal targeted changes
- If fixing a suppression requires restructuring more than ~20 lines or changing public API, flag it rather than fix it
- The `__init__.py` lazy import pattern (PLC0415) is intentional — document and keep

### Claude's Discretion
- Exact refactoring approach for each suppression
- Whether to use Protocol types, cast(), or restructuring for type: ignore fixes
- Order of fixes (can batch by file or by suppression type)

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project quality standards
- `CLAUDE.md` — Defines zero-tolerance policy for suppressions, lists ruff/ty/pyrefly as required checks
- `pyproject.toml` — Ruff rule selection, per-file-ignores, ty/pyrefly config

### Current suppressions (audit targets)
- `src/saneless/scanner/__init__.py:23` — noqa: PLC0415 (lazy import)
- `src/saneless/scanner/sane_backend.py:45,47` — noqa: PLW0603, PLC0415 (global sane module)
- `src/saneless/config.py:109,110` — noqa: ARG003 (unused pydantic settings args)
- `src/saneless/config.py:153` — type: ignore[call-arg] (pydantic dynamic)
- `src/saneless/paperless.py:116` — type: ignore[arg-type] (httpx multipart)
- `tests/test_scanner.py:220,466,607,653,700,701` — type: ignore (mock assignments)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- Ruff is already configured with extensive rule set — no new tooling needed
- ty and pyrefly both configured and passing on src/

### Established Patterns
- Lazy import pattern for python-sane (C extension) — PLC0415 suppressions are intentional
- Pydantic settings_customise_sources uses unused args by signature contract — ARG003 legitimate
- Mock object attribute assignment in tests — type: ignore may be unavoidable without Protocol stubs

### Integration Points
- All changes must pass: `uv run ruff check .`, `uv run ty check`, `uv run pyrefly check`, `uv run prek run`
- 226 tests must continue passing after any changes

</code_context>

<specifics>
## Specific Ideas

- From CLAUDE.md: "Fix reported issues properly. Do not suppress errors with `# type: ignore`, `# noqa`, or by disabling rules."
- Each remaining suppression after audit should have a comment explaining why it's necessary

</specifics>

<deferred>
## Deferred Ideas

- Phase 9: Enable pytest strict mode and bring test files under the same linting/type-checking quality bar as production code

</deferred>

---

*Phase: 08-audit-lint-and-type-checker-ignores-and-noqas-and-fix-them*
*Context gathered: 2026-03-21 via auto-selected defaults*
