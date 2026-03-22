# Phase 10 — UI Review

**Audited:** 2026-03-22
**Baseline:** Abstract 6-pillar standards (no UI-SPEC.md for this phase)
**Screenshots:** Not captured — Phase 10 made no changes to web templates; this is a CLI-only phase. Code audit only.

---

## Important Context: Scope of This Phase

Phase 10 added no web UI changes. The only user-facing interfaces are:

1. The `saneless auto-profiles` CLI command (stdout/stderr output, exit codes)
2. Worker log messages (internal, not user-facing at runtime)
3. The existing web UI's profile dropdown (unchanged — auto-generated profiles appear here automatically after generation)

The 6-pillar audit is therefore applied to the CLI interaction surface and the module's code-level quality signals.

---

## Pillar Scores

| Pillar | Score | Key Finding |
|--------|-------|-------------|
| 1. Copywriting | 3/4 | CLI output strings are clear and specific; one output line uses a raw key=value format that reads mechanically |
| 2. Visuals | 3/4 | CLI output structure is clean and scannable; no icon-only buttons; table alignment established for other commands not applied here |
| 3. Color | 4/4 | No hardcoded colors, no Tailwind misuse; error output correctly routes to stderr |
| 4. Typography | 4/4 | No font misuse; CLI output uses consistent indentation (2-space) and clear hierarchy |
| 5. Spacing | 3/4 | Profile summary output indented consistently; a single raw f-string with key=value pairs lacks the field-alignment used elsewhere in the CLI |
| 6. Experience Design | 4/4 | All error and edge-case paths handled; loading state communicated; force flag prevents accidental overwrites; single-attempt guard prevents retriggering |

**Overall: 21/24**

---

## Top 3 Priority Fixes

1. **Profile summary output uses raw `key=value` formatting instead of aligned columns** — Users scanning the output cannot quickly compare profile settings across multiple profiles; the `devices` command already demonstrates a well-aligned table pattern — apply the same `f"{name:<20} source={p.source:<15} res={p.resolution:<5} mode={p.mode}"` style to the auto-profiles summary output in `src/saneless/cli.py` line 294.

2. **"No new profiles written (use --force to overwrite)." exits with code 0 when all profiles already exist** — A user running `auto-profiles` on a system where profiles are already present gets a silent non-action with a success exit code, making it difficult to detect in shell scripts; add `sys.exit(0)` with a comment or change to exit code `2` (already used for config errors) when nothing was written and `--force` was not supplied, so callers can distinguish "generated" from "nothing to do".

3. **Worker lazy-trigger log message "Auto-profiles: scanner unreachable, using bare default" is only visible at WARNING level in log file** — Users running headless (`saneless serve`) will not see any console indication that scanner profile generation silently fell back; surface this to stderr via `click.echo` in the worker or emit a startup warning through the web API status endpoint, so the operator knows why profiles are not being generated.

---

## Detailed Findings

### Pillar 1: Copywriting (3/4)

**Passing:**
- `"No scanners found."` (line 276) — specific and actionable
- `"Generated {N} profile(s) in {path}:"` (line 291) — communicates count and destination
- `"No new profiles written (use --force to overwrite)."` (line 288) — includes recovery hint
- `"Auto-profiles: no scanners found, using bare default"` (worker line 171) — specific fallback message
- `"Auto-profiles: scanner unreachable, using bare default"` (worker line 188) — clear cause

**Concerns:**
- Line 294-296 in `cli.py`: `f"  {name}: source={p.source}, resolution={p.resolution}, mode={p.mode}"` — the colon-separated key=value format is functional but reads mechanically compared to the formatted table style used in `devices` (lines 167-171) and `jobs` (lines 216-221). The inconsistency creates a jarring shift in output style between commands.
- "No new profiles written" is a negative-space message — it tells the user what did not happen but not why profiles already exist. Consider: `"Profiles already exist — run with --force to regenerate."` for more clarity.

### Pillar 2: Visuals (3/4)

