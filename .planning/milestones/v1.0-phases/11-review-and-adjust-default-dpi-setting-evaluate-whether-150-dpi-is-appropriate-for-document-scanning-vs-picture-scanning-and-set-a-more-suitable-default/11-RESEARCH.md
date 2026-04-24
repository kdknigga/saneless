# Phase 11: Review and Adjust Default DPI Setting - Research

**Researched:** 2026-03-21
**Domain:** Scanner DPI configuration for document OCR
**Confidence:** HIGH

## Summary

Research strongly validates that 300 DPI is the correct default for saneless. Tesseract OCR (used by paperless-ngx) officially recommends "at least 300 DPI" for optimal text recognition. Industry standards uniformly identify 300 DPI as the standard for professional document scanning, with 150 DPI being the absolute minimum acceptable and 200 DPI being "conventional but not recommended for archival." Going below 300 DPI degrades OCR accuracy, especially for smaller fonts. Going above 300 DPI provides marginal OCR improvement at significant file size and scan speed cost.

The current codebase already uses 300 DPI as the default, but the value is duplicated in two places: `ProfileConfig.resolution = 300` and `pick_closest_resolution(target=300)`. The CONTEXT.md decision to create a single `DEFAULT_RESOLUTION` constant is the right consolidation, regardless of whether the value changes.

**Primary recommendation:** Keep 300 DPI as the default. Consolidate the duplicated value into a single `DEFAULT_RESOLUTION` constant. Close this phase with a small refactor, not a DPI change.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- The default DPI value must be determined by research, not assumption
- Research must cover: OCR quality (Tesseract via paperless-ngx), file size / scan speed tradeoffs, visual readability, and industry document scanning standards
- saneless is primarily a document scanning tool (paperless-ngx is document management) -- optimize for documents, not photos
- Absolute floor: 150 DPI (will not go below this regardless of research findings)
- If research validates 300 DPI as optimal, close the phase with no code changes
- Same default DPI for all source types (Flatbed, ADF, etc.)
- No per-source DPI differentiation
- Define a single `DEFAULT_RESOLUTION` constant
- `ProfileConfig.resolution` default and `pick_closest_resolution` target both reference this constant
- Auto-generated profiles (Phase 10) follow the default -- no independent target
- Do NOT modify existing user TOML configs or auto-generated profiles on upgrade
- Only new auto-generated profiles use the updated default going forward
- Update the example `saneless.toml` in the repo to reflect the new default value
- No startup warnings, no re-generation prompts

### Claude's Discretion
- Where to define the `DEFAULT_RESOLUTION` constant (config.py module level, or separate constants module)
- Whether to add a brief code comment explaining the DPI rationale
- Test adjustments if DPI value changes

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope
</user_constraints>

## DPI Research Findings

### OCR Quality (Tesseract via paperless-ngx)

| DPI | OCR Quality | Source |
|-----|-------------|--------|
| 150 | Lowest acceptable; text becomes "muddy blur of pixels" with compression | Industry guides |
| 200 | Functional but not recommended for archival | Industry standard (conventional) |
| 300 | **Official Tesseract recommendation** ("at least 300 DPI") | Tesseract docs |
| 600 | Marginal OCR improvement, significant file size increase | Archival standards |

**Key finding:** Tesseract's official documentation states: "Tesseract works best on images which have a DPI of at least 300 dpi." This is the authoritative answer.

**Confidence:** HIGH -- direct from Tesseract official documentation.

### File Size and Scan Speed Tradeoffs

| DPI | Relative File Size | Scan Speed Impact |
|-----|-------------------|-------------------|
| 150 | ~1x baseline | Fastest |
| 300 | ~4x (quadratic scaling) | Standard |
| 600 | ~16x | Significantly slower |

File size scales with the square of DPI (doubling DPI quadruples pixels). However, paperless-ngx's archival compression "more than makes up for the increase in scanned image size" according to community discussion, so the 300 DPI file size penalty is not a practical concern for the saneless use case.

### Visual Readability

- 150 DPI: Acceptable for screen viewing of large text; poor for fine print or printing
- 200 DPI: Adequate for standard office documents on screen
- 300 DPI: Clear for all document types, suitable for printing and archival
- 600 DPI: Archival quality, no visible improvement over 300 for standard documents

### Industry Document Scanning Standards

