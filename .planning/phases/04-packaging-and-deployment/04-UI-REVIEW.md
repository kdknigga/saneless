# Phase 4 — UI Review

**Audited:** 2026-03-22
**Baseline:** Abstract 6-pillar standards (no UI-SPEC.md)
**Screenshots:** Not captured (Bash tool unavailable; code-only audit)

---

## Audit Scope Note

Phase 4 delivered no new UI surfaces. The deliverables were: `saneless jobs` CLI command, consume-dir fallback robustness, Dockerfile, docker-compose.yml, GitHub Actions release workflow, and pyproject.toml metadata polish. The only user-facing output surfaces audited are:

1. **CLI text output** — `jobs` command table and JSON output added in this phase
2. **Web UI (steady-state)** — the Jinja2/HTMX templates from Phase 3, which Phase 4 did not modify but which constitute the only interactive UI in the product

---

## Pillar Scores

| Pillar | Score | Key Finding |
|--------|-------|-------------|
| 1. Copywriting | 3/4 | CLI output and web strings are clear; one generic "Continue" CTA in flip prompt could be more specific |
| 2. Visuals | 3/4 | Clean PicoCSS baseline with purposeful SVG flip illustration; page title "saneless" is lowercase-generic |
| 3. Color | 4/4 | All colors via PicoCSS custom properties; no hardcoded hex values; accent use is minimal and purposeful |
| 4. Typography | 4/4 | PicoCSS type scale used throughout; no arbitrary font sizes or weights; responsive adjustment on mobile |
| 5. Spacing | 3/4 | Consistent rem-based spacing via PicoCSS and app.css; one non-standard value (`0.9rem` font-size on refresh button) |
| 6. Experience Design | 3/4 | All five job states handled with live polling; inline script for button reset is fragile; no page-title feedback during active scan |

**Overall: 20/24**

---

## Top 3 Priority Fixes

1. **Inline `<script>` blocks in status.html re-enable the scan button** — If JavaScript is blocked or the HTMX swap races with DOM readiness, the scan button can remain permanently disabled after a DONE or ERROR state. Fix: replace the inline scripts with an `hx-on::after-swap` attribute on the `#status-area` div, or dispatch a custom event that the scan form listens for, eliminating the DOM query brittleness.

2. **`docker-compose.yml` ships with `SANELESS_PAPERLESS__TOKEN=changeme`** — This placeholder credential in a version-controlled file creates a risk that operators copy it verbatim without substitution. The value will be stored in shell history and container inspect output. Fix: remove the `TOKEN` env var line and replace with a comment instructing operators to set it, or use a secrets file reference (`env_file: .env`).

3. **`<title>` tag is always "saneless"** — During an active scan, the browser tab gives no feedback, making it impossible to tell at a glance (e.g., from another tab) whether a scan is in progress. Fix: add an HTMX out-of-band swap that updates `<title>` to "Scanning... — saneless" when a job is active and restores it on completion.

---

## Detailed Findings

### Pillar 1: Copywriting (3/4)

**CLI `jobs` command — satisfactory:**
- Table headers ("Timestamp", "Profile", "Title", "Status") are clear and match the web UI table headers ("Time", "Profile", "Title", "Status") closely enough.
- `--json` and `--limit` flag help text is concise and accurate.
- Empty state: no explicit empty-table message in CLI (the table header row appears with no rows below it). The web UI handles this correctly with "No scan history yet." in `history.html:13`.

**Web UI strings — mostly good:**
- "Ready to scan." (status.html:38) — informative idle state.
- "Starting scan..." / "Scanning..." / "Assembling PDF..." / "Uploading to paperless-ngx..." — all specific and useful.
- "Done: {{ job.title }}" (status.html:20) — clear success message with document context.
- "Error: {{ job.error }}" (status.html:27) — exposes raw error string; acceptable for a power-user tool but could be jarring.
- "Continue" (flip.html:42) — functional but generic. "Continue Scanning" or "Flip Done — Continue" would reduce cognitive load at a critical step in the duplex scan workflow.
- "Cancel" (flip.html:43) — appropriate; cancels the flip-wait state.
- `placeholder="Document title (auto-generated if empty)"` (index.html:20) — excellent; makes the optional nature clear.
- `-- None --` for correspondent (index.html:49, correspondents.html:1) — acceptable placeholder convention.

