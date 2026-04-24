# Phase 16: Configurable Auto Source Mode - Research

**Researched:** 2026-03-22
**Domain:** SANE scanner source routing, Pydantic config extension, dataclass extension
**Confidence:** HIGH

## Summary

This phase adds a single configurable field (`auto_source_mode`) that controls whether a scanner's "Auto" source routes to flatbed (snap) or ADF (multi_scan) scan paths. The change touches four files in a straight line: `ProfileConfig` (config model) -> `ScanSettings` (dataclass) -> `run_pipeline()` (construction) -> `scan_pages()` (routing). A fifth file (`auto_profiles.py`) needs `source_to_slug("Auto")` support and smart defaulting of `auto_source_mode` in generated profiles.

The existing code is well-structured for this extension. `ScanSettings` is a plain dataclass, `ProfileConfig` is a Pydantic BaseModel, and the routing logic in `scan_pages()` has a clear decision point at line 424 where `use_adf = _is_adf_source(effective_source)`. The change inserts a conditional override at exactly that point when `effective_source == "Auto"`.

**Primary recommendation:** Use `Literal["flatbed", "adf"]` for the `auto_source_mode` field in both `ProfileConfig` and `ScanSettings` -- Pydantic validates Literal types natively with clear error messages, and the dataclass carries it as a type annotation for documentation purposes.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- D-01: Add `auto_source_mode: str` field to `ProfileConfig` with allowed values `"flatbed"` or `"adf"`
- D-02: Default value is `"flatbed"` -- safe single-page behavior, matching current implicit behavior
- D-03: Field only affects scan routing when the effective source is `"Auto"` -- ignored for explicit sources like `"Flatbed"` or `"ADF"`
- D-04: In `SaneBackend.scan_pages()`, when `effective_source` is `"Auto"`, use `settings.auto_source_mode` (passed through `ScanSettings`) to decide `use_adf` instead of `_is_adf_source()`
- D-05: `_is_adf_source()` remains unchanged -- it still handles explicit ADF source names; only the "Auto" case gets new logic
- D-06: `ScanSettings` dataclass gets a new `auto_source_mode: str = "flatbed"` field to carry the config value through
- D-07: When `generate_profiles()` encounters an "Auto" source, set `auto_source_mode` based on other available sources: if no explicit "Flatbed" source exists, default to `"adf"`; otherwise default to `"flatbed"`
- D-08: `source_to_slug("Auto")` should return `"auto-scan"` as the profile slug
- D-09: Users configure via TOML profile section: `auto_source_mode = "adf"` under a profile that has `source = "Auto"`
- D-10: Document the field in the existing reference docs page

### Claude's Discretion
- Exact validation approach for the field (Literal type vs validator)
- Whether to log a message when auto_source_mode takes effect
- Test structure and coverage approach

### Deferred Ideas (OUT OF SCOPE)
- The scanimage subprocess approach for flatbed scanning (debug/scanner-memory-overflow.md) is a separate concern
- Web UI dropdown to override auto_source_mode per-scan (would be a UI enhancement phase)
</user_constraints>

## Standard Stack

No new dependencies. This phase exclusively modifies existing project code.

### Core (already in project)
| Library | Purpose | Relevance |
|---------|---------|-----------|
| pydantic | `ProfileConfig` model validation | Literal type for `auto_source_mode` |
| dataclasses | `ScanSettings` | New field with default |
| tomlkit | Config writing in `auto_profiles.py` | Writing `auto_source_mode` to generated profiles |
| pytest | Testing | All new tests |

## Architecture Patterns

### Data Flow: Config to Scan Routing

The exact flow that must be extended:

```
TOML file
  -> ProfileConfig.auto_source_mode (Pydantic model, validated)
  -> run_pipeline() in pipeline.py (line 342-346, constructs ScanSettings)
  -> ScanSettings.auto_source_mode (dataclass field, carried through)
  -> scan_pages() in sane_backend.py (line 424, routing decision)
```

### Pattern 1: Pydantic Literal Field on ProfileConfig

