# Phase 05 — UI Review

**Audited:** 2026-03-22
**Baseline:** Abstract 6-pillar standards (no UI-SPEC.md present)
**Screenshots:** Not captured (no dev server detected; Bash tool unavailable in this environment)

---

## Audit Scope Note

Phase 05 is a backend integration phase: it adds the `saneless serve` CLI command and the Dockerfile `CMD ["serve"]` instruction. No new UI components, templates, or styles were introduced in this phase. The audit therefore covers the full web UI as-built through phase 05, evaluating all HTML templates and the app.css that back the served application.

---

## Pillar Scores

| Pillar | Score | Key Finding |
|--------|-------|-------------|
| 1. Copywriting | 4/4 | All labels are specific and action-oriented; placeholder text is descriptive; empty/error states use plain language |
| 2. Visuals | 3/4 | Clear hierarchy and purposeful SVG flip illustration; page title "saneless" lacks a descriptive subtitle or tagline |
| 3. Color | 4/4 | Zero hardcoded hex/rgb values; all color references use PicoCSS design tokens; primary accent used sparingly and purposefully |
| 4. Typography | 3/4 | PicoCSS handles the scale well; two raw rem font-size overrides (0.9rem, 0.85rem) sit outside the design system's named scale |
| 5. Spacing | 3/4 | Mostly consistent rem-based spacing; a handful of ad-hoc values (0.2rem, 0.5rem, 3px) deviate from a clean scale |
| 6. Experience Design | 4/4 | All five active job states have distinct UI messages; error state uses role="alert"; empty history state handled; scan button disabled with aria-busy during active jobs |

**Overall: 21/24**

---

## Top 3 Priority Fixes

1. **Page title lacks context** — A new user seeing the browser tab or `<h1>saneless</h1>` has no immediate understanding of what the application does. Add a subtitle or tagline in the header, e.g. `<p class="subtitle">Document scanner for paperless-ngx</p>`, and update `<title>` to `saneless — Document Scanner`. Impact: first-visit clarity without layout changes.