| Standard | Recommended DPI | Context |
|----------|----------------|---------|
| General office | 300 DPI | Professional document scanning standard |
| NARA (US National Archives) | 300-400 DPI minimum | Government archival |
| True archival | 400-600 DPI | Original document preservation |
| Web/email only | 150-200 DPI | Non-archival, preview use |

**Conclusion: 300 DPI is validated as optimal for saneless's use case** (document scanning for paperless-ngx with Tesseract OCR).

## Standard Stack

No new libraries needed. This phase modifies existing Python constants only.

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| N/A | -- | No new dependencies | Pure constant refactoring |

## Architecture Patterns

### Recommended: Single Constant Pattern

The key architectural change is eliminating DPI value duplication:

**Current state (two separate 300 values):**
```python
# config.py
class ProfileConfig(BaseModel):
    resolution: int = 300  # hardcoded

# auto_profiles.py
resolution = pick_closest_resolution(capabilities.resolutions, target=300)  # hardcoded
```

**Target state (single constant):**
```python
# config.py (module level)
DEFAULT_RESOLUTION = 300
"""Default scan resolution in DPI. 300 DPI is the Tesseract OCR minimum recommendation."""

class ProfileConfig(BaseModel):
    resolution: int = DEFAULT_RESOLUTION

# auto_profiles.py
from saneless.config import DEFAULT_RESOLUTION

resolution = pick_closest_resolution(capabilities.resolutions, target=DEFAULT_RESOLUTION)
```

### Discretion: Constant Location

**Recommendation:** Define `DEFAULT_RESOLUTION` at module level in `config.py`.

**Rationale:**
- `config.py` already owns `ProfileConfig.resolution` -- the constant belongs with its primary consumer
- `auto_profiles.py` already imports from `config.py` (`from saneless.config import ProfileConfig, Settings`)
- A separate constants module would be overengineered for a single constant
- Module-level constant in `config.py` is the simplest, most discoverable location

### Discretion: Code Comment

**Recommendation:** Yes, add a brief docstring/comment explaining the DPI rationale. This is a researched decision that future developers should understand without re-researching.

### Anti-Patterns to Avoid
- **Don't change tests just to prove work was done:** If the value stays 300, tests should stay at 300. Only update tests if the constant import path changes or tests were hardcoding values that should reference the constant.
- **Don't add configuration for the default DPI target:** The whole point is a researched, opinionated default -- not another knob.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| DPI research | Custom DPI analysis tool | This research document | Research is one-time; codify the conclusion as a constant |

## Common Pitfalls

### Pitfall 1: Circular Import
**What goes wrong:** `auto_profiles.py` imports `DEFAULT_RESOLUTION` from `config.py`, which already works (it imports `ProfileConfig` and `Settings`), but adding the constant could cause issues if placed in a wrong module.
**How to avoid:** Place constant in `config.py` at module level, before class definitions. `auto_profiles.py` already imports from `config.py` so no circular risk.