**What:** Use `Literal["flatbed", "adf"]` for compile-time and runtime validation.
**When to use:** For the `auto_source_mode` field on `ProfileConfig`.

```python
from typing import Literal

class ProfileConfig(BaseModel):
    # ... existing fields ...
    auto_source_mode: Literal["flatbed", "adf"] = "flatbed"
```

Pydantic validates Literal types natively -- invalid values produce clear error messages like `Input should be 'flatbed' or 'adf'`. No custom validator needed.

### Pattern 2: Dataclass Field with Default

**What:** Add field to `ScanSettings` dataclass with a default value.
**When to use:** For carrying `auto_source_mode` through to `scan_pages()`.

```python
@dataclass
class ScanSettings:
    source: str
    resolution: int
    mode: str
    auto_source_mode: str = "flatbed"
```

The field has a default so existing `ScanSettings(source=..., resolution=..., mode=...)` calls remain valid. Only `run_pipeline()` needs updating.

### Pattern 3: Conditional Override in scan_pages()

**What:** After computing `use_adf` from `_is_adf_source()`, override when source is "Auto".
**When to use:** In `scan_pages()` at the routing decision point.

```python
use_adf = _is_adf_source(effective_source)

# Override for "Auto" source: use config-driven routing
if effective_source == "Auto":
    use_adf = settings.auto_source_mode == "adf"
```

This preserves `_is_adf_source()` for all explicit sources (D-05) and only intervenes for "Auto" (D-03).

### Pattern 4: Smart Default in generate_profiles()

**What:** Set `auto_source_mode` based on sibling sources when generating profiles for "Auto".
**When to use:** In `generate_profiles()` when iterating sources.

```python
for source in capabilities.sources:
    slug = source_to_slug(source)
    kwargs = {
        "source": source,
        "resolution": resolution,
        "mode": mode,
        "auto_generated": True,
    }
    if source == "Auto":
        has_flatbed = any("flatbed" in s.lower() for s in capabilities.sources)
        kwargs["auto_source_mode"] = "flatbed" if has_flatbed else "adf"
    profiles[slug] = ProfileConfig(**kwargs)
```

### Pattern 5: source_to_slug Extension

**What:** Add "Auto" case to `source_to_slug()`.
**When to use:** Before the fallback slugification.

```python
def source_to_slug(source: str) -> str:
    lower = source.lower()
    if lower == "auto":
        return "auto-scan"
    if "flatbed" in lower:
        return "flatbed-scan"
    # ... rest unchanged
```

Place it before "flatbed" check so "auto" doesn't fall through to a different match.

### Pattern 6: Pipeline ScanSettings Construction

**What:** Pass `auto_source_mode` from profile to ScanSettings.
**When to use:** In `run_pipeline()` at line 342.

```python
scan_settings = ScanSettings(
    source=profile.source,
    resolution=profile.resolution,
    mode=profile.mode,
    auto_source_mode=profile.auto_source_mode,
)
```

### Pattern 7: TOML Config Writing for Auto Profiles

**What:** Include `auto_source_mode` in generated profile TOML when source is "Auto".
**When to use:** In `write_profiles_to_config()`.

```python
profile_table.add("source", profile.source)
profile_table.add("resolution", profile.resolution)
profile_table.add("mode", profile.mode)
if profile.auto_source_mode != "flatbed":  # only write non-default
    profile_table.add("auto_source_mode", profile.auto_source_mode)
profile_table.add("auto_generated", True)
```

### Anti-Patterns to Avoid
- **Modifying `_is_adf_source()`:** D-05 explicitly locks this function as unchanged. The "Auto" routing is separate logic.
- **Adding auto_source_mode to the SANE device options:** This is a saneless config concept, not a SANE protocol concept. It never touches the device.
- **Forgetting the pipeline bridge:** If `run_pipeline()` doesn't pass `auto_source_mode` to `ScanSettings`, the field will silently use its default, making the config appear broken.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Field validation for "flatbed"/"adf" | Custom validator | `Literal["flatbed", "adf"]` | Pydantic handles it natively with clear errors |
| TOML comment preservation | Manual string manipulation | tomlkit (already used) | Round-trip TOML parsing is solved |

