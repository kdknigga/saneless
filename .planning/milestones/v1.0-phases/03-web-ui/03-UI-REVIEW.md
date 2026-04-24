# Phase 3 — UI Review

**Audited:** 2026-03-22
**Baseline:** Abstract 6-pillar standards (no UI-SPEC.md present)
**Screenshots:** Not captured (no dev server detected; Bash tool unavailable — code-only audit)

---

## Pillar Scores

| Pillar | Score | Key Finding |
|--------|-------|-------------|
| 1. Copywriting | 3/4 | Copy is specific and purposeful; one minor issue with the refresh button's icon-only affordance |
| 2. Visuals | 3/4 | Clear hierarchy with PicoCSS classless styling; flip SVG is functional but small and lacks scale labels |
| 3. Color | 4/4 | All color through PicoCSS design tokens, no hardcoded hex values, accent used sparingly |
| 4. Typography | 4/4 | Typography fully delegated to PicoCSS scale; two minor app overrides both purposeful |
| 5. Spacing | 3/4 | Consistent rem-based scale; one `3px` magic number and one `!important` override |
| 6. Experience Design | 4/4 | All states covered: loading, polling, error with role=alert, empty state, disabled scan button |

**Overall: 21/24**

---

## Top 3 Priority Fixes

1. **Refresh button is icon-only with no visible label** — Users who cannot hover (mobile, touch) see only `↻` with no text cue for what it refreshes. The `title` attribute is hover-only. Add visible `<span class="sr-only">Refresh tags</span>` or append a short text label to the button — `↻ Refresh` — so the action is discoverable without hover. Affects `index.html` lines 24–29 and 38–45.

2. **Status area `border-left` uses a `3px` magic number** — The rest of the CSS uses `rem` or PicoCSS tokens consistently. Replace `border-left: 3px` with `border-left: var(--pico-border-width, 3px)` to stay within the token system and respect any theme overrides. Affects `app.css` line 7.

3. **Flip illustration SVG has no visible size guidance for the user** — At 120px wide the diagrams are functional but small. The `<small>` caption ("Long edge (correct)" / "Short edge (incorrect)") is rendered at a reduced size beneath a small SVG, making it harder to read on mobile despite the responsive stacking. Increase SVG display size to 160px minimum, or increase caption font-size to `0.85rem` to match the responsive table body size. Affects `app.css` line 38 and `flip.html` lines 21, 37.

---

## Detailed Findings

### Pillar 1: Copywriting (3/4)

**What's good:**
- All CTA labels are action-specific: "Scan", "Scanning...", "Continue", "Cancel" — no generic "Submit" or "OK".
- Status messages are informative per state: "Starting scan...", "Assembling PDF...", "Uploading to paperless-ngx..."
- The empty history state uses a real sentence: "No scan history yet." (`history.html:13`) — not "No data" or "—".
- The flip prompt uses exact PRD wording at `flip.html:3–5`.
- Title placeholder is helpful: "Document title (auto-generated if empty)" (`index.html:20`).
- Error display uses `{{ job.error }}` — the actual error message surfaces to the user, not a generic string.

**Issues:**
- The two refresh buttons (`index.html:29`, `45`) render the Unicode character `&#x21bb;` (↻) with only a `title` attribute for context. On mobile and touch devices, `title` tooltips do not appear. The button action (invalidate cache and refetch) is not self-evident from the icon alone to a first-time user.
- The `Cancel` button in the flip prompt (`flip.html:43`) is generic for a potentially destructive action. The user abandons the scan job without knowing the job will be left in an error state. Consider "Abort scan" or adding a `hx-confirm` dialog.

---

### Pillar 2: Visuals (3/4)

**What's good:**
- Clear page hierarchy: `<h1>saneless</h1>` in the header, `<h2>Job History</h2>` in the history section. Only two heading levels used — appropriate for a single-page app.
- PicoCSS `<article>` cards provide visual grouping for the scan form and history table without any extra markup.
- The status area has a left-border accent (`border-left: 3px solid var(--pico-primary)`) that draws the eye as a focal element.
- The flip illustration uses two side-by-side SVGs with captions — the correct/incorrect framing is clear.
- The DONE checkmark (✓) and ERROR cross (✗) are Unicode characters embedded directly in text — they read cleanly.
- `data-theme="auto"` on `<html>` enables native dark/light mode switching via `prefers-color-scheme` with no extra code.

**Issues:**
- The flip SVGs are 120px wide. On a desktop viewport at 1440px, two 120px diagrams side by side inside a centered `<main class="container">` will appear small relative to the surrounding form content. No minimum height is set on the SVG.
- The `<h1>` in the header ("saneless") and the `<h2>` in the scan form section are the only structural landmarks. The scan form `<article>` has no heading, so screen readers and keyboard navigators have no way to identify its purpose without reading the form fields. Adding an `<h2>Scan</h2>` or `<legend>` inside the `<article>` would improve both accessibility and visual scannability.
- The history table has no visible row hover state beyond PicoCSS defaults, which is minor for a read-only table.

---

### Pillar 3: Color (4/4)

