# Phase 6 — UI Review

**Audited:** 2026-03-22
**Baseline:** Abstract 6-pillar standards (no UI-SPEC.md present)
**Screenshots:** Not captured (no dev server detected; code-only audit)

---

## Pillar Scores

| Pillar | Score | Key Finding |
|--------|-------|-------------|
| 1. Copywriting | 3/4 | Status messages are specific and clear; history table renders raw enum values ("DONE", "ERROR") instead of human labels |
| 2. Visuals | 3/4 | ASSEMBLING/UPLOADING states use aria-busy spinners consistently; no visual distinction between new intermediate states |
| 3. Color | 4/4 | Exclusively uses PicoCSS design tokens; no hardcoded hex values or inline colors |
| 4. Typography | 4/4 | Minimal, consistent sizing; PicoCSS handles the scale; only two custom font-size overrides (0.9rem, 0.85rem) |
| 5. Spacing | 3/4 | Mostly consistent rem-based spacing via tokens; two arbitrary non-scale values (0.2rem, 0.5rem) in refresh button |
| 6. Experience Design | 4/4 | All seven job states covered; aria-busy on active states; role="alert" on error; empty state for history; responsive CSS |

**Overall: 21/24**

---

## Top 3 Priority Fixes

1. **History table shows raw enum values** — Users see "DONE", "ERROR", "SCANNING" etc. in the history table's Status column. These are internal code identifiers, not user language. Impact: erodes trust and looks unfinished. Fix: map `job.state.value` to display labels in `partials/history.html` (e.g. "Done", "Error", "Scanning", "Assembling PDF", "Uploading").

2. **No UI entry point for the new connection test** — `GET /api/paperless/test` was built but is only reachable via direct HTTP call. Users have no way to verify their paperless-ngx configuration from the UI. Impact: the feature exists but is invisible and untestable without curl. Fix: add a "Test connection" button to the form or settings area that calls the endpoint via HTMX and displays the result inline.

3. **Refresh button spacing uses non-scale values** — `.refresh-btn` uses `padding: 0.2rem 0.5rem` and `margin-left: 0.5rem`. These are arbitrary values that fall outside the PicoCSS spacing rhythm (`0.25rem`, `0.5rem` are close but the `0.2rem` is non-standard). Impact: minor visual inconsistency that accumulates with the theme. Fix: change `padding: 0.2rem 0.5rem` to `padding: 0.25rem 0.5rem` to align with the quarter-rem scale.

---

## Detailed Findings

### Pillar 1: Copywriting (3/4)

**Passing:**
- Status partial (`partials/status.html`) uses specific, task-oriented strings: "Starting scan...", "Scanning...", "Assembling PDF...", "Uploading to paperless-ngx..." — all directly reflect what is happening
- ASSEMBLING state (`status.html:16`): "Assembling PDF..." mirrors the pipeline message exactly — clear and accurate
- UPLOADING state (`status.html:18`): "Uploading to paperless-ngx..." — service-specific, not generic
- DONE state (`status.html:20`): "Done: {title}" with checkmark — confirms the specific document, not just "success"
- ERROR state (`status.html:27`): "Error: {job.error}" — shows the actual error message, passes error detail through
- Empty history state (`partials/history.html:13`): "No scan history yet." — contextual, not generic "No results"
- Flip prompt (`partials/flip.html:2-5`): detailed, action-oriented instruction text with illustrated examples

**Issues:**
- `partials/history.html:8`: Status column renders `{{ job.state.value }}` directly, producing uppercase enum strings ("DONE", "ERROR", "SCANNING", "AWAITING_FLIP", "ASSEMBLING", "UPLOADING") in the finished history table. This is an existing pre-Phase-6 issue but Phase 6 adds two new states (ASSEMBLING, UPLOADING) that will now appear in history in raw form.
- `partials/status.html:24` and `status.html:31`: The inline `<script>` blocks that re-enable the scan button use `btn.textContent = "Scan"` — this is fine but the label is not sourced from a single constant, creating a minor maintenance risk if the label changes.

**Generic label check:** No "Submit", "Click Here", "OK", "Cancel", or "Save" patterns found in templates.
The "Cancel" in `partials/flip.html:43` is contextually correct for an abort action during duplex scanning.

---

### Pillar 2: Visuals (3/4)

**Passing:**
- All five active states (PENDING, SCANNING, AWAITING_FLIP, ASSEMBLING, UPLOADING) display `aria-busy="true"` on their containing element, which PicoCSS renders as a spinner animation — users get visual feedback that work is in progress
- AWAITING_FLIP state shows a dedicated illustrated UI (`partials/flip.html`) with SVG diagrams, correct/incorrect labeling, and action buttons — this is a strong, purposeful visual design for a complex user action
- Thumbnail preview renders inline in the status area with constrained max dimensions and a muted border — clean, non-intrusive
- Responsive CSS in `app.css:66-87` handles mobile layout for the flip illustration and history table

**Issues:**
- ASSEMBLING and UPLOADING states (`status.html:16,18`) are visually identical to SCANNING (`status.html:12`) — all three show a plain `<p aria-busy="true">` with different text. A user watching the status area during a job will see the text swap but no other visual change distinguishing pipeline phases. Low severity but a missed opportunity for progressive indication.
- The connection test endpoint (`GET /api/paperless/test`) has no UI surface at all — no button, no status indicator, no indicator of configuration state. The endpoint is only accessible via direct HTTP calls.

