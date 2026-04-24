# Phase 18: Scanned Page Size / Scan Area Control - Context

**Gathered:** 2026-03-23
**Status:** Ready for planning

<domain>
## Phase Boundary

Allow users to constrain the scan area to standard paper dimensions rather than always scanning the full scanner bed. Users specify a paper size preset (A4, Letter, Legal, etc.) per profile; the scanner backend sets SANE geometry options (`br-x`/`br-y`) before scanning. If the scanner doesn't support geometry options, fall back to post-scan Pillow crop so the output still matches the requested size.

</domain>

<decisions>
## Implementation Decisions

### Scan area method
- **D-01:** Primary approach is setting SANE geometry options (`br-x`, `br-y`) before scanning to constrain the acquisition area at the hardware level — faster scan, smaller image, less data
- **D-02:** If the scanner does not expose geometry options (or they are read-only), fall back to scanning the full bed and cropping with Pillow to the target paper dimensions based on DPI
- **D-03:** No auto-detection of paper edges via image analysis — this is a known-dimensions approach, not a computer vision approach

### Paper size configuration
- **D-04:** New `paper_size` field on `ProfileConfig` with named presets: `"full"`, `"a4"`, `"letter"`, `"legal"` (and potentially other common sizes)
- **D-05:** Default value is `"full"` — preserves current full-bed behavior. Users opt in to paper size constraint. Zero-change upgrade path
- **D-06:** Preset names map to known mm dimensions internally (e.g., A4 = 210x297mm, Letter = 215.9x279.4mm)
- **D-07:** `paper_size` field flows through `ScanSettings` dataclass to `scan_pages()`, following the established pattern from `auto_source_mode` (Phase 16)

### Auto-profile generation
- **D-08:** Auto-generated profiles keep `paper_size = "full"` (the default). No inference from scanner geometry — avoids wrong guesses
- **D-09:** Users who care about paper size configure it manually in their TOML profile

### Claude's Discretion
- Whether `paper_size` should apply to ADF scans, flatbed only, or both — decide based on how SANE ADF geometry actually works in practice
- Which additional paper size presets to include beyond A4, Letter, Legal (e.g., A3, A5, Tabloid)
- How to parse SANE geometry option constraints (min/max ranges vs discrete values)
- Whether to set `tl-x`/`tl-y` to 0 explicitly or leave them at default (scanner origin)
- Exact Pillow crop logic for the fallback path (centered crop vs top-left aligned)
- Test structure and mock approach for geometry option handling

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Scanner backend — where geometry options are set
- `src/saneless/scanner/sane_backend.py` — `scan_pages()` method (lines 360-446) where device options are set before scanning; `get_capabilities()` (lines 238-277) which already reads `raw_options`
- `src/saneless/scanner/base.py` — `ScanSettings` dataclass (line 44), `DeviceCapabilities` dataclass (line 34), `ScannerBackend` ABC

### Configuration model
- `src/saneless/config.py` — `ProfileConfig` model (line 63) where `paper_size` field will be added alongside `auto_source_mode`

### Auto-profile generation
- `src/saneless/auto_profiles.py` — `generate_profiles()` — must propagate default `paper_size = "full"`

### PDF assembly
- `src/saneless/pdf.py` — `assemble_pdf()` — receives images that may now be smaller than full bed; no changes expected but verify img2pdf handles variable page sizes

### Pipeline
- `src/saneless/pipeline.py` — `run_pipeline()` constructs `ScanSettings` from `ProfileConfig` — must pass `paper_size`

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `ProfileConfig`: Pydantic BaseModel — new `paper_size` field sits alongside `auto_source_mode`, same pattern
- `ScanSettings`: Simple dataclass — add `paper_size: str = "full"` field
- `DeviceCapabilities.raw_options`: Already captures all SANE options including geometry — just needs parsing
- `get_capabilities()`: Already iterates `raw_options` — can be extended to extract geometry bounds

### Established Patterns
- Config fields flow: `ProfileConfig` → `ScanSettings` → `scan_pages()` (see `auto_source_mode` in Phase 16)
- SANE option setting: `dev.mode`, `dev.resolution`, `dev.source` pattern — geometry may use same attribute-style or `dev.__setattr__()` for option names with hyphens
- Auto-profile generation writes `paper_size` only if non-default (Phase 16: "Only write auto_source_mode to TOML when non-default")

### Integration Points
- `scan_pages()` in sane_backend.py — set geometry before `dev.start()` / `multi_scan()`
- `ProfileConfig` in config.py — add field with Literal type for validation
- `ScanSettings` in base.py — add field to carry value through
- `run_pipeline()` — pass `paper_size` when constructing `ScanSettings`
- Pillow crop fallback — new utility function in pages.py or inline in sane_backend.py

</code_context>

<specifics>
## Specific Ideas

- SANE geometry options use mm as units — paper size presets map directly to mm without DPI conversion for the SANE path
- For the Pillow crop fallback, DPI conversion is needed: crop_pixels = paper_mm * dpi / 25.4
- The `raw_options` tuple format is `(index, name, title, desc, type, unit, size, cap, constraint)` — geometry options have `name` like `"br-x"`, `"br-y"`, `"tl-x"`, `"tl-y"` with mm units

</specifics>

<deferred>
## Deferred Ideas

- Custom paper dimensions via `"WxH"` string format (e.g., `paper_size = "210x297"`) — could be added later if presets prove insufficient
- Auto-detection of paper edges via image analysis — different feature entirely
- Web UI dropdown for paper size selection per-scan (would be a UI enhancement phase)

</deferred>

---

*Phase: 18-automatic-scanned-page-size-detection-or-user-specified-paper-size-to-avoid-capturing-the-full-scanner-bed*
*Context gathered: 2026-03-23*
