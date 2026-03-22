# Phase 7 — UI Review

**Audited:** 2026-03-22
**Baseline:** Abstract 6-pillar standards (no UI-SPEC.md)
**Screenshots:** Not captured (no dev server detected; Bash tool unavailable for port check)

---

## Pillar Scores

| Pillar | Score | Key Finding |
|--------|-------|-------------|
| 1. Copywriting | 3/4 | Error state exposes raw exception strings; "-- None --" is informal |
| 2. Visuals | 3/4 | History table leaks ALL_CAPS enum values (DONE, ERROR) to users |
| 3. Color | 4/4 | Exclusively PicoCSS tokens, no hardcoded colors, semantic done/error split |
| 4. Typography | 4/4 | Two contextually justified overrides; all else delegated to PicoCSS |
| 5. Spacing | 4/4 | Consistent rem-based scale throughout, no arbitrary pixel values |
| 6. Experience Design | 3/4 | Known scan-button-disabled bug persists; JS in partials re-enables button fragilely |

**Overall: 21/24**

---

## Top 3 Priority Fixes

1. **Raw exception strings in error state** — Users see internal messages like "Connection refused" or Python tracebacks in `partials/status.html:27` — Replace `{{ job.error }}` with a user-friendly prefix: `"Scan failed: {{ job.error }}"`, and consider truncating long strings. For FEEDER/CONFIG/SCANNER/UPLOAD categories (now available via `job.error_category`), provide category-specific guidance (e.g., "Check the paper feeder and try again" for FEEDER errors). The `ErrorCategory` enum added in Plan 01 is not yet surfaced in the UI.

2. **History table exposes raw enum values** — `partials/history.html:8` renders `job.state.value` which produces ALL_CAPS strings like DONE, ERROR, SCANNING — Change to title-case display labels using a Jinja2 mapping or `.replace("_", " ").title()`: `{{ job.state.value | replace("_", " ") | title }}`, producing "Done", "Error", "Awaiting Flip".

3. **Scan button re-enabled via inline JS in HTMX partials** — `partials/status.html:23-24` and `:30-31` use `<script>` blocks inside HTMX-swapped partials to find and mutate `#scan-btn` — This is fragile: if the DOM structure changes or HTMX replaces the parent, the script executes but the button reference may be stale. Move this logic to a named HTMX event (`htmx:afterSwap`) in `base.html` or `app.css`-anchored JS, or use HTMX response headers (`HX-Trigger`) to fire a `scanComplete` event.

---

## Detailed Findings

### Pillar 1: Copywriting (3/4)

**Passing:**
- "Scan" CTA is appropriately terse for a single-purpose tool — no ambiguity
- "Scanning..." in-progress label correctly mirrors the scan-button state
- "Ready to scan." idle state is clear and actionable
- "No scan history yet." empty history state is complete and friendly
- "Document title (auto-generated if empty)" placeholder gives useful guidance
- Status messages "Starting scan...", "Assembling PDF...", "Uploading to paperless-ngx..." are specific and accurate
- Flip prompt instructions ("Keep the pages in the same order... flip the stack over the long edge") are technically precise

**Issues:**
- `partials/status.html:27` — Error state is `Error: {{ job.error }}` which renders raw Python exception messages (e.g., "Error: [Errno 111] Connection refused"). No user-friendly framing, no guidance on remediation. The `error_category` field added in Plan 01 (`job.error_category`) is available but not used in the template.
- `partials/correspondents.html:1` — "-- None --" uses informal double-dash delimiter convention. Prefer "None" or "No correspondent" for cleaner rendering.
- "Cancel" on the flip prompt (`partials/flip.html:43`) is contextually appropriate here (the alternative to "Continue" is genuinely cancel), so this is not flagged.

---

### Pillar 2: Visuals (3/4)

**Passing:**
- Clear page structure: `<header>` for branding, `<main>` for content, PicoCSS `<article>` cards as visual sections
- Flip illustrations use inline SVG with `aria-label` attributes — accessible and no external image dependency
- SVG diagrams show correct/incorrect technique with checkmark and X indicators — strong instructional design
- Status area has a left-border accent (`border-left: 3px solid var(--pico-primary)`) providing a persistent visual anchor
- No icon-only buttons without accessible labels
- Profile dropdown and title input have explicit `<label for>` associations — good form hierarchy

**Issues:**
- `partials/history.html:8` — Status column shows raw enum values: DONE, ERROR, SCANNING, AWAITING_FLIP. ALL_CAPS is a code convention, not a display convention. Users encounter "AWAITING_FLIP" instead of "Awaiting Flip".
- `partials/status.html:27` — Error indicator uses `&#10007;` (✗) which is a light Unicode cross. Depending on font rendering this may appear faint. Consider `role="alert"` (already present — good) paired with a more visible indicator or PicoCSS `data-theme` color class.
- No visual differentiation between the scan form section and the history section beyond the `<article>` card border — acceptable given PicoCSS defaults but the two sections compete for attention equally.

---

### Pillar 3: Color (4/4)

