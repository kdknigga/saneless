# Phase 11: Review and Adjust Default DPI Setting - Context

**Gathered:** 2026-03-21
**Status:** Ready for planning

<domain>
## Phase Boundary

Evaluate whether the current 300 DPI default is appropriate for saneless's primary use case (document scanning for paperless-ngx). Research optimal DPI for document OCR, file size, readability, and archival standards. Adjust the default if warranted, or close as validated if 300 is correct. Does NOT add new scanning features, new profile types, or per-source DPI differentiation.

</domain>

<decisions>
## Implementation Decisions

### Research-driven default
- The default DPI value must be determined by research, not assumption
- Research must cover: OCR quality (Tesseract via paperless-ngx), file size / scan speed tradeoffs, visual readability, and industry document scanning standards
- saneless is primarily a document scanning tool (paperless-ngx is document management) — optimize for documents, not photos
- Absolute floor: 150 DPI (will not go below this regardless of research findings)
- If research validates 300 DPI as optimal, close the phase with no code changes

### Uniform DPI across sources
- Same default DPI for all source types (Flatbed, ADF, etc.)
- No per-source DPI differentiation — users who need different DPI per source customize their profiles manually

### Single source of truth
- Define a single `DEFAULT_RESOLUTION` constant
- `ProfileConfig.resolution` default and `pick_closest_resolution` target both reference this constant
- Auto-generated profiles (Phase 10) follow the default — no independent target

### Migration & existing configs
- Do NOT modify existing user TOML configs or auto-generated profiles on upgrade
- Only new auto-generated profiles use the updated default going forward
- Update the example `saneless.toml` in the repo to reflect the new default value
- No startup warnings, no re-generation prompts

### Claude's Discretion
- Where to define the `DEFAULT_RESOLUTION` constant (config.py module level, or separate constants module)
- Whether to add a brief code comment explaining the DPI rationale
- Test adjustments if DPI value changes

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### DPI configuration
- `src/saneless/config.py` — `ProfileConfig.resolution` field (current default: 300), the single place where profile defaults live
- `src/saneless/auto_profiles.py` — `pick_closest_resolution(target=300)` and `generate_profiles()` — auto-profile DPI target

### Scanner integration
- `src/saneless/scanner/sane_backend.py` — `dev.resolution = settings.resolution` at scan time, Pillow decompression bomb limits tied to DPI

### Example config
- `saneless.toml` — Example/dev config file with hardcoded `resolution = 300` values

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `ProfileConfig.resolution: int = 300` — single pydantic field controlling default DPI
- `pick_closest_resolution(resolutions, target=300)` — auto-profile resolution picker, target parameter is the key change point
- `generate_profiles()` — calls `pick_closest_resolution` with hardcoded `target=300`

### Established Patterns
- Config defaults defined as pydantic field defaults in `config.py`
- Auto-profile generation in `auto_profiles.py` uses its own target value (currently duplicates the config default)
- Pillow `MAX_IMAGE_PIXELS` set to 200M in `sane_backend.py`, `pdf.py`, `pages.py` — sized for 1200 DPI scans, not affected by default DPI changes

### Integration Points
- `config.py` — `ProfileConfig.resolution` default value
- `auto_profiles.py` — `pick_closest_resolution` target parameter and `generate_profiles` call
- `saneless.toml` — example config file resolution values
- Tests referencing resolution=300 may need updating

</code_context>

<specifics>
## Specific Ideas

- "Paperless is almost completely focused on document management, so saneless should be as well" — optimize DPI default for documents, not photos
- If research validates current 300 DPI, phase closes with no code changes (no need to force a change)

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope

</deferred>

---

*Phase: 11-review-and-adjust-default-dpi-setting*
*Context gathered: 2026-03-21*
