# Phase 19: User-Facing Docs -- Getting Started & Feature Updates - Research

**Researched:** 2026-03-26
**Domain:** MkDocs Material documentation, Diataxis model, documentation-only changes
**Confidence:** HIGH

## Summary

This is a documentation-only phase. The existing MkDocs Material site (built in Phase 15) needs a new "Getting Started" top-level nav section with three pages, plus weaving `auto_source_mode` and `paper_size` into two existing how-to guides. No code changes, no new dependencies, no external tooling beyond what is already configured.

The existing site uses MkDocs Material with tabbed content, admonitions, and a standard Diataxis four-quadrant nav (Tutorials, How-To Guides, Reference, Explanation). The Getting Started section becomes a fifth top-level entry, placed first in nav. The main risk is broken cross-links when relocating the tutorial from `tutorials/` to `getting-started/`.

**Primary recommendation:** Three plans -- (1) create Getting Started section with Quick Start and Web UI Scan pages, relocate and refresh the CLI tutorial; (2) weave auto_source_mode and paper_size into existing how-to guides; (3) update index.md, nav, and fix all cross-links.

<user_constraints>

## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Add a new "Getting Started" top-level nav section -- first item in nav, above Tutorials
- **D-02:** Getting Started has 3 pages: Quick Start (new), First CLI Scan (relocated tutorial), First Web UI Scan (new)
- **D-03:** Quick Start prioritizes Docker + Web UI as the fastest path; bare metal shown as tab alternative
- **D-04:** Existing `tutorials/scan-your-first-document.md` moves to `getting-started/` and gets refreshed; Tutorials section becomes placeholder or removed
- **D-05:** Weave `auto_source_mode` into "Configure Scan Profiles" and "Set Up ADF Duplex Scanning" -- no new pages
- **D-06:** Weave `paper_size` into "Configure Scan Profiles" -- no new page
- **D-07:** Phase 17 (Paperless upload fix) was internal -- no user-facing doc changes
- **D-08:** No screenshots -- text descriptions and config examples only
- **D-09:** All commands must be copy-pasteable
- **D-10:** Realistic example values
- **D-11:** Prerequisites sections where applicable

### Claude's Discretion
- Exact wording and flow of Quick Start and Web UI Scan pages
- Whether to keep an empty Tutorials section or remove it from nav
- Admonitions (tips, warnings) placement
- How deeply to cover auto_source_mode/paper_size in how-to guides vs just linking to config reference

### Deferred Ideas (OUT OF SCOPE)
- API documentation auto-generation (OpenAPI/Swagger)
- Man pages for CLI commands
- Video tutorials
- Troubleshooting guide

</user_constraints>

## Project Constraints (from CLAUDE.md)

- **Playwright MCP** must be used for browser-based validation of docs site (not "manual-only")
- **No screenshots** in docs (aligns with D-08)
- Python version is 3.14 (tutorial currently says 3.12 -- must be fixed)
- `uv` is the package manager (not pip) -- docs should reference `uv` where applicable for bare metal
- All quality checks (ruff, ty, pyrefly) not applicable since this is docs-only, but `prek` hooks may still apply

## Standard Stack

### Core
| Tool | Version | Purpose | Why Standard |
|------|---------|---------|--------------|
| MkDocs Material | already configured | Static site generator with Material theme | Already in use since Phase 15 |
| pymdownx.tabbed | already configured | Tab content for Docker/bare-metal alternatives | Already in mkdocs.yml extensions |
| admonition | already configured | Tips, warnings, info boxes | Already in mkdocs.yml extensions |

No new dependencies needed. The docs site is fully configured.

## Architecture Patterns

### Current Nav Structure
```
nav:
  - Home: index.md
  - Tutorials:                          # <-- will be emptied or removed
      - Scan Your First Document        # <-- relocates to getting-started/
  - How-To Guides: [6 pages]
  - Reference: [5 pages]
  - Explanation: [3 pages]
```