2. **Refresh buttons have no visible label** — The Tags and Correspondent refresh buttons render as a bare Unicode arrow (&#x21bb;) with only a `title` attribute for tooltip text. On touch devices `title` tooltips do not appear; screen readers will read the raw Unicode character. Add a visually-hidden `<span class="visually-hidden">Refresh</span>` inside each refresh button alongside the icon. Impact: accessibility on mobile and for assistive technology users.

3. **Font-size overrides bypass the design system** — `app.css` lines 48 and 78 set raw `font-size: 0.9rem` and `font-size: 0.85rem` on `.refresh-btn` and history table cells respectively, instead of using PicoCSS's `--pico-font-size` or a semantic `<small>` element. This creates two undocumented type sizes outside the otherwise clean PicoCSS scale. Replace `.refresh-btn { font-size: 0.9rem }` with a `<small>` wrapper or remove the override if the PicoCSS button default is acceptable. Replace the media-query `font-size: 0.85rem` with `font-size: var(--pico-font-size-sm)` or equivalent PicoCSS token if available. Impact: type consistency across viewport sizes.

---

## Detailed Findings

### Pillar 1: Copywriting (4/4)

All user-facing strings are specific and purposeful:

- Scan form labels: "Profile", "Title", "Tags", "Correspondent" — unambiguous
- Title input placeholder: "Document title (auto-generated if empty)" — tells the user exactly what happens when the field is left blank
- Scan button: dynamically switches between "Scan" and "Scanning..." — reflects real state
- Status messages: "Starting scan...", "Scanning...", "Assembling PDF...", "Uploading to paperless-ngx..." — each distinct and informative
- Flip prompt (`partials/flip.html`): full instructional prose with correct/incorrect diagrams and clear CTA labels "Continue" / "Cancel" — the only instance of "Cancel" found, which is contextually correct here (aborting a two-sided scan)
- Error state: displays `{{ job.error }}` verbatim — raw error messages may be technical, but this is acceptable for a single-operator internal tool
- History empty state (`partials/history.html` line 13): "No scan history yet." — friendly, complete sentence

No generic "Submit", "OK", "Click Here" labels found.

### Pillar 2: Visuals (3/4)

Strengths:
- PicoCSS `container` class provides consistent horizontal rhythm
- Three-section page layout (scan form, status area, history) is clearly delineated with `<article>` landmarks
- Flip illustration includes two SVGs with correct/incorrect indicators (checkmark vs X) and caption labels, providing visual reinforcement of the physical instruction
- Status area uses a left border accent (`border-left: 3px solid var(--pico-primary)`) that gives it distinct visual weight without heavy decoration
- Thumbnail preview has a defined max-size constraint (200×300px) preventing layout disruption

Issues:
- The `<h1>saneless</h1>` header provides no contextual orientation. Users arriving at the URL see a single lowercase word with no tagline. For an internal tool this is a minor concern but costs one point.
- The page `<title>` is identical: just "saneless". Browser history and tab labels provide no additional context.
- No favicon is declared, leaving the browser default in place.

### Pillar 3: Color (4/4)

`app.css` (88 lines) contains zero hardcoded hex or `rgb()` values. All color decisions delegate to PicoCSS custom properties:

- `var(--pico-primary)` — accent color on status area border and refresh button
- `var(--pico-primary-hover)` — hover state for refresh button
- `var(--pico-ins-color, green)` — done/success state with a fallback
- `var(--pico-del-color, red)` — error state with a fallback
- `var(--pico-muted-border-color)` — thumbnail border

The fallback values (`green`, `red`) are present as safety nets, not primary declarations, which is correct practice. Primary accent usage is limited to two elements (status area left border, refresh button text) — well within the 10-element threshold for overuse.

`data-theme="auto"` in `<html>` means the color scheme adapts to the OS preference, enabling dark mode at no additional cost.

### Pillar 4: Typography (3/4)

PicoCSS manages the base type scale; the application adds only two overrides:

- `app.css:48` — `.refresh-btn { font-size: 0.9rem }` — a raw rem value not referencing a design token
- `app.css:78` — `#history-body td { font-size: 0.85rem }` inside the mobile breakpoint — another raw value

HTML typography is semantically correct: `<h1>` for the page header, `<h2>` for "Job History", `<small>` for flip illustration captions (`<p><small>Long edge (correct)</small></p>`). No instances of faux-bold via `font-weight` overrides were found; PicoCSS handles weight. The `<small>` usage in flip captions is correct semantic HTML.

The two raw font-size values create a minor inconsistency with no design-system anchor, docking one point.

### Pillar 5: Spacing (3/4)

Spacing uses rem units throughout, which is good. Values observed:

- `1rem` — status area margin/padding (consistent with PicoCSS base unit)
- `0.5rem` — thumbnail margin-top, refresh button margin-left
- `2rem` — flip illustration gap (desktop)
- `1rem` — flip illustration gap (mobile)
- `0.2rem 0.5rem` — refresh button padding
- `3px` — status area border-left width (raw pixel value, not a rem multiple)

The `0.2rem` and the `3px` values are the notable deviations. `0.2rem` is an unusually fine increment that does not align with a 4px or 8px base grid. The `3px` border is a raw pixel value that breaks the rem-only spacing approach. These are small issues — the overall layout is clean — but they prevent a perfect score.

No arbitrary bracket-syntax values (Tailwind-style) are present because this project uses PicoCSS + custom CSS rather than Tailwind.

### Pillar 6: Experience Design (4/4)

State coverage is thorough:

Loading states:
- PENDING: `<p aria-busy="true">Starting scan...</p>`
- SCANNING: `<p aria-busy="true">Scanning...</p>`
- AWAITING_FLIP: full flip prompt with SVG diagrams and action buttons
- ASSEMBLING: `<p aria-busy="true">Assembling PDF...</p>`
- UPLOADING: `<p aria-busy="true">Uploading to paperless-ngx...</p>`

All active states use `aria-busy="true"` on the relevant paragraph, which PicoCSS renders as a spinner. The scan button is disabled with `aria-busy="true"` while any active state is in progress (index.html lines 56-57), preventing double-submission.

Success state:
- DONE: checkmark character with job title displayed; history table auto-refreshes via HTMX load trigger; scan button re-enabled via inline script

Error state:
- ERROR: `role="alert"` on the error paragraph (correct ARIA usage); error message surfaced verbatim; history table auto-refreshes; scan button re-enabled

Empty state:
- No active job: "Ready to scan." — clear, no confusion
- No history: "No scan history yet." — friendly

HTMX polling: the status div polls `/api/jobs/current/status` every 1 second only when a job is in an active state, avoiding unnecessary polling when idle. This is a good interaction design decision.

The flip abort button uses the label "Cancel" which is contextually appropriate (aborting a multi-page scan). Destructive-action confirmation (abort destroys the first-side scan) is handled implicitly by the flip prompt's framing, though there is no explicit "Are you sure?" confirmation modal. For a tool of this scope this is acceptable.

Registry audit: no shadcn/components.json present. Registry audit skipped.

---

## Files Audited

- `/home/kris/git/saneless/src/saneless/web/templates/base.html`
- `/home/kris/git/saneless/src/saneless/web/templates/index.html`
- `/home/kris/git/saneless/src/saneless/web/templates/partials/status.html`
- `/home/kris/git/saneless/src/saneless/web/templates/partials/history.html`
- `/home/kris/git/saneless/src/saneless/web/templates/partials/flip.html`
- `/home/kris/git/saneless/src/saneless/web/templates/partials/tags.html`
- `/home/kris/git/saneless/src/saneless/web/templates/partials/correspondents.html`
- `/home/kris/git/saneless/src/saneless/web/static/app.css`
- `/home/kris/git/saneless/src/saneless/web/app.py` (for route and state structure context)
- `/home/kris/git/saneless/.planning/phases/05-web-server-launch/05-01-SUMMARY.md`
- `/home/kris/git/saneless/.planning/phases/05-web-server-launch/05-CONTEXT.md`
