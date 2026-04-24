# Phase 19: User-Facing Docs — Getting Started & Feature Updates - Context

**Gathered:** 2026-03-26
**Status:** Ready for planning

<domain>
## Phase Boundary

Update and expand the existing Diataxis documentation site (built in Phase 15) with a new Getting Started top-level section and feature documentation for Phases 16-18. No code changes — documentation only.

</domain>

<decisions>
## Implementation Decisions

### Getting Started section (new top-level nav)
- **D-01:** Add a new "Getting Started" top-level nav section — first item in nav, above Tutorials
- **D-02:** Getting Started has 3 pages:
  1. **Quick Start** (new) — 5-minute path: Docker + Web UI as the primary flow, bare metal + CLI as tab alternative
  2. **First CLI Scan** — the existing "Scan Your First Document" tutorial, relocated from `tutorials/` to `getting-started/` and refreshed (fix Python 3.12 → 3.14, cover auto_source_mode and paper_size where relevant)
  3. **First Web UI Scan** (new) — open browser, select profile, enter metadata, click Scan, verify in paperless-ngx
- **D-03:** Quick Start prioritizes Docker + Web UI as the fastest path (most self-hosters use Docker). Bare metal shown as tab alternative
- **D-04:** The existing `tutorials/scan-your-first-document.md` moves to `getting-started/` and gets refreshed — Tutorials section becomes a placeholder for future task-specific tutorials (or is removed if empty)

### Feature documentation gaps (Phases 16-18)
- **D-05:** Weave `auto_source_mode` into existing guides: "Configure Scan Profiles" and "Set Up ADF Duplex Scanning" — no new pages
- **D-06:** Weave `paper_size` into existing guide: "Configure Scan Profiles" — no new page
- **D-07:** Phase 17 (Paperless upload fix) was an internal bug fix — no user-facing doc changes needed

### Style and structure (carried from Phase 15)
- **D-08:** No screenshots (D-13 from Phase 15) — text descriptions and config examples only
- **D-09:** All commands must be copy-pasteable (D-14 from Phase 15)
- **D-10:** Realistic example values (D-15 from Phase 15)
- **D-11:** Prerequisites sections where applicable (D-16 from Phase 15)

### Claude's Discretion
- Exact wording and flow of Quick Start and Web UI Scan pages
- Whether to keep an empty Tutorials section or remove it from nav
- Admonitions (tips, warnings) placement
- How deeply to cover auto_source_mode/paper_size in the how-to guides vs just linking to config reference

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Existing documentation (source of truth for current state)
- `mkdocs.yml` — Current nav structure, theme config, extensions
- `docs/index.md` — Docs landing page (needs nav update)
- `docs/tutorials/scan-your-first-document.md` — Existing tutorial to be relocated and refreshed
- `docs/how-to/configure-scan-profiles.md` — Needs auto_source_mode + paper_size additions
- `docs/how-to/set-up-adf-duplex.md` — Needs auto_source_mode mention
- `docs/reference/configuration.md` — Already has auto_source_mode and paper_size (Phase 16/18 updated this)

### Feature context (for accurate documentation)
- `src/saneless/config.py` — ProfileConfig with auto_source_mode and paper_size fields
- `src/saneless/paper_sizes.py` — Paper size presets and dimensions
- `src/saneless/web/routes.py` — Web UI endpoints (for Web UI Scan page accuracy)
- `src/saneless/web/templates/index.html` — Web UI layout (for describing the interface)

### Prior phase context
- `.planning/phases/15-create-user-facing-documentation-using-the-di-taxis-approach/15-CONTEXT.md` — All Phase 15 documentation decisions (style, audience, structure)
- `.planning/phases/16-when-a-scanner-advertised-auto-mode-it-should-be-configurable-by-the-user-if-that-means-flatbed-mode-or-adf-mode/16-CONTEXT.md` — auto_source_mode feature decisions
- `.planning/phases/18-automatic-scanned-page-size-detection-or-user-specified-paper-size-to-avoid-capturing-the-full-scanner-bed/18-CONTEXT.md` — paper_size feature decisions

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `docs/tutorials/scan-your-first-document.md` — 136-line tutorial to relocate and refresh (not rewrite from scratch)
- `docs/how-to/configure-scan-profiles.md` — Existing profile guide to extend with new fields
- `docs/how-to/set-up-adf-duplex.md` — Existing ADF guide to extend with auto_source_mode
- `docs/reference/configuration.md` — Already up-to-date with all config fields (link target)

### Established Patterns
- MkDocs Material with tabbed content (`=== "Tab"` syntax) for install alternatives
- Admonitions for tips and warnings (`!!! tip`, `!!! info`)
- Config examples use realistic IPs (192.168.x.x) and plausible values
- Each guide has Prerequisites section

### Integration Points
- `mkdocs.yml` nav — add Getting Started section, update Tutorials section
- `docs/index.md` — update landing page to feature Getting Started prominently
- `docs/getting-started/` — new directory for 3 pages
- Existing tutorial cross-links in other pages may need URL updates after relocation

</code_context>

<specifics>
## Specific Ideas

- Quick Start should feel like "5 minutes to your first scan" — Docker pull, open browser, done
- Web UI Scan page describes the actual UI elements (profile dropdown, title field, tags, scan button, status indicator)
- The moved tutorial gets Python version fixed (3.12 → 3.14) and mentions paper_size/auto_source_mode where natural

</specifics>

<deferred>
## Deferred Ideas

- API documentation auto-generation (OpenAPI/Swagger) — deferred from Phase 15
- Man pages for CLI commands — deferred from Phase 15
- Video tutorials — deferred from Phase 15
- Troubleshooting guide — could be a future how-to page

</deferred>

---

*Phase: 19-write-user-facing-docs-including-a-full-getting-started-section-that-walks-a-new-user-through-setup-and-first-scan-using-the-di-taxis-model*
*Context gathered: 2026-03-26*
