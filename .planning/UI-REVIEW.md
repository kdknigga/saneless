---
phase: master
slug: project-master-ui-review
audited: 2026-04-20
baseline: .planning/UI-SPEC.md (approved design contract)
screenshots: not captured (no saneless dev server detected; port 8080 hosts unrelated app)
---

# saneless — Master UI Review

**Audited:** 2026-04-20
**Baseline:** `.planning/UI-SPEC.md` — Master Design Contract
**Screenshots:** Not captured (no saneless dev server running; code-only audit)

---

## Pillar Scores

| Pillar | Score | Key Finding |
|--------|-------|-------------|
| 1. Copywriting | 3/4 | Two documented inconsistencies not yet resolved; all other copy matches contract verbatim |
| 2. Visuals | 3/4 | Clear hierarchy and purposeful components; scan button misleadingly shows "Scanning..." during user-action state AWAITING_FLIP |
| 3. Color | 4/4 | Full spec compliance; no hardcoded hex values; 60/30/10 ratio delegated correctly to Pico tokens |
| 4. Typography | 3/4 | Two off-scale font-size micro-overrides documented in spec but not yet normalized |
| 5. Spacing | 4/4 | All spacing values match declared rem scale; one minor hardcoded 1px border documented as acceptable |
| 6. Experience Design | 3/4 | All active states handled; label/select association broken for Tags and Correspondent; AWAITING_FLIP lacks contextual button label |

**Overall: 20/24**

---

## Top 3 Priority Fixes

1. **Tags and Correspondent selects have no programmatic label** — screen readers cannot announce which field is focused, violating WCAG 1.3.1 — add `for="tags-select"` to the Tags `<label>` and `for="correspondent-select"` to the Correspondent `<label>` (`index.html` lines 22 and 39).

2. **Scan button reads "Scanning..." during AWAITING_FLIP** — the user must physically flip paper and press Continue, but the button tells them scanning is in progress, creating a confusing mixed signal — change the button label condition to emit "Waiting..." (or "Awaiting flip...") specifically when `job.state.value == "AWAITING_FLIP"` (`index.html` line 59).

3. **Correspondent "none" option uses two different strings** — `No correspondent` on initial page render (`index.html` line 51) vs `-- None --` after htmx hydration (`partials/correspondents.html` line 1) — standardize to `No correspondent` in both locations as it is the more descriptive form.

---

## Detailed Findings

### Pillar 1: Copywriting (3/4)

The copywriting contract is overwhelmingly met. All status states, headings, table columns, button labels, placeholder text, and aria-labels match the spec verbatim. The style guide (sentence case, ASCII `...` dots, glyph prefixes, no emoji) is consistently applied throughout.

Two documented inconsistencies from the spec remain unresolved:

**Finding 1.1 — Correspondent none-option copy mismatch (minor)**
- `index.html:51` renders `No correspondent`
- `partials/correspondents.html:1` renders `-- None --`
- These are shown to users in the same visual field (the select's first option); after any tag/correspondent cache invalidation, the option text changes visibly. Recommend normalizing to `No correspondent` per the spec's recommendation.

**Finding 1.2 — Busy CTA ellipsis form mismatch (cosmetic)**
- `index.html:59` uses `Scanning...` (three ASCII dots, Jinja server-render path)
- `app.js:21` uses `Scanning\u2026` (U+2026 true ellipsis, JS client path)
- The Jinja path fires on hard page load when a scan is already active; the JS path fires on form submit without page reload. Both are visible. Spec recommends unifying on U+2026.

**Finding 1.3 — Scan button copy does not reflect AWAITING_FLIP state**
- `index.html:59`: all active states including `AWAITING_FLIP` display `Scanning...`
- During `AWAITING_FLIP`, the user must perform a physical action (flip paper); displaying "Scanning..." implies the machine is doing work autonomously. While not a typo, this is a copywriting accuracy issue. (See also Pillar 6.)

No generic labels (`Submit`, `OK`, `Click Here`) found. Empty state copy (`No scan history yet.`, `Ready to scan.`) is appropriately specific.

---

### Pillar 2: Visuals (3/4)

Visual hierarchy is clear: H1 app name in header, two `<article>` cards with H2 headings create a logical scan-then-review reading order. The status area sits between them as a contextual feedback zone, correctly positioned.

**Finding 2.1 — AWAITING_FLIP state: scan button is disabled with a misleading spinner (minor UX)**
During the flip prompt, the primary Scan button shows `aria-busy="true"` and `disabled`, indicating active machine work. But the user must take a physical action. The disabled + spinner state suppresses any affordance for the action the user actually needs to take (flip the paper and press Continue in the flip prompt). This creates a double visual conflict: the flip prompt says "press Continue" while the scan button area signals "waiting for hardware."

**Finding 2.2 — No visual anchor distinguishing the status area from surrounding cards (informational)**
The `#status-area` left border (4px `--pico-primary` accent) is the only visual differentiator from blank whitespace. In the DONE state, the thumbnail and success message appear inside this border-only container — there is no `<article>` wrapper providing the card surface that the Scan and History sections get. This is a deliberate design choice per the spec but may feel slightly inconsistent at a glance.

**Finding 2.3 — Flip SVG illustrations: correct vs incorrect orientation (pass)**
Both SVGs carry `aria-label`, render with `currentColor` for theme compliance, and are paired with visible `<small>` captions. The checkmark/X mark iconography is clear. No issues.

**Finding 2.4 — Icon-only refresh buttons: fully accessible (pass)**
Refresh buttons carry `aria-label`, `title`, and `.sr-only` text — triple coverage per spec. No issues.

---

### Pillar 3: Color (4/4)

Full spec compliance. No hardcoded hex values (`#rrggbb`) or `rgb()` literals exist in any template or CSS file. All color is delegated to PicoCSS semantic tokens.

Accent token (`--pico-primary`) usage matches the four declared spots exactly:
1. `#status-area` left border (`app.css:7`)
2. `.refresh-btn` color (`app.css:52`)
3. Primary `<button type="submit">` (Pico classless default)
4. `aria-busy` spinner (Pico built-in)

Semantic tokens used correctly:
- `.status-done` uses `--pico-ins-color` (`app.css:13`)
- `.status-error` uses `--pico-del-color` (`app.css:17`)
- Thumbnail border uses `--pico-muted-border-color` (`app.css:25`) — the `1px solid` hardcode is the border-width only; color is tokenized. This is consistent with the spec's documented exception.

No dark-mode overrides written; Pico handles both themes automatically. `data-theme="auto"` confirmed in `base.html:2`.

---

### Pillar 4: Typography (3/4)

Four declared type scale tiers (H1, H2, Body 1rem, Small 0.875rem) are correctly in use. No custom fonts loaded. System font stack inherited from Pico as specified.

**Finding 4.1 — Two off-scale font-size overrides not yet normalized**
- `app.css:48`: `.refresh-btn { font-size: 0.9rem }` — 14.4px, between Body and Small tiers. Spec recommends removing this and inheriting `1rem`.
- `app.css:78`: `@media (max-width: 576px) #history-body td { font-size: 0.85rem }` — 13.6px, between Body and Small tiers. Spec recommends normalizing to Small (`0.875rem`) or removing the override entirely.

These are documented in the UI-SPEC as known gaps. Neither represents an undocumented deviation, but both remain unresolved.

Two weights in effective use (400 body, 700 headings) — matches spec.

---

### Pillar 5: Spacing (4/4)

All spacing values in `app.css` map to the four declared rem multiples:

| Value | Spec entry | Location |
|-------|-----------|----------|
| `0.25rem` | declared | `.refresh-btn` padding top/bottom (`app.css:46`) |
| `0.5rem` | declared | `.refresh-btn` padding horizontal, margin-left; `.thumbnail` margin-top (`app.css:46-47, 24`) |
| `1rem` | declared | `#status-area` margin + padding; `.flip-illustration` vertical margin; mobile flip gap (`app.css:5-6, 34, 70`) |
| `2rem` | declared | `.flip-illustration` desktop gap (`app.css:33`) |

Fixed artistic dimensions (`120px` flip SVG, `200px`/`300px` thumbnail) match spec exactly.

No arbitrary `[Npx]` or `[Nrem]` values outside declared scale. No `!important` present (confirmed per spec note from Phase 12).

`.sr-only` uses `1px`/`-1px` per the standard visually-hidden pattern — correctly documented as an a11y technique, not a grid deviation.

Single responsive breakpoint at `576px` matches spec.

---

### Pillar 6: Experience Design (3/4)

State coverage is comprehensive — all six job states (PENDING, SCANNING, AWAITING_FLIP, ASSEMBLING, UPLOADING, DONE, ERROR) render distinct UI. Idle state renders correctly. Empty history state (`No scan history yet.`) is handled. Error state uses `role="alert"` for live-region announcement.

**Finding 6.1 — Tags and Correspondent selects: broken label association (accessibility defect)**
- `index.html:22`: `<label>Tags <button...></button></label>` — the `<label>` wraps the button but has no `for` attribute and does NOT wrap the `<select id="tags-select">` (which is a sibling element, not a child)
- `index.html:39`: Same pattern for Correspondent
- Result: `#tags-select` and `#correspondent-select` have no programmatic label. Screen readers announce them without context. This violates WCAG 1.3.1 (Info and Relationships).
- Fix: add `for="tags-select"` to the Tags label and `for="correspondent-select"` to the Correspondent label. The button embedded inside the label is a separate concern (interactive element inside a label is technically valid HTML but can cause focus issues in some browsers — a `<span>` or adjacent `<div>` would be safer, but the `for` fix alone resolves the label association defect).

**Finding 6.2 — Scan button label during AWAITING_FLIP misleads user about required action**
- `index.html:58-59`: AWAITING_FLIP is grouped with SCANNING/PENDING etc. — button shows `Scanning...` with `aria-busy="true"`
- During AWAITING_FLIP, the machine has stopped; the user must flip paper and press Continue. Showing `aria-busy="true"` and `Scanning...` implies autonomous machine operation.
- Fix: add a specific branch for `AWAITING_FLIP` in the button label condition, e.g., `Waiting for flip...` (or simply omit aria-busy during this state).

**Finding 6.3 — Hidden history-reload div uses inline style (cosmetic)**
- `status.html:21,24`: `<div ... style="display:none">` uses inline style for the hidden htmx reload trigger
- This is functionally correct and the spec acknowledges it, but a CSS class (e.g., `.htmx-hidden`) would be more consistent with the no-inline-style posture.

**Finding 6.4 — No loading state for initial tag/correspondent hydration (informational)**
- Tags and correspondents load via `hx-trigger="load"` from the server. If the API is slow, the selects render empty with no loading indicator.
- Pico's `aria-busy` could be pre-set on the selects until htmx swaps content, but this is not a defect per current spec scope.

**Finding 6.5 — Polling, disabled states, and terminal transitions: all correct (pass)**
- Self-limiting poll (only active states carry the `hx-get` attribute) confirmed in `status.html:3-7`
- Button disable/enable lifecycle in `app.js` is correct and covers both server-render and JS paths
- DONE/ERROR states trigger history reload via hidden div pattern — works correctly

---

## Registry Safety

No shadcn initialized (`components.json` absent). No third-party component registries declared in UI-SPEC.md. Registry audit not applicable.

CDN supply-chain note (inherited from spec): jsDelivr assets for PicoCSS v2 and htmx 2.0.8 lack SRI hashes. This is a known gap documented in UI-SPEC.md, out of scope for this audit cycle.

---

## Files Audited

- `/home/kris/git/saneless/src/saneless/web/templates/base.html`
- `/home/kris/git/saneless/src/saneless/web/templates/index.html`
- `/home/kris/git/saneless/src/saneless/web/templates/partials/status.html`
- `/home/kris/git/saneless/src/saneless/web/templates/partials/history.html`
- `/home/kris/git/saneless/src/saneless/web/templates/partials/tags.html`
- `/home/kris/git/saneless/src/saneless/web/templates/partials/correspondents.html`
- `/home/kris/git/saneless/src/saneless/web/templates/partials/flip.html`
- `/home/kris/git/saneless/src/saneless/web/static/app.css`
- `/home/kris/git/saneless/src/saneless/web/static/app.js`
- `/home/kris/git/saneless/.planning/UI-SPEC.md` (audit baseline)
