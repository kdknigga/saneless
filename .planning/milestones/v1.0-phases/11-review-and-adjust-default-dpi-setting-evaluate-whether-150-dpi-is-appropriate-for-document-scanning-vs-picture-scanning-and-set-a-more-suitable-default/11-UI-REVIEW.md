# Phase 11 — UI Review

**Audited:** 2026-03-22
**Baseline:** Abstract 6-pillar standards (no UI-SPEC.md)
**Screenshots:** Not captured — no dev server detected; code-only audit

---

## Audit Scope Note

Phase 11 was a pure backend refactor: it added a `DEFAULT_RESOLUTION = 300`
constant to `src/saneless/config.py` and updated two consumers
(`auto_profiles.py`) to reference it instead of duplicating the magic number
`300`. No HTML templates, CSS files, or frontend logic were modified.

The UI audit therefore evaluates the **existing web UI** (PicoCSS + HTMX) as
it stood before and after Phase 11, because the phase left it untouched. All
findings below are pre-existing conditions unrelated to Phase 11's changes.

---

## Pillar Scores

| Pillar | Score | Key Finding |
|--------|-------|-------------|
| 1. Copywriting | 3/4 | Labels and states are clear; "Cancel" on flip abort is the one generic label |
| 2. Visuals | 3/4 | PicoCSS provides solid hierarchy; flip SVG illustrations are a UI strength |
| 3. Color | 4/4 | All colors use PicoCSS CSS custom properties; zero hardcoded hex/rgb values |
| 4. Typography | 4/4 | Single font scale from PicoCSS; only one explicit override (`0.9rem` on refresh button, `0.85rem` on mobile table) |
| 5. Spacing | 3/4 | Consistent use of `rem` units via CSS variables; one `!important` override on table wrap |
| 6. Experience Design | 4/4 | Full state machine coverage — pending, scanning, flip, assembling, uploading, done, error, empty |

**Overall: 21/24**

---

## Top 3 Priority Fixes

1. **"Cancel" on flip abort uses generic label** — Users who pressed Scan by
   accident need context about what gets cancelled. Change
   `src/saneless/web/templates/partials/flip.html` line 43: replace `Cancel`
   with `Abort scan` to match the destructive action semantics already encoded
   in the route name `/api/flip/abort`.

2. **`!important` override on `.history-table-wrap`** — The declaration
   `overflow-x: auto !important` in `app.css` line 61 signals a specificity
   fight with PicoCSS `article` styles. Remove `!important` and use a more
   specific selector (e.g., `.history-table-wrap`) without the `article`
   prefix, or increase specificity via `.history-table-wrap { overflow-x: auto
   }` at file end so it wins naturally.

3. **History table shows raw enum values** — `job.state.value` renders machine
   strings like `DONE`, `ERROR`, `AWAITING_FLIP` directly in the Status column
   of the history table (`partials/history.html` line 7). These are exposed to
   end users without humanization. Add a Jinja2 filter or template macro to map
   enum values to readable labels ("Complete", "Failed", "Waiting for flip").

---

## Detailed Findings

### Pillar 1: Copywriting (3/4)

**Passing:**
- Scan button copy transitions to "Scanning..." during active job — good
  contextual feedback (`index.html` lines 57-57).
- Status messages are descriptive: "Starting scan...", "Scanning...",
  "Assembling PDF...", "Uploading to paperless-ngx..." — each state has a
  unique, human-readable message (`status.html` lines 10-18).
- Empty history state says "No scan history yet." rather than generic "No
  data" (`history.html` line 13).
- Title placeholder "Document title (auto-generated if empty)" sets correct
  expectations (`index.html` line 20).
- Flip instructions are clear and specific (`flip.html` lines 2-6).

**Issues:**
- `Cancel` button on flip prompt (`flip.html` line 43) is generic. In context
  this aborts an in-progress scan with a loaded feeder — the consequence is
  significant. `Abort scan` is more precise.
- `-- None --` as the default correspondent option (`index.html` line 49) is a
  mild convention smell; "No correspondent" reads more naturally to end users
  unfamiliar with form patterns.

### Pillar 2: Visuals (3/4)

**Passing:**
- PicoCSS `article` containers create clear section separation for the scan
  form and history table.
- SVG flip illustrations with aria-labels provide a meaningful visual aid for
  a non-obvious physical operation (`flip.html` lines 11-38). The correct/wrong
  pattern with checkmark/X is a strong UX choice.
- Responsive breakpoint at 576px handles mobile gracefully: flip illustrations
  stack vertically, table text wraps, thumbnail expands to full width
  (`app.css` lines 66-87).
- Status area left-border accent provides visual distinction without consuming
  layout space (`app.css` lines 4-9).

