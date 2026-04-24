# Phase 08 — UI Review

**Audited:** 2026-03-22
**Baseline:** Abstract 6-pillar standards (no UI-SPEC.md; no frontend component)
**Screenshots:** Not captured — this phase has no frontend UI. Saneless is a Python CLI/API backend. The audit covers code quality, developer-facing interface, and tooling configuration.

> **Scope note:** Phase 08 is a pure code-quality phase: auditing and tightening lint suppressions, type-checker ignores, and per-file-ignores. There is no UI to render. The 6-pillar framework is applied to the developer-facing surfaces: API contracts, CLI strings, error messages, code structure legibility, configuration consistency, and error handling coverage.

---

## Pillar Scores

| Pillar | Score | Key Finding |
|--------|-------|-------------|
| 1. Copywriting | 4/4 | Inline justification comments are precise and informative; error messages are accurate |
| 2. Visuals | 3/4 | Code structure is clear; 3 of 6 src/ suppressions lack inline justification despite plan requirement |
| 3. Color | 4/4 | No hardcoded color; no frontend surface; not applicable — rated as structural consistency pass |
| 4. Typography | 4/4 | Docstrings are complete, consistent, and follow the required style throughout modified files |
| 5. Spacing | 4/4 | Per-file-ignores tightened from 10 rules to 6; all spacing in config is properly commented |
| 6. Experience Design | 3/4 | All 226 tests pass; one unresolved inline noqa in test code lacks justification |

**Overall: 22/24**

---

## Top 3 Priority Fixes

1. **3 src/ noqa comments lack inline justification** — Violates the phase's stated plan requirement ("Every inline # noqa in src/ has a justification comment explaining WHY it exists"). Inline comments on the line itself are more discoverable than relying on a nearby block comment. Add `-- <reason>` suffix to lines `sane_backend.py:45`, `sane_backend.py:47`, and `scanner/__init__.py:23`.

2. **test_cli.py:538 noqa lacks justification comment** — `# noqa: PLC0415` is used in test code with no inline explanation. PLC0415 was intentionally removed from the test per-file-ignores, so this inline suppression stands alone. Without a justification comment it looks like an oversight rather than a deliberate decision. Add `-- lazy import required inside closure; cannot move to module top-level`.

3. **PLW0603 suppression (global sane) has no inline justification despite being a globally-mutable sentinel** — This is the highest-risk suppression in the codebase (module-level global reassignment). The surrounding block comment (lines 36-39) is good documentation but the plan explicitly required inline justification on the suppressed line. An inline comment would also survive future refactors that move the block comment. Change line 45 to: `global sane  # noqa: PLW0603 -- module-level sentinel for monkeypatch; tests patch sane_backend.sane directly`.

---

## Detailed Findings

### Pillar 1: Copywriting (4/4)

All justification comments that were added are precise and informative. Examples:

- `config.py:118` — `# noqa: ARG003 -- pydantic-settings requires this signature param` — clearly names the constraint and its source.
- `config.py:119` — same pattern, consistent repetition.
- `config.py:162` — `# type: ignore[call-arg] -- ty cannot see BaseSettings dynamic __init__ kwargs` — names both the tool and the root cause (dynamic `__init__`).

Error messages in production code continue to use specific, actionable language (e.g., `"Paperless rejected upload: {status_code} {text}"`, `"Device does not support source '{source}'. Available: {available}"`). No generic "Something went wrong" patterns found.

### Pillar 2: Visuals (3/4)

"Visual" is interpreted here as code legibility and inline documentation consistency — the developer-facing analogue of visual hierarchy.

**Passed:**
- `config.py` suppressions all carry inline justification on the same line as required.
- `paperless.py` has no suppressions after the `type: ignore[arg-type]` removal; the `FileTypes` import under `TYPE_CHECKING` is clean and idiomatic.
- All modified files have module-level docstrings and complete class/method docstrings.

**Failed (3 suppressions with block-comment-only justification):**

`src/saneless/scanner/__init__.py:23`:
```python
from .sane_backend import SaneBackend  # noqa: PLC0415
```
The docstring of `__getattr__` (line 21) explains the lazy import pattern but that is one level of indirection. The plan required the suppression line itself to carry a comment.

`src/saneless/scanner/sane_backend.py:45`:
```python
global sane  # noqa: PLW0603
```
The surrounding block comment at lines 36-39 explains the monkeypatching rationale. However, an inline comment would make the justification immediately visible without requiring context scrolling.

`src/saneless/scanner/sane_backend.py:47`:
```python
import sane as _sane  # noqa: PLC0415
```
Same block-comment dependency as line 45.

