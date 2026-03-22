---
phase: 12-ui-polish-humanize-enum-labels-add-accessible-button-labels-fix-cli-table-truncation-replace-inline-htmx-scripts-normalize-spacing-to-design-token-grid
plan: 01
subsystem: ui
tags: [jinja2, htmx, picocss, accessibility, aria, css]

requires:
  - phase: 03-web-ui
    provides: "Web UI templates, app.py factory, app.css, status/history partials"
provides:
  - "humanize_state Jinja2 filter for human-readable job state labels"
  - "External app.js with HTMX lifecycle handlers (no inline scripts)"
  - "Accessible refresh buttons with aria-label and sr-only text"
  - "CSS .sr-only utility class"
  - "PicoCSS grid-aligned spacing and design token usage"
affects: []

tech-stack:
  added: []
  patterns: ["Jinja2 custom filter for enum display", "External JS for HTMX lifecycle events", "CSS .sr-only for accessible button labels"]

key-files:
  created: ["src/saneless/web/static/app.js"]
  modified: ["src/saneless/web/app.py", "src/saneless/web/static/app.css", "src/saneless/web/templates/base.html", "src/saneless/web/templates/index.html", "src/saneless/web/templates/partials/status.html", "src/saneless/web/templates/partials/history.html", "src/saneless/web/templates/partials/flip.html", "tests/test_web.py"]

key-decisions:
  - "humanize_state as Jinja2 filter registered on template env rather than template-level macro"
  - "External app.js with IIFE pattern for HTMX event delegation instead of inline scripts"

patterns-established:
  - "Jinja2 custom filters: register on app.state.templates.env.filters in create_app"
  - "HTMX lifecycle: delegate via document-level event listeners in external JS"
  - "Accessible buttons: aria-label + .sr-only span pattern"

requirements-completed: [P12-01, P12-02, P12-04, P12-05]

duration: 3min
completed: 2026-03-22
---

# Phase 12 Plan 01: UI Polish Summary

**humanize_state Jinja2 filter, accessible button labels, external app.js for HTMX events, PicoCSS grid-aligned CSS, and copywriting fixes**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-22T12:38:18Z
- **Completed:** 2026-03-22T12:41:58Z
- **Tasks:** 3
- **Files modified:** 9

## Accomplishments
- Job history table shows "Complete", "Failed", "Scanning" instead of raw DONE/ERROR/SCANNING enum values
- Refresh buttons have aria-label attributes and screen-reader-only text for accessibility
- All inline script tags removed from templates; scan button state managed by external app.js
- CSS spacing normalized to PicoCSS 0.25rem grid with design tokens; no !important overrides
- Scan form has h2 heading, flip prompt says "Abort scan", correspondent shows "No correspondent"
- 9 new tests covering all UI polish behaviors including CSS regression guard

## Task Commits

Each task was committed atomically:

1. **Task 1: Register humanize_state Jinja2 filter and create external app.js** - `ca9462f` (feat)
2. **Task 2: Update all templates -- humanize labels, accessible buttons, extract inline scripts, copywriting fixes** - `573fc38` (feat)
3. **Task 3: Add tests for new behaviors** - `24ea0a1` (test)

## Files Created/Modified
- `src/saneless/web/app.py` - Added _STATE_LABELS dict, humanize_state filter, registered on Jinja2 env
- `src/saneless/web/static/app.js` - New: HTMX lifecycle event handlers for scan button state
- `src/saneless/web/static/app.css` - Added .sr-only class, normalized spacing to 0.25rem grid, design tokens, removed !important
- `src/saneless/web/templates/base.html` - Added app.js script tag
- `src/saneless/web/templates/index.html` - Removed hx-on::, added h2 heading, aria-labels, sr-only spans, No correspondent
- `src/saneless/web/templates/partials/status.html` - Removed inline script blocks from DONE and ERROR branches
- `src/saneless/web/templates/partials/history.html` - Applied humanize_state filter to job.state.value
- `src/saneless/web/templates/partials/flip.html` - Changed Cancel to Abort scan
- `tests/test_web.py` - Added 9 new tests for all UI polish behaviors

## Decisions Made
- humanize_state registered as Jinja2 filter on template env rather than template-level macro for cleaner templates
- External app.js uses IIFE with document-level event listeners for HTMX lifecycle delegation

## Deviations from Plan

None - plan executed exactly as written.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- All UI polish for templates, accessibility, and inline script removal complete
- Ready for phase 12 plan 02 (CLI table truncation fix) or phase completion

---
*Phase: 12-ui-polish*
*Completed: 2026-03-22*