**Issues:**
- No page-level `<meta name="description">` or favicon. Minor for a local tool,
  but the browser tab shows "saneless" with no icon, which looks unfinished
  when docked alongside other tabs (`base.html` lines 5-6).
- The scan form and history table share the same `article` visual weight. A
  subtle elevation or color distinction between the primary action area (form)
  and secondary area (history) would guide first-time users to the scan button
  more quickly.

### Pillar 3: Color (4/4)

All color declarations use PicoCSS custom properties exclusively:
- `var(--pico-primary)` — status area border, refresh button color
- `var(--pico-primary-hover)` — refresh button hover
- `var(--pico-ins-color, green)` — done status (with sensible fallback)
- `var(--pico-del-color, red)` — error status (with sensible fallback)
- `var(--pico-muted-border-color)` — thumbnail border
- `var(--pico-border-radius)` — radius tokens

Zero hardcoded hex values or `rgb()` calls in any template or CSS file. Accent
color (`--pico-primary`) is used on two elements only (status border, refresh
button) — well within the 60/30/10 principle. Dark/light mode is handled
automatically via `data-theme="auto"` on the `<html>` element (`base.html`
line 2).

### Pillar 4: Typography (4/4)

No explicit font-size declarations beyond two:
- `.refresh-btn`: `font-size: 0.9rem` (`app.css` line 49) — one step smaller
  than body to de-emphasize the utility button appropriately.
- Mobile `#history-body td`: `font-size: 0.85rem` (`app.css` line 78) —
  space-saving reduction on small viewports.

All headings (`h1`, `h2`) and body copy delegate entirely to PicoCSS defaults,
giving a consistent single-scale type system. Font weight is not overridden
anywhere. This is a minimal, coherent typography implementation.

### Pillar 5: Spacing (3/4)

**Passing:**
- All spacing in `app.css` uses `rem` units aligned to PicoCSS conventions
  (`0.5rem`, `1rem`, `2rem`).
- `gap: 2rem` on flip illustrations and `margin: 1rem 0` on status area are
  consistent with the PicoCSS base scale.
- Thumbnail spacing (`margin-top: 0.5rem`) is proportional.

**Issues:**
- `overflow-x: auto !important` on `.history-table-wrap` (`app.css` line 61)
  uses `!important` to override `article` PicoCSS box model. This is a
  specificity workaround rather than a clean cascade. The selector
  `article .history-table-wrap` already provides sufficient specificity — the
  `!important` is redundant and fragile against PicoCSS version upgrades.
- `padding: 0.2rem 0.5rem` on `.refresh-btn` (`app.css` line 47) uses
  non-standard fractional value `0.2rem`. PicoCSS's scale uses `0.25rem` as
  its smallest step; `0.2rem` is slightly off-grid. Minimal visual impact but
  inconsistent.

### Pillar 6: Experience Design (4/4)

Full state coverage for the scan workflow:

| State | UI Response |
|-------|-------------|
| PENDING | "Starting scan..." with `aria-busy` |
| SCANNING | "Scanning..." with `aria-busy` |
| AWAITING_FLIP | Full flip prompt with SVG illustrations and Continue/Cancel |
| ASSEMBLING | "Assembling PDF..." with `aria-busy` |
| UPLOADING | "Uploading to paperless-ngx..." with `aria-busy` |
| DONE | Checkmark + title, re-enables scan button via inline script |
| ERROR | Alert role + error message, re-enables scan button |
| No job | "Ready to scan." idle state |
| Empty history | "No scan history yet." empty state |

Additional positive signals:
- Scan button disables with `aria-busy` during active jobs — prevents double
  submission.
- Tags and correspondents load via HTMX on page load with cache-invalidation
  refresh buttons — reduces stale data risk.
- Thumbnail preview on completion gives visual confirmation of what was
  scanned.
- History table auto-refreshes on DONE/ERROR via hidden HTMX trigger.
- `role="alert"` on error paragraph ensures screen readers announce errors
  immediately.

The one remaining gap is that raw enum values appear in the history table
Status column (see Priority Fix 3), but the live status area properly
humanizes all states.

---

## Files Audited

**Phase 11 modified files (direct audit):**
- `src/saneless/config.py` — DEFAULT_RESOLUTION constant, ProfileConfig
- `src/saneless/auto_profiles.py` — constant import and usage
- `tests/test_config.py` — TestDefaultResolution class
- `tests/test_auto_profiles.py` — TestPickClosestResolution constant usage

**Web UI files (pre-existing, audited for full context):**
- `src/saneless/web/templates/base.html`
- `src/saneless/web/templates/index.html`
- `src/saneless/web/templates/partials/status.html`
- `src/saneless/web/templates/partials/history.html`
- `src/saneless/web/templates/partials/flip.html`
- `src/saneless/web/static/app.css`

Registry audit: shadcn not initialized — skipped.