**Passing:**
- All CLI icon-only buttons are absent (text-only CLI)
- Error output correctly separated to stderr (err=True on all error echoes)
- Docstrings present and complete on all 7 exported functions and the `auto_profiles` CLI command

**Concerns:**
- The auto-profiles output is the only multi-line summary in the CLI that does not use aligned columns. `devices` uses `f"{d.name:<30} {d.vendor:<15} {d.model:<20} {d.device_type}"`. The profile summary would benefit from the same alignment, especially when 4+ profiles are output (flatbed-scan, adf-simplex, adf-duplex, default).
- No `--json` output option on `auto-profiles` command, unlike `devices` and `jobs`. This is a minor gap — scripted callers cannot parse generated profiles programmatically without parsing stdout text.

### Pillar 3: Color (4/4)

No Tailwind usage in Phase 10 files. No hardcoded colors. The web templates were not modified. Error output correctly uses `err=True` to route to stderr, which is the CLI equivalent of correct color/channel separation. Full marks.

### Pillar 4: Typography (4/4)

No web template changes. CLI output uses consistent two-space indentation for profile detail lines (line 294-296), matching established CLI output conventions. Module docstrings follow the multi-line-summary-second-line style required by ruff D rules. All 7 functions have complete Args/Returns documentation. Full marks.

### Pillar 5: Spacing (3/4)

**Passing:**
- Two-space indent for profile detail lines is consistent
- No arbitrary spacing values
- Docstring formatting consistent with project conventions

**Concerns:**
- The profile summary line packs three fields (`source=`, `resolution=`, `mode=`) into a single unaligned line. With four profiles, the output becomes hard to scan:
  ```
    flatbed-scan: source=Flatbed, resolution=300, mode=Color
    adf-simplex: source=ADF, resolution=300, mode=Color
    adf-duplex: source=ADF Duplex, resolution=300, mode=Color
    default: source=Flatbed, resolution=300, mode=Color
  ```
  The profile name widths are inconsistent and the fields do not align across rows. The spacing pattern from `devices` (fixed-width left-justified columns) would resolve this.

### Pillar 6: Experience Design (4/4)

Phase 10 demonstrates thorough experience design across all interaction paths:

**Loading/progress states:**
- CLI prints `"Generated N profile(s) in {path}:"` before listing profiles — user gets confirmation of outcome
- Worker logs `"Auto-generated N profile(s): ..."` on success

**Error states:**
- Scanner not found: `"No scanners found."` + exit code 1 (line 276)
- Scanner unreachable during lazy trigger: `logger.warning(... exc_info=True)` with graceful fallback (worker lines 187-190)
- Config load failure: `"Configuration error: {exc}"` + exit code 2 (existing, line 59)

**Empty states:**
- No new profiles to write: explicit feedback + recovery hint (line 288)
- No devices during auto-profiles: exits cleanly (line 276)

**Conflict/destructive action protection:**
- `--force` flag required to overwrite existing profiles — correct guard for a potentially destructive operation
- `_auto_generated` flag ensures single-attempt semantics — prevents runaway retrigger on failure

**Guard against repeated execution:**
- Worker sets `self._auto_generated = True` before attempting (worker line 163) — failures do not cause infinite retry loops

No major experience design gaps found. Full marks.

---

## Registry Safety

No `components.json` found. shadcn not initialized. Registry audit skipped.

---

## Files Audited

- `src/saneless/auto_profiles.py` — Core module (248 lines)
- `src/saneless/cli.py` — CLI command wiring, `auto-profiles` command (297 lines)
- `src/saneless/worker.py` — Lazy trigger integration (265 lines)
- `src/saneless/config.py` — `ProfileConfig.auto_generated` field addition (first 60 lines examined)
- `tests/test_auto_profiles.py` — 28 unit tests across 7 test classes (285 lines)
- `tests/test_cli.py` — `TestAutoProfiles` class with 4 tests (excerpted)
- `src/saneless/web/templates/index.html` — Confirmed no Phase 10 changes (profile dropdown unchanged)