**CLI `saneless jobs` empty output:**
- When the job store is empty, the CLI renders the header and separator with no rows below. Adding an explicit empty message ("No scan history yet.") would match the web UI behavior and set clearer expectations. This is a minor gap, not scored down from 3 to 2.

### Pillar 2: Visuals (3/4)

**Page identity:**
- `<title>saneless</title>` and `<h1>saneless</h1>` use a lowercase product name that serves as the only identity signal. There is no favicon, no subtitle, and no descriptive tagline. For a utility app this is acceptable, but the lowercase-only branding makes the page feel like a dev prototype to first-time users.

**Visual hierarchy:**
- The scan form `<article>` is the clear focal point at top.
- Status area is visually distinguished with a left border (`border-left: 3px solid var(--pico-primary)`) — a good in-context attention mechanism.
- The flip illustration SVG diagrams (correct vs incorrect) are purposeful and well-executed — clear pedagogical value at a confusing step. `aria-label` attributes are correctly set on both SVGs.

**Refresh buttons:**
- Refresh buttons (↻) use a Unicode character (U+21BB) with `title="Refresh tags"`. The `title` attribute provides tooltip text on hover (desktop), but this is invisible on mobile. An `aria-label` attribute would improve screen reader support.
- `refresh-btn` styling removes border and background, making them visually icon-only. The `title` attribute is the only label — acceptable for a utility tool but worth noting.

**Thumbnail preview:**
- `max-width: 200px` on `.thumbnail` is appropriate for a sidebar-style preview.

**Mobile responsiveness:**
- `@media (max-width: 576px)` breakpoint flips the flip-illustration to column layout and adjusts table font size — good responsive coverage.

### Pillar 3: Color (4/4)

All color declarations use PicoCSS custom properties:
- `var(--pico-primary)` — used on status area left border and refresh button foreground/hover
- `var(--pico-ins-color, green)` — success status text with fallback
- `var(--pico-del-color, red)` — error status text with fallback
- `var(--pico-muted-border-color)` — thumbnail border
- `var(--pico-border-radius)` — consistent radius tokens

No hardcoded hex values (`#rrggbb`) or `rgb()` calls anywhere in app.css or templates. The `data-theme="auto"` attribute on `<html>` enables automatic dark/light mode via PicoCSS — no extra effort required from the application layer.

Accent color (`--pico-primary`) appears in exactly two places (status area border, refresh button), both functional and non-decorative. The 60/30/10 color split is implicitly delegated to PicoCSS defaults, which is appropriate.

### Pillar 4: Typography (4/4)

Font sizing in app.css:
- `font-size: 0.9rem` — one instance, on `.refresh-btn` (intentionally smaller inline button)
- `font-size: 0.85rem` — one instance, in `@media (max-width: 576px)` history table adjustment

All other sizing deferred to PicoCSS. No arbitrary `px` font sizes, no hardcoded `font-family`, no conflicting weight declarations. The `<small>` element in flip.html (`<p><small>Long edge (correct)</small></p>`) uses a semantic HTML size indicator rather than a class — correct approach.

The 0.9rem value on `.refresh-btn` is a deliberate design choice (small inline icon button), not inconsistency. Only two font sizes appear in application code (0.9rem and 0.85rem), well within the "no more than 4 custom sizes" threshold.

### Pillar 5: Spacing (3/4)

Spacing in app.css uses rem units throughout:
- `margin: 1rem 0` — status area vertical margins
- `padding: 1rem` — status area
- `margin-top: 0.5rem` — thumbnail
- `border: 1px solid` — thumbnail border (px is correct for borders)
- `gap: 2rem` / `gap: 1rem` — flip illustration gap
- `margin: 1rem 0` — flip illustration margin
- `padding: 0.2rem 0.5rem` — refresh button padding
- `margin-left: 0.5rem` — refresh button margin