**What's good:**
- Zero hardcoded hex values or `rgb()` calls anywhere in the codebase. Grep confirmed clean.
- All color is via PicoCSS design tokens: `var(--pico-primary)`, `var(--pico-ins-color, green)`, `var(--pico-del-color, red)`, `var(--pico-muted-border-color)`, `var(--pico-border-radius)`.
- The fallback values in `var(--pico-ins-color, green)` and `var(--pico-del-color, red)` are safety nets — PicoCSS defines these variables so the fallbacks are for extreme edge cases only.
- Accent color (`--pico-primary`) is used on only two elements: the status area left border and the refresh button text. Not overused.
- All SVG strokes use `currentColor`, making them compatible with both light and dark mode without duplication.
- The `class="secondary"` on the Cancel button uses PicoCSS's semantic secondary style — no custom color assignment needed.

---

### Pillar 4: Typography (4/4)

**What's good:**
- Typography is almost entirely delegated to PicoCSS's type scale. The app does not define its own font family, base size, or line-height.
- Only two font-size overrides in `app.css`: `0.9rem` for the refresh button (line 48) and `0.85rem` for mobile table cells (line 78). Both are intentional and purposeful.
- The heading hierarchy is well-controlled: `h1` → `h2` only, with `<small>` used correctly inside SVG captions (semantic small print).
- No font-weight overrides. PicoCSS handles weight for headings/body, and no custom `font-weight` properties are set.
- No text-transform or letter-spacing overrides that could break readability.

**Minor note:** `font-size: 0.9rem` on `.refresh-btn` is a custom value rather than a PicoCSS scale step, but given it applies to a decorative icon button, this is acceptable.

---

### Pillar 5: Spacing (3/4)

**What's good:**
- All spacing uses `rem` units: `1rem`, `0.5rem`, `0.2rem`, `2rem`, `1rem`. No arbitrary `px` values in the spacing properties (margin/padding/gap).
- PicoCSS design tokens are used for border-radius: `var(--pico-border-radius)`.
- Consistent rhythm: status area uses `margin: 1rem 0; padding: 1rem`, thumbnail uses `margin-top: 0.5rem`, flip illustration uses `gap: 2rem`.
- Responsive breakpoint at `576px` with adjusted gap and font-size — consistent with a mobile-first approach.

**Issues:**
- `border-left: 3px` on the status area (`app.css:7`) uses a raw pixel value. This is a dimension, not spacing per se, but the `3px` is a magic number not derived from any token. Using `var(--pico-border-width, 3px)` would align it with the token system.
- `overflow-x: auto !important` on `.history-table-wrap` (`app.css:61`) requires `!important` to override PicoCSS's article overflow defaults. This suggests a specificity conflict. The fix is to either increase selector specificity (e.g. `article.history-table-wrap`) or remove the wrapping `article` and use a plain `div`. Using `!important` for layout properties is a maintenance risk.
- `width: 120px` on `.flip-illustration svg` (`app.css:38`) is a fixed pixel size. On high-DPI or very narrow screens, a fluid width like `min(120px, 40%)` or simply `max-width: 120px` would be more robust.

---

### Pillar 6: Experience Design (4/4)

**What's good:**
- Loading states: All five active job states have explicit UI: PENDING shows "Starting scan...", SCANNING shows "Scanning...", ASSEMBLING shows "Assembling PDF...", UPLOADING shows "Uploading to paperless-ngx..." — each with `aria-busy="true"` for screen readers.
- Error state: ERROR state shows the error message in a `<p role="alert">` — correctly triggers ARIA live region announcement. The error text is the actual exception message, not a generic string.
- Empty state: The history table shows "No scan history yet." when no jobs exist (`history.html:13`). The status area shows "Ready to scan." when no job is active (`status.html:38`).
- Disabled states: The scan button is disabled server-side (on initial page load) AND client-side (via `hx-on::before-request`) before the server responds. Double coverage prevents double-submit.
- Polling stops automatically on terminal states (DONE/ERROR) — the `hx-trigger="every 1s"` attribute is only rendered when `job.state.value in active_states`. No polling leak.
- Auto-refresh of history table after job completion: hidden `<div hx-trigger="load">` inside DONE/ERROR blocks triggers a history refresh automatically.
- Graceful degradation: Tags/correspondents fetch failure returns `[]` with a warning log, not a 500. The page always loads.
- No error boundaries needed (server-side rendering — template errors surface as 500s handled by FastAPI's default error handling).

**Minor note:** The `Cancel` button in the flip prompt has no confirmation. The job will be placed in an ERROR state if aborted. While this is documented behavior, a `hx-confirm="Abort this scan job?"` would prevent accidental cancellation. This is a UX preference, not a hard defect.

---

## Registry Safety

No `components.json` found — shadcn not initialized. Registry audit skipped.

---

## Files Audited

- `src/saneless/web/templates/base.html`
- `src/saneless/web/templates/index.html`
- `src/saneless/web/templates/partials/status.html`
- `src/saneless/web/templates/partials/flip.html`
- `src/saneless/web/templates/partials/history.html`
- `src/saneless/web/templates/partials/tags.html`
- `src/saneless/web/templates/partials/correspondents.html`
- `src/saneless/web/static/app.css`
- `src/saneless/web/routes.py`
- `.planning/phases/03-web-ui/03-CONTEXT.md`
- `.planning/phases/03-web-ui/03-01-SUMMARY.md`
- `.planning/phases/03-web-ui/03-02-SUMMARY.md`
- `.planning/phases/03-web-ui/03-02-PLAN.md`