### Target Nav Structure
```
nav:
  - Home: index.md
  - Getting Started:                     # NEW -- first after Home
      - Quick Start: getting-started/quick-start.md
      - First CLI Scan: getting-started/first-cli-scan.md
      - First Web UI Scan: getting-started/first-web-ui-scan.md
  - How-To Guides: [6 pages, 2 updated]
  - Reference: [5 pages, unchanged]
  - Explanation: [3 pages, unchanged]
```

### New Directory Structure
```
docs/
  getting-started/
    quick-start.md          # NEW
    first-cli-scan.md       # RELOCATED from tutorials/scan-your-first-document.md
    first-web-ui-scan.md    # NEW
  tutorials/                # DELETE directory or keep empty
```

### Pattern: Quick Start Page Flow
The Quick Start should follow a linear 5-minute path:

1. Prerequisites (one-liner: scanner + paperless-ngx + Docker or Python)
2. Install (Docker primary tab, bare metal secondary tab)
3. Create minimal config (paperless URL + token)
4. Open web UI in browser
5. Click Scan, verify in paperless-ngx
6. Next steps links

### Pattern: Web UI Scan Page Flow
Describes the actual web interface elements the user sees:

1. Prerequisites (saneless running, browser)
2. Open `http://<host>:8080`
3. Profile dropdown -- select scan profile
4. Title field -- enter document name
5. Tags multi-select -- pick from paperless-ngx tags (with refresh button)
6. Correspondent select -- pick from list (with refresh button)
7. Scan button -- click and wait
8. Status area -- shows scanning/assembling/uploading/done
9. First-page thumbnail -- appears during scan
10. Job history table -- shows completed scan
11. Verify in paperless-ngx

### Pattern: Feature Weaving (auto_source_mode / paper_size)
Add to existing pages rather than creating new ones:

**In configure-scan-profiles.md:**
- Add `auto_source_mode` to the "Source values" section (new entry for `"Auto"`)
- Add `paper_size` as a new section after "Source values" or in the profile field reference table
- Add both fields to the profile field reference table (currently missing)

**In set-up-adf-duplex.md:**
- Mention `auto_source_mode` in context of scanners that only expose `Auto` source
- Brief example showing `source = "Auto"` with `auto_source_mode = "adf"`

### Anti-Patterns to Avoid
- **Duplicating reference content in tutorials:** Link to the Configuration reference for full field details; don't reproduce the entire table
- **Broken cross-links after relocation:** Every reference to `tutorials/scan-your-first-document.md` must be updated
- **Stale Python version:** The current tutorial says "Python 3.12" -- must be updated to 3.14
- **pip instead of pipx/uv:** The tutorial currently says `pip install saneless` -- should use pipx (matching the bare metal how-to)

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Tab content for install alternatives | Custom HTML | `=== "Tab"` pymdownx.tabbed syntax | Already configured, consistent with existing docs |
| Callout boxes | Custom CSS divs | `!!! tip` / `!!! warning` admonitions | MkDocs Material native support |
| Dark/light mode | Custom toggle | Material palette toggle | Already configured in mkdocs.yml |

## Common Pitfalls

### Pitfall 1: Broken Cross-Links After Tutorial Relocation
**What goes wrong:** Moving `tutorials/scan-your-first-document.md` to `getting-started/first-cli-scan.md` breaks all internal links pointing to the old URL.
**Why it happens:** MkDocs uses relative paths; any page linking to `../tutorials/scan-your-first-document.md` will 404.
**How to avoid:** Grep for all references to `scan-your-first-document` across the docs directory and update them. Currently only `docs/index.md` links to it.
**Warning signs:** `mkdocs build --strict` will report broken links.

### Pitfall 2: Stale Tutorial Content
**What goes wrong:** The relocated tutorial still references Python 3.12 and `pip install saneless`.
**Why it happens:** The tutorial was written during Phase 15 and not updated for Phase 16-18 features.
**How to avoid:** During relocation, update: Python 3.12 to 3.14, pip to pipx, and mention paper_size/auto_source_mode where natural.