No Tailwind arbitrary values (not a Tailwind project). The one slight inconsistency: `padding: 0.2rem 0.5rem` on `.refresh-btn` uses 0.2rem (not a standard 4/8/16 rem step). This is minor and visually appropriate for a compact inline button — it does not warrant a score below 3.

All structural layout spacing (between sections, form element stacking, container padding) is delegated to PicoCSS, which maintains internal consistency.

### Pillar 6: Experience Design (3/4)

**State coverage:**
- PENDING, SCANNING, ASSEMBLING, UPLOADING, DONE, ERROR, AWAITING_FLIP — all seven job states have explicit UI representations in status.html.
- Live polling via `hx-trigger="every 1s"` on active states — good.
- Job history auto-refreshes on DONE and ERROR states via out-of-band HTMX load trigger.
- Scan button is disabled with `aria-busy="true"` during active job — prevents double-submission.
- Empty state for job history: "No scan history yet." (history.html:13).

**Loading states:**
- Tags and correspondents load lazily on page load (`hx-trigger="load"`) with no loading indicator shown. If the paperless-ngx connection is slow, the dropdowns appear empty with no feedback. PicoCSS supports `aria-busy="true"` on select elements as a loading indicator — this could be added to the `hx-on::before-request` of the initial load.

**Error states:**
- Upload errors surface as "Error: {raw error message}" in the status area with `role="alert"` — accessible.
- No error state shown if tags or correspondents fail to load (the dropdowns silently remain empty).

**Inline script fragility (priority fix #1):**
- status.html lines 23-25 and 30-32 contain `<script>` blocks that query `document.getElementById("scan-btn")` to reset the button after DONE/ERROR. These run during HTMX swaps of `#status-area`, which does not contain `#scan-btn`. The button re-enable relies on the scan button being present in the DOM at swap time. If a future refactor moves the button into a separate HTMX region, or if the page is partially rendered, the button will stay disabled. An `hx-on::after-swap` on the parent form or a `hx-trigger="htmx:afterSwap from:#status-area"` listener would be more robust.

**Destructive actions:**
- The "Cancel" button in flip.html aborts a running scan job. There is no confirmation dialog. For the target use case (single user, physical scanner present), this is acceptable — the cost of an accidental cancel is low (rescan needed, not data loss).

**Dockerfile CMD default:**
- `CMD ["serve"]` is present in the Dockerfile (line 23) — not in the plan's artifact spec but a meaningful UX improvement: `docker run ghcr.io/.../saneless` works without specifying a subcommand, following the principle of least surprise for container deployments.

---

## Files Audited

**Phase 4 deliverables:**
- `/home/kris/git/saneless/src/saneless/cli.py` — `jobs` command (lines 188-225)
- `/home/kris/git/saneless/src/saneless/paperless.py` — consume dir fallback (lines 156-164)
- `/home/kris/git/saneless/Dockerfile`
- `/home/kris/git/saneless/docker-compose.yml`
- `/home/kris/git/saneless/.github/workflows/release.yml`
- `/home/kris/git/saneless/pyproject.toml`

**Web UI surface (Phase 3, audited as steady-state):**
- `/home/kris/git/saneless/src/saneless/web/templates/base.html`
- `/home/kris/git/saneless/src/saneless/web/templates/index.html`
- `/home/kris/git/saneless/src/saneless/web/templates/partials/status.html`
- `/home/kris/git/saneless/src/saneless/web/templates/partials/history.html`
- `/home/kris/git/saneless/src/saneless/web/templates/partials/flip.html`
- `/home/kris/git/saneless/src/saneless/web/templates/partials/tags.html`
- `/home/kris/git/saneless/src/saneless/web/templates/partials/correspondents.html`
- `/home/kris/git/saneless/src/saneless/web/static/app.css`

**Registry audit:** shadcn not initialized (no `components.json`). Registry audit skipped.