### Pitfall 2: Test Fragility from Magic Numbers
**What goes wrong:** Tests hardcode `resolution=300` throughout. If the default ever changes in the future, many tests break.
**How to avoid:** Tests that test specific scanner behavior with an explicit resolution should keep their hardcoded values (they're testing "scan at 300 DPI works," not "scan at default DPI works"). Only tests that specifically test default behavior should reference the constant.
**Warning signs:** A test that asserts `profile.resolution == 300` when it's really testing "default profile has default resolution" -- that test should use the constant.

### Pitfall 3: Forgetting Example Config
**What goes wrong:** `saneless.toml.example` and `saneless.toml` still show old value if DPI changes.
**How to avoid:** The CONTEXT.md explicitly requires updating `saneless.toml` example. Also update `saneless.toml.example`, `docs/PRD.md`, and `README.md` if any contain hardcoded DPI values.

## Code Examples

### Constant Definition (config.py)
```python
# At module level, before class definitions
DEFAULT_RESOLUTION = 300
"""Default scan resolution in DPI.

300 DPI is the minimum recommended by Tesseract OCR and the industry
standard for professional document scanning. See Phase 11 research.
"""
```

### Auto-profiles Update (auto_profiles.py)
```python
from saneless.config import DEFAULT_RESOLUTION, ProfileConfig, Settings

def pick_closest_resolution(
    resolutions: list[int],
    target: int = DEFAULT_RESOLUTION,
) -> int:
    ...

def generate_profiles(capabilities: DeviceCapabilities) -> dict[str, ProfileConfig]:
    resolution = pick_closest_resolution(capabilities.resolutions, target=DEFAULT_RESOLUTION)
    ...
```

### ProfileConfig Update (config.py)
```python
class ProfileConfig(BaseModel):
    resolution: int = DEFAULT_RESOLUTION
```

## Code Change Inventory

Files that need modification (if consolidating to constant):

| File | Change | Type |
|------|--------|------|
| `src/saneless/config.py` | Add `DEFAULT_RESOLUTION = 300`, update `ProfileConfig.resolution` default | Constant + field default |
| `src/saneless/auto_profiles.py` | Import `DEFAULT_RESOLUTION`, replace hardcoded `target=300` | Import + parameter default |
| `saneless.toml` | Verify `resolution = 300` (no change needed if staying 300) | Example config |
| `saneless.toml.example` | Verify `resolution = 300` (no change needed if staying 300) | Example config |

Files that reference `resolution = 300` in tests (30+ occurrences) -- these are explicit test values, NOT defaults, and should NOT be changed to use the constant. They test specific behavior at specific DPI.

Files that MAY warrant updating to use the constant:
| File | Line | Current | Recommendation |
|------|------|---------|----------------|
| `tests/conftest.py` | 49 | `resolution = 300` in sample TOML | Leave as-is (explicit test fixture) |
| `tests/test_auto_profiles.py` | 61,65,70 | `target=300` in pick_closest_resolution tests | Update to use `DEFAULT_RESOLUTION` for tests that validate default behavior |

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| 150 DPI for speed | 300 DPI minimum for OCR | Tesseract 4.x era (~2018+) | OCR quality baseline established |
| Per-use-case DPI | 300 DPI universal standard | Industry consensus | Simplifies defaults |

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest (latest via uv) |
| Config file | `pyproject.toml` [tool.pytest.ini_options] |
| Quick run command | `uv run pytest tests/test_auto_profiles.py tests/test_config.py -x -q` |
| Full suite command | `uv run pytest -x -q` |

### Phase Requirements to Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| (none) | DEFAULT_RESOLUTION constant imported correctly | unit | `uv run pytest tests/test_config.py -x -q` | Needs update |
| (none) | auto_profiles uses DEFAULT_RESOLUTION | unit | `uv run pytest tests/test_auto_profiles.py -x -q` | Needs update |
| (none) | ProfileConfig default matches constant | unit | `uv run pytest tests/test_config.py -x -q` | Needs update |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_auto_profiles.py tests/test_config.py -x -q`
- **Per wave merge:** `uv run pytest -x -q`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
None -- existing test infrastructure covers all phase requirements. Only minor test assertions may need updating to reference the new constant.

## Open Questions

1. **Should tests reference DEFAULT_RESOLUTION or hardcode 300?**
   - What we know: Most test `resolution=300` values are explicit test parameters, not default-testing
   - What's unclear: Which specific tests are testing "default behavior" vs "behavior at 300 DPI"
   - Recommendation: Only update tests in `test_auto_profiles.py` that explicitly test default target behavior. Leave all scanner/web/CLI test fixtures hardcoded.

## Sources

### Primary (HIGH confidence)
- [Tesseract OCR ImproveQuality docs](https://tesseract-ocr.github.io/tessdoc/ImproveQuality.html) -- "at least 300 DPI" recommendation
- [paperless-ngx Discussion #5239](https://github.com/paperless-ngx/paperless-ngx/discussions/5239) -- community DPI practices

### Secondary (MEDIUM confidence)
- [SecureScan Resolution Guide](https://www.securescan.com/articles/document-scanning/resolution-matters-your-guide-to-scanning-resolution/) -- industry standards overview
- [MES Ltd DPI Guide](https://blog.mesltd.ca/what-is-the-best-dpi-for-scanning-documents/) -- 300 DPI professional standard
- [NARA Guidelines](https://www.archives.gov/files/preservation/technical/guidelines-1998.pdf) -- US government archival standards

### Tertiary (LOW confidence)
- Single paperless-ngx discussion participant recommending 150 DPI for B/W -- not representative of broader community consensus

## Metadata

**Confidence breakdown:**
- DPI recommendation: HIGH -- Tesseract official docs + industry consensus
- Architecture (constant pattern): HIGH -- straightforward Python refactor
- Pitfalls: HIGH -- well-understood codebase with clear change inventory

**Research date:** 2026-03-21
**Valid until:** Indefinite -- DPI standards and Tesseract recommendations are stable