### Pitfall 3: Nav Ordering in mkdocs.yml
**What goes wrong:** Getting Started section not placed first, or Tutorials section left with broken reference.
**Why it happens:** mkdocs.yml nav is an ordered list -- position matters for user experience.
**How to avoid:** Getting Started must be the second item (after Home), before How-To Guides. Remove or empty the Tutorials section.

### Pitfall 4: Inconsistent Relative Link Depths
**What goes wrong:** Links from `getting-started/*.md` to `how-to/*.md` or `reference/*.md` use wrong relative depth.
**Why it happens:** New pages are in a new subdirectory; the relative path prefix changes from `../tutorials/` to `../getting-started/`.
**How to avoid:** All cross-links from getting-started pages to other sections use `../how-to/`, `../reference/`, etc.

## Code Examples

### MkDocs Tabbed Content (Docker/Bare Metal)
```markdown
=== "Docker"

    ```bash
    docker run -p 8080:8080 \
      -v ./config.toml:/etc/saneless/config.toml:ro \
      -e SANELESS_SCANNER__HOST=192.168.1.50 \
      ghcr.io/kris-knigga/saneless:latest
    ```

=== "Bare metal"

    ```bash
    pipx install saneless
    saneless serve
    ```
```

### Admonition Examples
```markdown
!!! tip "First time?"
    The fastest path is Docker + Web UI. You can switch to bare metal later.

!!! info "Network scanners"
    If your scanner is on a different machine, set `SANELESS_SCANNER__HOST`
    or `scanner.host` in your config. See [Scanner Host Discovery](../how-to/scanner-host-discovery.md).
```

### auto_source_mode Documentation Snippet
```markdown
### Auto source

Some scanners expose only an `Auto` source instead of separate `Flatbed` and `ADF` entries.
When you set `source = "Auto"`, saneless needs to know whether to treat it as a single-page
flatbed scan or a multi-page ADF scan. The `auto_source_mode` field controls this:

| `auto_source_mode` | Behavior |
|---|---|
| `"flatbed"` (default) | Single page, like Flatbed |
| `"adf"` | Multi-page feeder, like ADF |

```toml
[profiles.auto-scan]
source = "Auto"
auto_source_mode = "adf"
resolution = 300
mode = "Color"
```

See [Configuration reference](../reference/configuration.md) for all profile fields.
```

### paper_size Documentation Snippet
```markdown
## Paper size

By default, saneless scans the entire scanner bed. If your documents are a standard size,
set `paper_size` to crop the scan area automatically:

```toml
[profiles.letters]
source = "ADF"
resolution = 300
mode = "Color"
paper_size = "letter"
```

Available presets: `full` (default -- entire bed), `a3`, `a4`, `a5`, `letter`, `legal`.

When your scanner supports SANE geometry options, saneless sets the scan area directly.
Otherwise, it crops the image after scanning.
```

## Existing Content Audit

### Files to CREATE (new)
| File | Purpose |
|------|---------|
| `docs/getting-started/quick-start.md` | 5-minute Docker + Web UI path |
| `docs/getting-started/first-web-ui-scan.md` | Web UI walkthrough |

### Files to RELOCATE + REFRESH
| From | To | Changes |
|------|------|---------|
| `docs/tutorials/scan-your-first-document.md` | `docs/getting-started/first-cli-scan.md` | Rename, fix Python 3.12 to 3.14, fix pip to pipx, mention auto_source_mode/paper_size where natural, update relative links |

### Files to UPDATE (weave features)
| File | Changes |
|------|---------|
| `docs/how-to/configure-scan-profiles.md` | Add auto_source_mode to source values, add paper_size section, update profile field reference table |
| `docs/how-to/set-up-adf-duplex.md` | Brief auto_source_mode mention for Auto-only scanners |
| `docs/index.md` | Replace Tutorials intro with Getting Started intro, update link from tutorial to getting-started |
| `mkdocs.yml` | Add Getting Started nav section, remove/update Tutorials section |

### Files to DELETE
| File | Reason |
|------|--------|
| `docs/tutorials/scan-your-first-document.md` | Relocated to getting-started/ |
| `docs/tutorials/` directory | Empty after relocation (or keep if Tutorials section remains as placeholder) |

