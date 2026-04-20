# Phase 16: Configurable Auto Source Mode - Context

**Gathered:** 2026-03-22
**Status:** Ready for planning

<domain>
## Phase Boundary

When a scanner advertises "Auto" as a source, the user can configure whether that means flatbed scanning (single page, snap) or ADF scanning (multi-page, multi_scan). Currently `_is_adf_source("Auto")` returns False, so Auto always routes to flatbed. This phase makes that routing configurable per profile.

</domain>

<decisions>
## Implementation Decisions

### Config field for auto source behavior
- **D-01:** Add `auto_source_mode: str` field to `ProfileConfig` with allowed values `"flatbed"` or `"adf"`
- **D-02:** Default value is `"flatbed"` — safe single-page behavior, matching current implicit behavior
- **D-03:** Field only affects scan routing when the effective source is `"Auto"` — ignored for explicit sources like `"Flatbed"` or `"ADF"`

### Scan path routing change
- **D-04:** In `SaneBackend.scan_pages()`, when `effective_source` is `"Auto"`, use `settings.auto_source_mode` (passed through `ScanSettings`) to decide `use_adf` instead of `_is_adf_source()`
- **D-05:** `_is_adf_source()` remains unchanged — it still handles explicit ADF source names; only the "Auto" case gets new logic
- **D-06:** `ScanSettings` dataclass gets a new `auto_source_mode: str = "flatbed"` field to carry the config value through

### Auto-profile generation
- **D-07:** When `generate_profiles()` encounters an "Auto" source, set `auto_source_mode` based on other available sources: if no explicit "Flatbed" source exists, default to `"adf"`; otherwise default to `"flatbed"`
- **D-08:** `source_to_slug("Auto")` should return `"auto-scan"` as the profile slug

### TOML config surface
- **D-09:** Users configure via TOML profile section: `auto_source_mode = "adf"` under a profile that has `source = "Auto"`
- **D-10:** Document the field in the existing reference docs page

### Claude's Discretion
- Exact validation approach for the field (Literal type vs validator)
- Whether to log a message when auto_source_mode takes effect
- Test structure and coverage approach

</decisions>

<specifics>
## Specific Ideas

- The removed heuristic (Auto + no Flatbed → ADF) was the right idea but wrong place — it should be in auto-profile generation, not in scan-time routing
- Users with scanners that only have "Auto" (no explicit Flatbed/ADF) need the ADF path to work for multi-page scanning

</specifics>

<canonical_refs>
## Canonical References

### Scanner source handling
- `src/saneless/scanner/sane_backend.py` — `scan_pages()` source validation and routing logic (lines 364-436), `_is_adf_source()` helper (lines 92-94)
- `src/saneless/scanner/base.py` — `ScanSettings` dataclass and `DeviceCapabilities` model

### Configuration model
- `src/saneless/config.py` — `ProfileConfig` model with source field (line 67)

### Auto-profile generation
- `src/saneless/auto_profiles.py` — `generate_profiles()`, `source_to_slug()` — must handle "Auto" source intelligently

### Documentation
- `docs/reference/configuration.md` — Config reference page where new field must be documented

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `ProfileConfig`: Pydantic BaseModel with existing `source` field — new field sits alongside it
- `ScanSettings`: Simple dataclass — straightforward to extend with one field
- `_is_adf_source()`: Existing helper for ADF detection — remains for explicit sources
- `source_to_slug()`: Handles known source names — needs "Auto" case added

### Established Patterns
- Profile config fields flow through `ScanSettings` dataclass to `scan_pages()`
- `generate_profiles()` creates one profile per source with sensible defaults
- Auto-profile logic already inspects available sources to set the "default" profile

### Integration Points
- `run_pipeline()` in worker constructs `ScanSettings` from `ProfileConfig` — must pass `auto_source_mode`
- Web UI scan form and CLI scan command both go through worker — no UI changes needed (field is profile-level config)
- `source_to_slug()` is called during auto-profile generation — must handle "Auto" without crashing

</code_context>

<deferred>
## Deferred Ideas

- The scanimage subprocess approach for flatbed scanning (debug/scanner-memory-overflow.md) is a separate concern — that's about the scan mechanism, this phase is about source routing
- Web UI dropdown to override auto_source_mode per-scan (would be a UI enhancement phase)

</deferred>

---

*Phase: 16-when-a-scanner-advertised-auto-mode-it-should-be-configurable-by-the-user-if-that-means-flatbed-mode-or-adf-mode*
*Context gathered: 2026-03-22*