The SUMMARY.md acknowledges these were retained with surrounding context rather than inline comments. For a phase whose stated requirement was "Every inline # noqa in src/ has a justification comment explaining WHY it exists", this is a partial miss. The plan acceptance criteria at `08-01-PLAN.md` lines 165-167 lists specific per-suppression checks for config.py but did not include scanner/*.py checks with the same specificity, which explains the gap.

### Pillar 3: Color (4/4)

Not applicable as a visual metric — this is a Python backend with no CSS, no Tailwind, no frontend. Structural consistency is used as a proxy:

- `pyproject.toml` color coding (TOML inline comments) is consistent throughout the per-file-ignores section.
- Rule selection in `[tool.ruff.lint]` is consistent with no stray rules.
- No hardcoded magic values introduced in this phase; `DEFAULT_RESOLUTION = 300` from Phase 11 is properly named and documented.

Rated 4/4: the configuration file is internally consistent.

### Pillar 4: Typography (4/4)

Interpreted as docstring quality and style consistency across modified files.

All six modified files have:
- Module-level docstring
- Class docstrings where classes exist
- Method/function docstrings following the Args/Returns/Raises/Yields conventions required by the D-rule set

Spot-checked files against ruff D rule requirements:
- `paperless.py`: Complete docstrings at module, class, and every public method level. Args/Returns/Raises sections present.
- `config.py`: All public classes (ScannerConfig, PaperlessConfig, ProfileConfig, OutputConfig, Settings) and all public functions have docstrings.
- `sane_backend.py`: `SaneDevice` Protocol, `SaneBackend`, and all methods documented. Private helpers (`_as_image`, `_is_adf_source`, `_validate_page_image`) also have docstrings.

No D-rule violations are expected given the phase verified ruff passes clean.

### Pillar 5: Spacing (4/4)

Interpreted as configuration structure and rule-set discipline.

The tightening of `per-file-ignores` from 10 rules to 6 represents the core deliverable:

**Before Phase 08 (inferred from context):**
```toml
"tests/**/*.py" = [
    "S101", "ARG", "S104", "S105", "S106",
    "TCH", "T201", "PLR0915", "PLR0912", "PLR0913", "PLC0415"
]
```

**After Phase 08:**
```toml
"tests/**/*.py" = [
    "S101",   # assert is the test mechanism
    "ARG",    # unused args in ABC stubs and fixture overrides
    "S104", "S105", "S106",  # hardcoded test values are inherent to fixtures
]
```

Each retained rule has a trailing comment explaining why. Each removed rule (TCH, T201, PLR0915, PLR0912, PLR0913) was validated to have zero violations before removal. TCH removal surfaced 3 TC003 violations in test files that were properly fixed (Iterator imports moved to TYPE_CHECKING blocks) rather than re-suppressed. This is exemplary suppression hygiene.

The per-src-file exceptions are also tight:
- `src/saneless/__init__.py = ["PLC0415"]` — one rule, justified by lazy load pattern
- `src/saneless/config.py = ["S104"]` — one rule, justified by test-like TOML value handling

### Pillar 6: Experience Design (3/4)

Interpreted as test coverage completeness, error handling, and edge case coverage for modified code paths.

**Passed:**
- 226 tests pass after all changes — zero regression.
- The `FileTypes` typing change in `paperless.py` is verified by the existing `test_paperless.py` suite.
- TCH fix (Iterator into TYPE_CHECKING blocks) was verified by re-running ruff and pytest.
- The one auto-fixed deviation (TC003 violations surfaced during per-file-ignores tightening) was handled correctly: underlying code fixed rather than suppression re-added.

**Gap:**
- `test_cli.py:538` carries `# noqa: PLC0415` without a justification comment. Since PLC0415 is intentionally absent from test per-file-ignores (removed in this phase as having 77 violations that are "test-inherent"), a single inline suppression in test_cli.py now looks inconsistent — it implies PLC0415 was removed from per-file-ignores but one test still has a legitimate in-function import. The noqa itself is correct; it just needs a comment explaining why the import must be inside the closure rather than at module top level.

- The SUMMARY.md's Remaining Suppressions Inventory lists only 6 src/ suppressions but does not account for this test-file inline suppression. This is a minor documentation gap.

---

## Files Audited

- `/home/kris/git/saneless/src/saneless/paperless.py`
- `/home/kris/git/saneless/src/saneless/config.py`
- `/home/kris/git/saneless/src/saneless/scanner/__init__.py`
- `/home/kris/git/saneless/src/saneless/scanner/sane_backend.py`
- `/home/kris/git/saneless/pyproject.toml`
- `/home/kris/git/saneless/tests/test_cli.py` (lines 530-544)
- `/home/kris/git/saneless/.planning/phases/08-audit-lint-and-type-checker-ignores-and-noqas-and-fix-them/08-01-SUMMARY.md`
- `/home/kris/git/saneless/.planning/phases/08-audit-lint-and-type-checker-ignores-and-noqas-and-fix-them/08-01-PLAN.md`
- `/home/kris/git/saneless/.planning/phases/08-audit-lint-and-type-checker-ignores-and-noqas-and-fix-them/08-CONTEXT.md`

Registry audit: Not applicable — no `components.json` (not a shadcn/frontend project).