### Cross-Link Impact
Only `docs/index.md` links to `tutorials/scan-your-first-document.md`. Other pages link to how-to guides which are not moving. Low cross-link risk.

## Web UI Elements (for first-web-ui-scan.md accuracy)

Based on the actual template (`index.html`), the web UI has these elements to document:

1. **Profile dropdown** (`<select name="profile">`) -- lists all configured profiles
2. **Title input** (`<input name="title">`) -- placeholder "Document title (auto-generated if empty)"
3. **Tags multi-select** (`<select name="tags" multiple>`) -- loaded from paperless-ngx via HTMX, with refresh button
4. **Correspondent select** (`<select name="correspondent">`) -- loaded from paperless-ngx via HTMX, with "No correspondent" default, with refresh button
5. **Scan button** -- disabled with "Scanning..." text while job is active
6. **Status area** (partial template) -- shows job state (scanning/assembling/uploading/done/error), first-page thumbnail
7. **Job History table** -- columns: Time, Profile, Title, Status

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | MkDocs build + Playwright MCP |
| Config file | `mkdocs.yml` |
| Quick run command | `uv run mkdocs build --strict` |
| Full suite command | `uv run mkdocs build --strict` + Playwright visual checks |

### Phase Requirements to Test Map
| Behavior | Test Type | Automated Command |
|----------|-----------|-------------------|
| All markdown renders without errors | build | `uv run mkdocs build --strict` |
| No broken internal links | build | `uv run mkdocs build --strict` (reports broken links) |
| Getting Started appears first in nav | visual | Playwright: navigate to docs site, verify nav order |
| Quick Start page renders with tabs | visual | Playwright: navigate to quick-start, verify tab content |
| Web UI Scan page describes all form elements | content review | Read the generated HTML |
| auto_source_mode documented in profiles guide | content | grep docs output |
| paper_size documented in profiles guide | content | grep docs output |

### Wave 0 Gaps
- None -- MkDocs is already configured and builds successfully

## Open Questions

1. **Keep Tutorials nav section or remove it?**
   - Current state: Has one page (being relocated)
   - Recommendation: Remove the Tutorials section entirely. An empty section with "Coming soon" is worse than no section. Re-add it when actual task-based tutorials are written.

2. **How deep to cover auto_source_mode/paper_size in how-to guides?**
   - Recommendation: One subsection each in configure-scan-profiles.md (as shown in Code Examples above), plus a one-paragraph mention in set-up-adf-duplex.md. Link to Configuration reference for full details. This avoids duplicating reference content while making the features discoverable.

3. **Should Quick Start reference pipx or uv for bare metal?**
   - The bare metal how-to uses pipx. The project itself uses uv. Recommendation: Use pipx for end-user installation (matching existing how-to), since uv is for development, not end-user package installation.

## Sources

### Primary (HIGH confidence)
- `mkdocs.yml` -- current nav structure, theme config, extensions (read directly)
- `docs/tutorials/scan-your-first-document.md` -- existing 136-line tutorial (read directly)
- `docs/how-to/configure-scan-profiles.md` -- existing profile guide (read directly)
- `docs/how-to/set-up-adf-duplex.md` -- existing ADF guide (read directly)
- `docs/reference/configuration.md` -- full config reference with auto_source_mode and paper_size (read directly)
- `src/saneless/web/templates/index.html` -- actual web UI template (read directly)
- `docs/index.md` -- landing page with cross-links (read directly)
- `docs/how-to/deploy-docker-compose.md` -- Docker deployment guide (read directly)

### Secondary (MEDIUM confidence)
- Phase 15, 16, 18 CONTEXT.md files (referenced but not fully re-read; decisions summarized in 19-CONTEXT.md)

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- no new tools, everything already configured
- Architecture: HIGH -- nav changes are straightforward, file moves are well-scoped
- Pitfalls: HIGH -- cross-link risk is minimal (only 1 file links to relocated tutorial)

**Research date:** 2026-03-26
**Valid until:** 2026-04-26 (stable -- MkDocs Material is mature)