---

### Pillar 3: Color (4/4)

All color references use PicoCSS design tokens:
- `app.css:8`: `var(--pico-primary)` for the status area left border accent
- `app.css:13`: `var(--pico-ins-color, green)` for DONE state (with fallback)
- `app.css:17`: `var(--pico-del-color, red)` for ERROR state (with fallback)
- `app.css:25`: `var(--pico-muted-border-color)` for thumbnail border
- `app.css:52`: `var(--pico-primary)` for refresh button
- `app.css:56`: `var(--pico-primary-hover)` for refresh button hover

No hardcoded hex values found anywhere in templates or CSS. The `#[0-9a-fA-F]` pattern match in `status.html:20` and `status.html:27` was the HTML entity references `&#10003;` (checkmark) and `&#10007;` (ballot X) — not color values. Token usage is consistent and the `data-theme="auto"` in `base.html:2` enables automatic dark/light mode through PicoCSS.

---

### Pillar 4: Typography (4/4)

Typography is almost entirely delegated to PicoCSS defaults. Custom font-size overrides are minimal:
- `app.css:48`: `font-size: 0.9rem` on `.refresh-btn` — reduces refresh button text to slightly below body size, appropriate for a secondary inline control
- `app.css:78`: `font-size: 0.85rem` inside `@media (max-width: 576px)` for history table cells — explicit responsive reduction for tight mobile viewports

Two custom font sizes across the entire codebase, both contextually justified. No font-weight overrides present. PicoCSS handles the heading hierarchy (h1 in base, h2 in index.html). Type scale discipline is excellent.

---

### Pillar 5: Spacing (3/4)

**Standard spacing (rem rhythm):**
- `app.css:5`: `margin: 1rem 0` — standard
- `app.css:6`: `padding: 1rem` — standard
- `app.css:24`: `margin-top: 0.5rem` — standard half-rem
- `app.css:33`: `gap: 2rem` for flip illustration — standard
- `app.css:34`: `margin: 1rem 0` — standard
- `app.css:70`: `gap: 1rem` — standard

**Non-standard values:**
- `app.css:46`: `padding: 0.2rem 0.5rem` on `.refresh-btn` — the `0.2rem` vertical padding is not a standard quarter-rem step (`0.25rem`). This value is 20% of 1rem, outside the typical 4px-baseline scale.
- `app.css:47`: `margin-left: 0.5rem` — standard, no issue.

The two non-standard values are contained in one rule and have no downstream impact, but they represent a minor deviation from spacing discipline.

---

### Pillar 6: Experience Design (4/4)

All seven `JobState` values are handled in the status partial with appropriate UX for each:

| State | UI Treatment |
|-------|-------------|
| PENDING | "Starting scan..." with aria-busy |
| SCANNING | "Scanning..." with aria-busy |
| AWAITING_FLIP | Full flip instruction UI with SVG diagrams and Continue/Cancel buttons |
| ASSEMBLING | "Assembling PDF..." with aria-busy (Phase 6 wired) |
| UPLOADING | "Uploading to paperless-ngx..." with aria-busy (Phase 6 wired) |
| DONE | Checkmark + title, re-enables scan button, refreshes history |
| ERROR | Alert role, X mark, renders actual error message, re-enables scan button |

Additional state handling quality markers:
- Scan button disabled (`disabled aria-busy="true"`) during all active states (`index.html:56`) — prevents double-submit
- HTMX polling only active for active states (`status.html:3`) — stops polling on terminal states, no wasted requests
- History auto-refreshes on DONE and ERROR via hidden `hx-get` trigger (`status.html:21,28`) — table stays current without page reload
- Error state uses `role="alert"` (`status.html:27`) — screen readers announce errors immediately
- Empty history state handled (`history.html:13`) — no blank table body
- Connection test endpoint returns structured JSON error (status 502) rather than HTML 500 — API consumers get machine-readable failures
- ASSEMBLING and UPLOADING properly included in the "active states" set in `index.html:56` — button stays disabled through the full pipeline, not just during scanning

No loading skeletons present, but PicoCSS `aria-busy` spinner serves the same purpose within the app's component library constraint.

---

## Files Audited

- `/home/kris/git/saneless/src/saneless/web/routes.py`
- `/home/kris/git/saneless/src/saneless/worker.py`
- `/home/kris/git/saneless/src/saneless/paperless.py`
- `/home/kris/git/saneless/src/saneless/web/static/app.css`
- `/home/kris/git/saneless/src/saneless/web/templates/base.html`
- `/home/kris/git/saneless/src/saneless/web/templates/index.html`
- `/home/kris/git/saneless/src/saneless/web/templates/partials/status.html`
- `/home/kris/git/saneless/src/saneless/web/templates/partials/history.html`
- `/home/kris/git/saneless/src/saneless/web/templates/partials/flip.html`
- `/home/kris/git/saneless/src/saneless/web/templates/partials/tags.html` (structure verified)
- `/home/kris/git/saneless/src/saneless/web/templates/partials/correspondents.html` (structure verified)