## Common Pitfalls

### Pitfall 1: Forgetting the Pipeline Bridge
**What goes wrong:** `auto_source_mode` is on `ProfileConfig` and `ScanSettings` but never passed between them in `run_pipeline()`.
**Why it happens:** The `ScanSettings` constructor in `run_pipeline()` uses explicit keyword arguments, not `**profile.model_dump()`.
**How to avoid:** Update `ScanSettings(...)` in `run_pipeline()` to include `auto_source_mode=profile.auto_source_mode`.
**Warning signs:** Config sets `auto_source_mode = "adf"` but Auto source still scans as flatbed.

### Pitfall 2: Source Fallback Masks Auto Routing
**What goes wrong:** When a profile requests source "Flatbed" but the scanner only has "Auto", `scan_pages()` falls back to `effective_source = "Auto"` (lines 404-410). But `settings.auto_source_mode` might be "flatbed" (the default), which is correct -- the user configured a Flatbed profile, it fell back to Auto, and the default "flatbed" behavior is right.
**Why it matters:** This fallback path already works correctly with the default. No special handling needed.
**How to avoid:** Just verify that fallback + default auto_source_mode = "flatbed" produces the expected flatbed behavior.

### Pitfall 3: Dataclass Field Ordering
**What goes wrong:** Adding `auto_source_mode: str = "flatbed"` to `ScanSettings` after fields without defaults causes a `TypeError`.
**Why it happens:** Python dataclasses require fields with defaults to come after fields without defaults.
**How to avoid:** All existing `ScanSettings` fields (`source`, `resolution`, `mode`) have no defaults. The new field with a default goes last -- this is correct.

### Pitfall 4: Literal Type Import Location
**What goes wrong:** Importing `Literal` at the wrong scope or under `TYPE_CHECKING` makes it unavailable at runtime for Pydantic validation.
**Why it happens:** `from __future__ import annotations` defers all annotation evaluation, but Pydantic needs the `Literal` type at class definition time for model schema generation.
**How to avoid:** Import `Literal` from `typing` at the top level, NOT under `TYPE_CHECKING`. The project's `config.py` already does top-level imports from `typing` (line 14: `from typing import TYPE_CHECKING, cast`).

### Pitfall 5: write_profiles_to_config Doesn't Write auto_source_mode
**What goes wrong:** The `write_profiles_to_config()` function explicitly lists which fields to write to TOML. If `auto_source_mode` isn't added to this list, auto-generated profiles with `auto_source_mode = "adf"` won't persist.
**Why it happens:** The function uses explicit `profile_table.add()` calls, not `profile.model_dump()`.
**How to avoid:** Add `auto_source_mode` to the field list in `write_profiles_to_config()`. Only write it when non-default to keep TOML clean.

### Pitfall 6: Logging When Auto Mode Takes Effect
**What goes wrong:** Silent routing changes confuse debugging.
**How to avoid:** Add an `logger.info()` when `auto_source_mode` overrides the default routing. This aligns with existing logging patterns (e.g., lines 406-409 log source fallback).

## Code Examples

### Complete ProfileConfig Change
```python
# In config.py
from typing import TYPE_CHECKING, Literal, cast

class ProfileConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source: str = "Flatbed"
    resolution: int = DEFAULT_RESOLUTION
    mode: str = "color"
    auto_source_mode: Literal["flatbed", "adf"] = "flatbed"
    # ... rest unchanged
```

### Complete scan_pages Routing Change
```python
# In sane_backend.py, scan_pages() around line 424
use_adf = _is_adf_source(effective_source)

# D-04: Override for "Auto" source using config-driven routing
if effective_source == "Auto":
    use_adf = settings.auto_source_mode == "adf"
    logger.info(
        "Auto source routing: auto_source_mode='%s', use_adf=%s",
        settings.auto_source_mode,
        use_adf,
    )
```

