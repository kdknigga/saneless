# Phase 08: Audit Lint and Type Checker Ignores - Research

**Researched:** 2026-03-21
**Domain:** Code quality -- ruff noqa, type: ignore, per-file-ignores, tool-level excludes
**Confidence:** HIGH

## Summary

This phase audits all inline suppressions (`# noqa`, `# type: ignore`), per-file-ignores in `pyproject.toml`, and tool-level excludes (ty/pyrefly test exclusions). The codebase has a small, well-defined set of suppressions: 7 inline `# noqa` directives, 2 `# type: ignore` comments in production code, 6 `# type: ignore` comments in test code, 3 per-file-ignore entries, and 2 tool-level test excludes (ty, pyrefly).

Investigation reveals that most suppressions are either fixable with better typing or legitimately required by the patterns in use. The httpx multipart typing issue is fixable. The pydantic `_toml_file` dynamic kwarg and the lazy import patterns are legitimately unfixable without architecture changes. Test `type: ignore` comments are out of scope (Phase 9).

**Primary recommendation:** Fix the httpx multipart typing, document all remaining suppressions with justification comments, and tighten per-file-ignores for tests to only what is actually needed.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- Audit each inline `# noqa` and `# type: ignore` individually
- Fix the underlying issue if possible (preferred)
- If legitimately unfixable (e.g., python-sane's C extension types, pydantic dynamic args), document WHY in a brief inline comment and keep the suppression
- Never add new suppressions to fix existing ones
- Keep test-inherent exemptions: S101 (assert), ANN (annotations), S104/S105/S106 (hardcoded bind/password in test fixtures)
- Audit and potentially remove: PLC0415 (import not at top), PLR0915/PLR0912/PLR0913 (complexity), T201 (print), TCH (type checking imports)
- Each removal must be validated -- if removing an exemption causes failures, fix the underlying code
- Keep tests excluded from ty and pyrefly for this phase (Phase 9 scope)
- Focus only on src/ type: ignore comments
- Verify that production code passes both ty and pyrefly clean after fixes
- Inline suppressions should be fixed with minimal targeted changes
- If fixing a suppression requires restructuring more than ~20 lines or changing public API, flag it rather than fix it
- The `__init__.py` lazy import pattern (PLC0415) is intentional -- document and keep

### Claude's Discretion
- Exact refactoring approach for each suppression
- Whether to use Protocol types, cast(), or restructuring for type: ignore fixes
- Order of fixes (can batch by file or by suppression type)

### Deferred Ideas (OUT OF SCOPE)
- Phase 9: Enable pytest strict mode and bring test files under the same linting/type-checking quality bar as production code
</user_constraints>

## Standard Stack

No new libraries needed. All tools are already configured and passing:

| Tool | Version | Purpose | Status |
|------|---------|---------|--------|
| ruff | >=0.15.5 | Linting + formatting | All checks pass |
| ty | >=0.0.21 | Type checking (src/) | All checks pass |
| pyrefly | >=0.55.0 | Type checking (src/) | 0 errors (1 suppressed) |

**Key finding:** pyrefly reports "1 suppressed" error -- this is the `# type: ignore[call-arg]` on `config.py:153` which pyrefly does not need (only ty needs it). pyrefly treats it as an unused suppression. This is harmless but worth noting.

## Architecture Patterns

### Current Suppression Inventory

#### Production Code -- Inline `# noqa` (5 occurrences, 3 unique sites)

| File | Line | Rule | Code Pattern | Fixable? |
|------|------|------|-------------|----------|
| `src/saneless/scanner/__init__.py` | 23 | PLC0415 | Lazy import in `__getattr__` for python-sane | NO -- intentional lazy loading to avoid C extension at import time |
| `src/saneless/scanner/sane_backend.py` | 45 | PLW0603 | `global sane` statement | NO -- required for lazy module-level sane import pattern |
| `src/saneless/scanner/sane_backend.py` | 47 | PLC0415 | `import sane as _sane` inside function | NO -- deferred import is the whole point |

#### Production Code -- Inline `# noqa` (2 occurrences in config.py)

| File | Line | Rule | Code Pattern | Fixable? |
|------|------|------|-------------|----------|
| `src/saneless/config.py` | 109 | ARG003 | Unused `dotenv_settings` param in `settings_customise_sources` | NO -- pydantic-settings requires this exact signature |
| `src/saneless/config.py` | 110 | ARG003 | Unused `file_secret_settings` param in same method | NO -- pydantic-settings requires this exact signature |

#### Production Code -- Inline `# type: ignore` (2 occurrences)

| File | Line | Checker | Code Pattern | Fixable? |
|------|------|---------|-------------|----------|
| `src/saneless/paperless.py` | 116 | `[arg-type]` (both ty and pyrefly) | `files=multipart_files` typed as `list[tuple[str, object]]` | **YES** -- fix type annotation to use httpx FileTypes |
| `src/saneless/config.py` | 153 | `[call-arg]` (ty only, unused in pyrefly) | `Settings(_toml_file=toml_file)` dynamic kwarg | NO -- ty cannot see that BaseSettings.__init__ accepts arbitrary kwargs via init_settings |

#### Per-File-Ignores in pyproject.toml (3 entries)

| Pattern | Rules | Purpose | Action |
|---------|-------|---------|--------|
| `tests/**/*.py` | S101, ANN, TCH, T201, PLC0415, PLR0915, PLR0912, PLR0913, S104, S105, S106 | Test exemptions | Audit: keep S101/ANN/S104/S105/S106 (test-inherent), evaluate removing others |
| `src/saneless/__init__.py` | PLC0415 | Lazy CLI import in `main()` | Keep -- intentional pattern |
| `src/saneless/config.py` | S104 | Bind-all-interfaces `0.0.0.0` default | Keep -- LAN app, documented decision |

#### Tool-Level Excludes (2 entries)

| Tool | Exclude | Purpose | Action |
|------|---------|---------|--------|
| `[tool.ty.src] exclude` | `tests/` | Tests not type-checked | Keep (Phase 9 scope) |
| `[tool.pyrefly] project_excludes` | `tests/` | Tests not type-checked | Keep (Phase 9 scope) |

### Pattern: Fixing the httpx Multipart Typing

The `paperless.py:116` suppression is fixable. The current code types `multipart_files` as `list[tuple[str, object]]` which is too broad. The fix is to use the proper httpx types.

Current (broken typing):
```python
multipart_files: list[tuple[str, object]] = [
    *fields,
    ("document", (pdf_path.name, f, "application/pdf")),
]
response = self._client.post(
    "/api/documents/post_document/",
    files=multipart_files,  # type: ignore[arg-type]
)
```

Fix approach -- use httpx's `RequestFiles` type alias or construct the sequence with proper typing:
```python
from httpx._types import FileTypes, RequestFiles

# Option A: Type the list properly
multipart_files: list[tuple[str, FileTypes]] = [
    *fields,  # list[tuple[str, str]] -- str is a valid FileTypes
    ("document", (pdf_path.name, f, "application/pdf")),
]
response = self._client.post(
    "/api/documents/post_document/",
    files=multipart_files,
)
```

**Important:** Verify that both ty and pyrefly accept `list[tuple[str, FileTypes]]` as assignable to the `files` parameter. The httpx `RequestFiles` type is `Sequence[tuple[str, FileTypes]] | Mapping[str, FileTypes]`, so a `list[tuple[str, FileTypes]]` should satisfy it.

**Alternative if private import is undesirable:** Use `Sequence` from httpx's public API or inline the type.

### Pattern: Documenting Justified Suppressions

Each remaining suppression should have a brief inline comment explaining WHY it exists:
```python
# Lazy import: python-sane C extension must not load at collection time
from .sane_backend import SaneBackend  # noqa: PLC0415

# pydantic-settings requires this exact signature; these params are unused
dotenv_settings: PydanticBaseSettingsSource,  # noqa: ARG003
```

### Anti-Patterns to Avoid
- **Adding `cast()` to avoid type: ignore when the underlying type IS correct** -- cast hides real errors. Only use when the type system genuinely cannot express the relationship.
- **Removing per-file-ignores without running ruff check** -- always validate that removal does not introduce new failures in test files.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| httpx multipart typing | Custom type aliases | `httpx._types.FileTypes` / `RequestFiles` | httpx already defines the exact union type needed |
| Listing suppressions | Manual grep | `ruff check --select` with specific rules | More reliable and catches edge cases |

## Common Pitfalls

### Pitfall 1: Removing Per-File-Ignores Without Validation
**What goes wrong:** Removing a test per-file-ignore causes dozens of new ruff errors in test files.
**Why it happens:** Test code legitimately uses patterns that production rules forbid (assert, no annotations, print debugging).
**How to avoid:** Before removing any per-file-ignore rule, run `ruff check --select RULE tests/` to see how many violations exist. If high count, the exemption is probably legitimate.
**Warning signs:** More than 5-10 violations when removing an exemption.

### Pitfall 2: Type Ignore Needed by One Checker But Not Another
**What goes wrong:** Removing `# type: ignore[call-arg]` from config.py breaks ty but not pyrefly. Pyrefly already reports it as "unused suppression."
**Why it happens:** ty and pyrefly have different type inference capabilities. ty cannot see pydantic's dynamic `__init__` kwargs; pyrefly can.
**How to avoid:** Test both checkers when modifying any `# type: ignore` comment. The comment must satisfy the stricter checker (ty).

### Pitfall 3: httpx Private Types Import
**What goes wrong:** Importing from `httpx._types` may break on httpx version updates.
**Why it happens:** `_types` is a private module.
**How to avoid:** Check if httpx re-exports `FileTypes` or `RequestFiles` from the public API. If not, the private import is acceptable since httpx's type stubs are stable and widely used. Pin httpx version range.

### Pitfall 4: Changing Suppression Scope
**What goes wrong:** Changing `# noqa: ARG003` to a broader `# noqa` accidentally suppresses other legitimate warnings on the same line.
**How to avoid:** Always use specific rule codes in noqa comments.

## Code Examples

### Checking Per-File-Ignore Impact
```bash
# Before removing TCH from test per-file-ignores:
uv run ruff check --select TCH tests/

# Before removing T201 from test per-file-ignores:
uv run ruff check --select T201 tests/

# Before removing PLC0415 from test per-file-ignores:
uv run ruff check --select PLC0415 tests/

# Before removing PLR0915,PLR0912,PLR0913 from test per-file-ignores:
uv run ruff check --select PLR0915,PLR0912,PLR0913 tests/
```

### Verifying All Checkers Pass After Changes
```bash
uv run ruff check .
uv run ty check
uv run pyrefly check src tests
uv run pytest -x
```

## State of the Art

No technology changes relevant. Ruff, ty, and pyrefly are all current versions.

## Open Questions

1. **httpx._types import stability**
   - What we know: `httpx._types.FileTypes` and `RequestFiles` exist and are correct types
   - What's unclear: Whether these are considered stable public API
   - Recommendation: Use the import -- httpx type stubs are widely used by the ecosystem and the types are fundamental. If concerned, a `TYPE_CHECKING` guard limits runtime exposure.

2. **Test per-file-ignores tightening scope**
   - What we know: Some test exemptions (PLC0415, PLR0915/0912/0913, T201, TCH) may be removable
   - What's unclear: How many violations each rule produces in tests
   - Recommendation: Run ruff check with each rule individually on tests/ and decide based on violation count

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest >=9.0.2 |
| Config file | pyproject.toml [tool.pytest.ini_options] |
| Quick run command | `uv run pytest -x` |
| Full suite command | `uv run pytest` |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| AUDIT-01 | All production noqa comments justified or removed | lint | `uv run ruff check src/` | N/A (lint check) |
| AUDIT-02 | All production type: ignore comments justified or removed | type-check | `uv run ty check && uv run pyrefly check src tests` | N/A (type check) |
| AUDIT-03 | Per-file-ignores tightened where possible | lint | `uv run ruff check .` | N/A (lint check) |
| AUDIT-04 | No regressions in test suite | unit | `uv run pytest -x` | tests/ (226 tests) |

### Sampling Rate
- **Per task commit:** `uv run ruff check . && uv run ty check && uv run pyrefly check src tests && uv run pytest -x`
- **Per wave merge:** `uv run prek run && uv run pytest`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
None -- existing test infrastructure covers all phase requirements. This phase modifies code quality tooling config and inline comments, validated by existing linters and test suite.

## Sources

### Primary (HIGH confidence)
- Direct codebase investigation: grep for all `# noqa` and `# type: ignore` comments
- Direct testing: removed each suppression and ran ty/pyrefly to verify what errors they suppress
- `pyproject.toml` -- ruff config, per-file-ignores, ty/pyrefly excludes
- `httpx._types` module -- verified `FileTypes` and `RequestFiles` type definitions at runtime

### Secondary (MEDIUM confidence)
- pydantic-settings `settings_customise_sources` signature requirements -- based on existing code comments and Phase 01 decision log

## Metadata

**Confidence breakdown:**
- Suppression inventory: HIGH -- exhaustive grep verified against codebase
- Fix strategies: HIGH -- tested each removal against both type checkers
- Per-file-ignore audit: MEDIUM -- need to run per-rule checks on tests/ during implementation
- httpx typing fix: HIGH -- verified types at runtime, approach is straightforward

**Research date:** 2026-03-21
**Valid until:** 2026-04-21 (stable domain, no moving targets)