**Passing:**
- Zero hardcoded hex or RGB values anywhere in `app.css` or templates
- All colors reference PicoCSS custom properties: `--pico-primary`, `--pico-primary-hover`, `--pico-ins-color`, `--pico-del-color`, `--pico-muted-border-color`, `--pico-border-radius`
- `--pico-ins-color` (green semantics) for DONE state and `--pico-del-color` (red semantics) for ERROR state is the correct semantic mapping
- `data-theme="auto"` on `<html>` enables OS-level dark/light mode without custom CSS — correct approach
- Primary accent (`--pico-primary`) used in three well-justified places: status area left-border, refresh button icon color, refresh button hover
- One `!important` on `overflow-x: auto` in `.history-table-wrap` — justified to override PicoCSS article padding, not a color concern

Registry audit: No `components.json` found — shadcn not initialized. Registry safety audit skipped.

---

### Pillar 4: Typography (4/4)

**Passing:**
- No Tailwind typography classes (not a Tailwind project — PicoCSS handles all type)
- Only two font-size overrides in `app.css`:
  - `font-size: 0.9rem` on `.refresh-btn` (line 48) — justified, makes the refresh symbol smaller than surrounding label text
  - `font-size: 0.85rem` on `#history-body td` at mobile breakpoint (line 78) — justified responsive reduction
- No font-weight overrides — PicoCSS's default weight scale is used throughout
- Semantic heading hierarchy: `<h1>` for app name in header, `<h2>` for "Job History" section — correct single-page structure
- `<small>` used appropriately for SVG diagram captions in `partials/flip.html:21,33`

---

### Pillar 5: Spacing (4/4)

**Passing:**
- All spacing values in `app.css` use rem units:
  - `margin: 1rem 0` and `padding: 1rem` for status area
  - `margin-top: 0.5rem` for thumbnail
  - `gap: 2rem` for flip illustration (desktop), `gap: 1rem` (mobile)
  - `margin-left: 0.5rem` for refresh button
  - `padding: 0.2rem 0.5rem` for refresh button (small compound value — justified for a compact inline button)
- No arbitrary pixel values or calc() expressions
- Spacing scale follows approximate doubling: 0.2 / 0.5 / 1 / 2rem — coherent
- Responsive breakpoint at 576px reduces flip illustration gap and thumbnail max-width — appropriate

**Minor note:**
- `0.2rem` on the refresh button top/bottom padding is non-standard (not a typical 4/8/16px rem step) but is visually correct for the use case and does not create inconsistency elsewhere.

---

### Pillar 6: Experience Design (3/4)

**Passing:**
- All five active scan states have distinct UI feedback: PENDING ("Starting scan..."), SCANNING ("Scanning..."), ASSEMBLING ("Assembling PDF..."), UPLOADING ("Uploading to paperless-ngx..."), AWAITING_FLIP (flip prompt)
- All active state `<p>` elements use `aria-busy="true"` — screen readers announce activity
- HTMX polling (`hx-trigger="every 1s"`) is applied only when job is in active states — polling auto-stops on DONE/ERROR
- Error state uses `role="alert"` for screen reader announcement
- History empty state handled (`partials/history.html:12-14`)
- Scan button disabled with `aria-busy="true"` during active scan — prevents double-submission
- Flip prompt provides visual illustration (SVG) plus textual instructions — multi-modal guidance

**Issues:**
- **Known bug (deferred):** `partials/index.html:9` — `hx-on::before-request` on the form element fires for all child HTMX requests (tags and correspondents load on page-load). This disables the scan button immediately on page load as a side effect of tags/correspondents fetching. The test was changed from `is_enabled()` to `is_visible()` to work around it, and it was logged to deferred-items.md. No fix was applied in this phase.
- **Fragile button re-enable pattern:** `partials/status.html:23-24` and `:30-31` embed `<script>` blocks in HTMX-swapped HTML to re-enable `#scan-btn`. This couples the partial to the outer page's DOM structure. If HTMX replaces a parent container, the script may execute with a stale reference or in a disconnected context. A more robust approach would use an `htmx:afterSwap` event listener in `base.html` or a custom `HX-Trigger` response header.
- **ErrorCategory not surfaced in UI:** The `error_category` field added in Plan 01 to `Job` is available in templates via `job.error_category`, but `partials/status.html` does not use it. Category-specific error guidance (e.g., "Check the feeder" for FEEDER errors) would meaningfully improve error recovery UX. The infrastructure is built but the UI connection is missing.
- No confirmation dialog before aborting a flip (`partials/flip.html:43`) — the Cancel button immediately posts to `/api/flip/abort`. Aborting mid-scan may discard work. A confirmation would be appropriate given the consequence.

---

## Files Audited

- `/home/kris/git/saneless/src/saneless/web/templates/base.html`
- `/home/kris/git/saneless/src/saneless/web/templates/index.html`
- `/home/kris/git/saneless/src/saneless/web/templates/partials/status.html`
- `/home/kris/git/saneless/src/saneless/web/templates/partials/flip.html`
- `/home/kris/git/saneless/src/saneless/web/templates/partials/history.html`
- `/home/kris/git/saneless/src/saneless/web/templates/partials/tags.html`
- `/home/kris/git/saneless/src/saneless/web/templates/partials/correspondents.html`
- `/home/kris/git/saneless/src/saneless/web/static/app.css`
- `/home/kris/git/saneless/src/saneless/web/routes.py` (referenced)
- `/home/kris/git/saneless/src/saneless/job.py` (referenced — ErrorCategory enum)
- `/home/kris/git/saneless/tests/test_browser.py` (referenced — scan button bug documentation)