### Complete source_to_slug Change
```python
# In auto_profiles.py
def source_to_slug(source: str) -> str:
    lower = source.lower()
    if lower == "auto":
        return "auto-scan"
    if "flatbed" in lower:
        return "flatbed-scan"
    if "duplex" in lower:
        return "adf-duplex"
    if "back" in lower:
        return lower.replace(" ", "-").replace("_", "-")
    if "adf" in lower or "document feeder" in lower or "feeder" in lower:
        return "adf-simplex"
    return lower.replace(" ", "-").replace("_", "-")
```

### Documentation Addition
```markdown
<!-- In docs/reference/configuration.md, add to [profiles.NAME] table -->
| `auto_source_mode` | string | `"flatbed"` | When source is `"Auto"`: route as `"flatbed"` (single page) or `"adf"` (multi-page feeder). Ignored for explicit sources. |
```

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest (strict mode, strict markers) |
| Config file | pyproject.toml `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/test_scanner.py tests/test_auto_profiles.py tests/test_config.py -x` |
| Full suite command | `uv run pytest` |

### Phase Requirements -> Test Map

This phase has no formal requirement IDs in REQUIREMENTS.md (it's a post-v1 enhancement). Tests map to implementation decisions:

| Decision | Behavior | Test Type | Automated Command | File Exists? |
|----------|----------|-----------|-------------------|-------------|
| D-01/D-02 | ProfileConfig accepts auto_source_mode with Literal validation | unit | `uv run pytest tests/test_config.py -x -k auto_source` | Wave 0 |
| D-04 | scan_pages routes Auto to ADF when auto_source_mode="adf" | unit | `uv run pytest tests/test_scanner.py -x -k auto_source` | Wave 0 |
| D-04 | scan_pages routes Auto to flatbed when auto_source_mode="flatbed" | unit | `uv run pytest tests/test_scanner.py -x -k auto_source` | Wave 0 |
| D-05 | _is_adf_source unchanged, explicit sources unaffected | unit | `uv run pytest tests/test_scanner.py -x -k adf_scan` | Existing |
| D-06 | ScanSettings accepts auto_source_mode field | unit | `uv run pytest tests/test_scanner.py -x -k scan_settings` | Wave 0 |
| D-07 | generate_profiles sets auto_source_mode for Auto source | unit | `uv run pytest tests/test_auto_profiles.py -x -k auto` | Wave 0 |
| D-08 | source_to_slug("Auto") returns "auto-scan" | unit | `uv run pytest tests/test_auto_profiles.py -x -k auto` | Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_scanner.py tests/test_auto_profiles.py tests/test_config.py -x`
- **Per wave merge:** `uv run pytest`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] Tests for `auto_source_mode` on `ProfileConfig` (Literal validation, default value, invalid value rejection)
- [ ] Tests for `auto_source_mode` on `ScanSettings` (field exists, default value)
- [ ] Tests for `scan_pages()` Auto source routing with both `auto_source_mode` values
- [ ] Tests for `source_to_slug("Auto")` returning `"auto-scan"`
- [ ] Tests for `generate_profiles()` with Auto source (smart defaulting based on sibling sources)

None of these require framework or fixture additions -- existing `MockSaneDev`, `MockSaneModule`, and `sane_backend` fixtures cover all needs.

## Sources

### Primary (HIGH confidence)
- Direct code reading of `src/saneless/scanner/sane_backend.py` (current routing logic)
- Direct code reading of `src/saneless/scanner/base.py` (ScanSettings dataclass)
- Direct code reading of `src/saneless/config.py` (ProfileConfig model)
- Direct code reading of `src/saneless/auto_profiles.py` (generate_profiles, source_to_slug)
- Direct code reading of `src/saneless/pipeline.py` (ScanSettings construction at line 342)
- Direct code reading of `tests/test_scanner.py` and `tests/test_auto_profiles.py` (test patterns)

### Secondary (MEDIUM confidence)
- Pydantic Literal type validation -- well-established pattern, no external verification needed

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - no new dependencies, pure code modification
- Architecture: HIGH - data flow traced through source code end-to-end
- Pitfalls: HIGH - all identified from actual code structure and field ordering rules
- Code examples: HIGH - derived from reading exact current implementation

**Research date:** 2026-03-22
**Valid until:** 2026-04-22 (stable codebase, no external dependency changes)
