# Phase 10: Automatic Scanner Profile Creation - Context

**Gathered:** 2026-03-21
**Status:** Ready for planning

<domain>
## Phase Boundary

Automatically generate scan profiles from scanner capabilities so users don't have to manually write TOML config. Covers lazy first-scan generation, an explicit CLI command, TOML persistence, and conflict detection. Does NOT add new scanning features, UI changes beyond existing profile dropdown, or scanner discovery changes.

</domain>

<decisions>
## Implementation Decisions

### Trigger & timing
- Auto-generation triggers lazily on first scan when only the bare (uncustomized) `default` profile exists
- If scanner is unreachable at trigger time, fall back to bare `default` profile and retry next time
- Also available as explicit CLI command: `saneless auto-profiles`
- CLI command requires `--force` flag to overwrite existing profiles

### Profile generation
- Generate one profile per available source (e.g., `flatbed-scan`, `adf-simplex`, `adf-duplex`)
- Default resolution: 300 dpi (or closest available if scanner doesn't support 300)
- Default mode: Color (or closest available)
- Naming: descriptive lowercase slugs like `flatbed-scan`, `adf-simplex`, `adf-duplex`
- The `default` profile is also updated to point to Flatbed at 300dpi/Color (so `saneless scan` works out of the box)

### Conflict handling
- Lazy trigger backs off once user has customized profiles (any non-bare-default profile exists)
- `auto_generated = true` marker field on each auto-generated profile for machine-parseable detection
- CLI `saneless auto-profiles` refuses to overwrite without `--force`
- If scanner capabilities don't match existing profiles (scanner swap), log a warning but don't auto-change

### Persistence
- Auto-generated profiles are written to the TOML config file (permanent, editable by user)
- `auto_generated = true` field added to each generated profile section
- CLI command prints summary: profile names, key settings, file path written to
- Both lazy trigger and CLI command print to stdout

### Claude's Discretion
- Exact source-name-to-slug mapping logic
- How to detect "bare default" vs customized (compare against ProfileConfig defaults or check auto_generated markers)
- TOML serialization library choice (tomli-w, tomlkit, etc.)
- Warning message format for scanner capability mismatch
- Whether `auto_generated` field is preserved or stripped when user edits a profile

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Scanner capabilities
- `src/saneless/scanner/base.py` — `DeviceCapabilities` dataclass (sources, resolutions, modes), `ScannerBackend.get_capabilities()` ABC method
- `src/saneless/scanner/sane_backend.py` — `SaneBackend.get_capabilities()` concrete implementation

### Profile configuration
- `src/saneless/config.py` — `ProfileConfig` model (source, resolution, mode, thresholds, metadata defaults), `Settings.profiles` dict, `validate_default_profile` validator

### CLI patterns
- `src/saneless/cli.py` — Existing Click commands (`scan`, `devices`, `jobs`), `--profile` flag pattern, `--json` output suppression pattern

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `ScannerBackend.get_capabilities(device_id)` → returns `DeviceCapabilities` with sources, resolutions, modes
- `ProfileConfig` pydantic model already handles all profile fields with defaults
- `Settings.validate_default_profile` ensures `default` key exists — will need to accommodate auto-generated default
- `DeviceInfo` dataclass for scanner identification (useful for mismatch detection)

### Established Patterns
- Config loaded via pydantic-settings from TOML + env vars (`_build_settings` / `load_settings`)
- CLI uses Click with `@cli.command()` decorators and shared context via `@click.pass_context`
- Lazy import pattern for python-sane via `_ensure_sane()`
- Profile selection: CLI `--profile` flag, web UI form POST field

### Integration Points
- `cli.py` — New `auto-profiles` command added alongside existing commands
- `config.py` — `ProfileConfig` needs `auto_generated: bool = False` field (with `extra="ignore"` or explicit field)
- `pipeline.py` or `worker.py` — Lazy trigger hook before first scan execution
- TOML config file — Write path needed (currently read-only)

</code_context>

<specifics>
## Specific Ideas

No specific requirements — open to standard approaches

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope

</deferred>

---

*Phase: 10-automatic-scanner-profile-creation*
*Context gathered: 2026-03-21*
