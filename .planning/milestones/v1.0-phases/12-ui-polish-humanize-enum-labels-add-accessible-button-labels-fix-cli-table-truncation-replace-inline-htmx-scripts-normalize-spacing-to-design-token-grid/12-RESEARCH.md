# Phase 12: UI Polish - Research

**Researched:** 2026-03-22
**Domain:** Web UI (PicoCSS + HTMX + Jinja2), CLI (Click), Accessibility
**Confidence:** HIGH

## Summary

Phase 12 addresses five distinct UI polish tasks identified across multiple UI audit reviews (Phases 3 and 11). The changes are cosmetic and structural -- no new features, no schema changes, no new dependencies. All issues exist in the current codebase and have been precisely located by file and line number.

The five work items are: (1) humanize raw enum values displayed in the history table and CLI output, (2) add accessible labels to icon-only refresh buttons, (3) fix CLI table truncation when device/job names exceed column widths, (4) replace inline `<script>` tags and `hx-on` attributes with an external JS file, and (5) normalize spacing values to PicoCSS's design token grid. Each is a self-contained change with no cross-dependencies.

**Primary recommendation:** Group into two plans -- one for HTML/CSS/JS changes (items 1, 2, 4, 5) and one for CLI changes (item 3). Both can be verified with existing test infrastructure plus Playwright browser tests.

## Standard Stack

### Core (already in use -- no new dependencies)

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| PicoCSS | 2.x (CDN) | Classless CSS framework | Already in base.html, provides design tokens |
| HTMX | 2.0.8 (CDN) | HTML-driven interactivity | Already in base.html, provides event system |
| Jinja2 | 3.1.6 | Server-side templating | Already renders all templates |
| Click | 8.3.1+ | CLI framework | Already used for all CLI commands |
| FastAPI | 0.135.1+ | Web framework | Already serves all routes |

### No New Dependencies Needed

This phase is pure refactoring of existing templates, CSS, and CLI code. No new packages required.

## Architecture Patterns

### Recommended Project Structure (no changes)

```
src/saneless/web/
    static/
        app.css          # Existing -- spacing fixes go here
        app.js           # NEW -- extracted inline scripts
    templates/
        base.html        # Add <script src="/static/app.js">
        index.html       # Remove hx-on inline handler
        partials/
            status.html  # Remove inline <script> blocks
            history.html # Add humanized labels
```

### Pattern 1: Jinja2 Filter for Enum Humanization

**What:** A custom Jinja2 filter that maps `JobState` enum values to human-readable labels.
**When to use:** Wherever `job.state.value` is rendered to users (history table, status area).
**Example:**

```python
# In web/app.py or a new web/filters.py
_STATE_LABELS: dict[str, str] = {
    "PENDING": "Pending",
    "SCANNING": "Scanning",
    "AWAITING_FLIP": "Waiting for flip",
    "ASSEMBLING": "Assembling",
    "UPLOADING": "Uploading",
    "DONE": "Complete",
    "ERROR": "Failed",
}

def humanize_state(value: str) -> str:
    """Convert a JobState enum value to a human-readable label."""
    return _STATE_LABELS.get(value, value)

# Register on Jinja2 environment:
templates.env.filters["humanize_state"] = humanize_state
```

```html
<!-- In history.html -->
{{ job.state.value | humanize_state }}
```

**Confidence:** HIGH -- standard Jinja2 filter pattern, documented in Jinja2 docs.

### Pattern 2: HTMX Event-Driven Button Reset (Replace Inline Scripts)

**What:** Replace the two inline `<script>` blocks in `status.html` (lines 22-25 and 29-32) and the `hx-on::before-request` in `index.html` (line 9) with HTMX event listeners in an external JS file.

**Current problematic code (status.html lines 22-25, duplicated at 29-32):**
```html
<script>
  var btn = document.getElementById("scan-btn");
  if (btn) { btn.disabled = false; btn.ariaBusy = "false"; btn.textContent = "Scan"; }
</script>
```

**Current problematic code (index.html line 9):**
```html
hx-on::before-request="if(event.detail.elt===this){...}"
```

**Replacement approach -- external app.js:**
```javascript
// app.js -- HTMX event handlers for scan button state management

// Re-enable scan button when status area swaps to a terminal state
document.addEventListener("htmx:afterSwap", function(event) {
    if (event.detail.target && event.detail.target.id === "status-area") {
        var btn = document.getElementById("scan-btn");
        if (!btn) return;
        var area = event.detail.target;
        // Terminal states: contains .status-done or .status-error or "Ready to scan"
        var isDone = area.querySelector(".status-done") !== null;
        var isError = area.querySelector(".status-error") !== null;
        var isReady = area.textContent.indexOf("Ready to scan") !== -1;
        if (isDone || isError || isReady) {
            btn.disabled = false;
            btn.ariaBusy = "false";
            btn.textContent = "Scan";
        }
    }
});

// Disable scan button immediately on form submit
document.addEventListener("htmx:beforeRequest", function(event) {
    var form = event.detail.elt;
    if (form && form.tagName === "FORM" && form.querySelector("#scan-btn")) {
        var btn = form.querySelector("#scan-btn");
        btn.disabled = true;
        btn.ariaBusy = "true";
        btn.textContent = "Scanning...";
    }
});
```

**Why this works:** HTMX fires `htmx:afterSwap` on every swap. The status area is swapped on every poll cycle. When it reaches DONE or ERROR, the CSS classes `.status-done` / `.status-error` are present in the new content, so we detect the terminal state and re-enable the button. This eliminates inline scripts entirely.

**Confidence:** HIGH -- HTMX event system is well-documented; `htmx:afterSwap` and `htmx:beforeRequest` are standard lifecycle events.

### Pattern 3: CLI Table with Dynamic Column Widths

**What:** Replace fixed-width `f"{value:<30}"` formatting with `shutil.get_terminal_size()` aware column sizing or use `min(len, max_width)` with truncation.

**Current problematic code (cli.py line 167-171):**
```python
header = f"{'Name':<30} {'Vendor':<15} {'Model':<20} {'Type'}"
click.echo(header)
click.echo("-" * len(header))
for d in device_list:
    click.echo(f"{d.name:<30} {d.vendor:<15} {d.model:<20} {d.device_type}")
```

**Replacement approach -- truncate to fit:**
```python
import shutil

def _truncate(value: str, width: int) -> str:
    """Truncate a string to fit within width, adding ellipsis if needed."""
    if len(value) <= width:
        return value
    return value[: width - 1] + "\u2026"  # ellipsis character

# In devices command:
cols = shutil.get_terminal_size((80, 24)).columns
# Allocate proportional widths
name_w = max(20, cols - 45)  # Remaining space after other columns
vendor_w = 15
model_w = 20
```

**Confidence:** HIGH -- `shutil.get_terminal_size` is stdlib since Python 3.3; truncation with ellipsis is standard CLI pattern.

### Anti-Patterns to Avoid

- **Inline `<script>` in HTMX partials:** Inline scripts execute on every swap, creating timing dependencies. Use HTMX lifecycle events in an external file instead.
- **`hx-on::*` with complex logic:** One-line handlers are fine; multi-statement logic should be in external JS.
- **Magic pixel values in CSS:** Use PicoCSS custom properties or the rem-based scale (0.25rem steps).
- **`!important` for layout overrides:** Increase selector specificity instead.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Enum label display | Template-level string manipulation | Jinja2 custom filter | Centralized, testable, reusable |
| Terminal width detection | Manual `os.get_terminal_size()` | `shutil.get_terminal_size()` with fallback | Handles pipes and non-TTY gracefully |
| CSS design tokens | Custom CSS variables | PicoCSS built-in custom properties | Already using PicoCSS; stay consistent |
| Button state management | Inline scripts per template | HTMX lifecycle events in external JS | Single source of truth, no duplication |

## Common Pitfalls

### Pitfall 1: HTMX Inline Script Execution Timing

**What goes wrong:** Inline `<script>` tags inside HTMX-swapped content execute immediately when injected into the DOM. If the target element (`#scan-btn`) hasn't been updated yet or is outside the swap target, the script may find stale references.
**Why it happens:** HTMX swaps innerHTML before scripts run, but sibling elements outside the swap target are not guaranteed to be in a specific state.
**How to avoid:** Use HTMX's event system (`htmx:afterSwap`, `htmx:afterSettle`) which fire after DOM is fully updated.
**Warning signs:** Button state gets stuck in "Scanning..." after completion.

### Pitfall 2: CSS Specificity with PicoCSS

**What goes wrong:** PicoCSS applies styles via element selectors and attribute selectors with moderate specificity. Custom overrides using simple class selectors may lose the cascade battle.
**Why it happens:** PicoCSS v2 uses patterns like `article > *` that are more specific than `.my-class`.
**How to avoid:** Test the specificity change by removing `!important` and verifying the rule still applies. Use browser DevTools or Playwright screenshot comparison.
**Warning signs:** Styles revert to PicoCSS defaults after removing `!important`.

### Pitfall 3: CLI Truncation Breaking Alignment

**What goes wrong:** Truncating values with multi-byte characters (unicode ellipsis is 3 bytes but 1 display column) can misalign columns.
**Why it happens:** `len()` counts characters, not display width.
**How to avoid:** Use single-character ellipsis (`\u2026`) and count display columns, not byte length. For this project, device names are typically ASCII, so this is LOW risk.
**Warning signs:** Columns misalign when device names contain non-ASCII characters.

### Pitfall 4: Jinja2 Filter Not Registered Before Template Render

**What goes wrong:** If the filter is registered after the first `TemplateResponse` is called, Jinja2 raises `UndefinedError`.
**Why it happens:** Filter registration must happen during app setup, before any request is handled.
**How to avoid:** Register the filter immediately after creating the `Jinja2Templates` instance in `app.py`.
**Warning signs:** 500 error on first page load after adding the filter to a template.

## Code Examples

### Humanized Enum Labels in History Table

```html
<!-- partials/history.html -- BEFORE -->
<td>{{ job.state.value }}</td>

<!-- partials/history.html -- AFTER -->
<td>{{ job.state.value | humanize_state }}</td>
```

### Accessible Refresh Button Labels

```html
<!-- index.html -- BEFORE -->
<button type="button" ... class="refresh-btn" title="Refresh tags">&#x21bb;</button>

<!-- index.html -- AFTER -->
<button type="button" ... class="refresh-btn" title="Refresh tags" aria-label="Refresh tags">
    &#x21bb; <span class="sr-only">Refresh</span>
</button>
```

With supporting CSS:

```css
/* Visually hidden but accessible to screen readers */
.sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
}
```

### Normalized Spacing Values

```css
/* BEFORE */
border-left: 3px solid var(--pico-primary);
padding: 0.2rem 0.5rem;  /* 0.2rem is off-grid */
overflow-x: auto !important;

/* AFTER */
border-left: var(--pico-border-width) solid var(--pico-primary);
padding: 0.25rem 0.5rem;  /* 0.25rem aligns to PicoCSS 4px grid */
overflow-x: auto;  /* Remove !important; selector specificity is sufficient */
```

### CLI Table Truncation

```python
# BEFORE
click.echo(f"{d.name:<30} {d.vendor:<15} {d.model:<20} {d.device_type}")

# AFTER
def _truncate(value: str, width: int) -> str:
    """Truncate string to width with ellipsis if needed."""
    if len(value) <= width:
        return value
    return value[: width - 1] + "\u2026"

click.echo(
    f"{_truncate(d.name, 30):<30} "
    f"{_truncate(d.vendor, 15):<15} "
    f"{_truncate(d.model, 20):<20} "
    f"{d.device_type}"
)
```

### External app.js (replacing inline scripts)

```javascript
/* static/app.js -- Scan button state management via HTMX events */

(function () {
    "use strict";

    function resetScanButton() {
        var btn = document.getElementById("scan-btn");
        if (btn) {
            btn.disabled = false;
            btn.setAttribute("aria-busy", "false");
            btn.textContent = "Scan";
        }
    }

    function disableScanButton() {
        var btn = document.getElementById("scan-btn");
        if (btn) {
            btn.disabled = true;
            btn.setAttribute("aria-busy", "true");
            btn.textContent = "Scanning\u2026";
        }
    }

    // After status-area swap: reset button on terminal states
    document.addEventListener("htmx:afterSwap", function (evt) {
        var target = evt.detail.target;
        if (!target || target.id !== "status-area") return;
        if (target.querySelector(".status-done") || target.querySelector(".status-error")) {
            resetScanButton();
        }
    });

    // Before scan form submit: disable button immediately
    document.addEventListener("htmx:beforeRequest", function (evt) {
        var elt = evt.detail.elt;
        if (elt && elt.matches && elt.matches('form[hx-post="/api/scan"]')) {
            disableScanButton();
        }
    });
})();
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Inline `<script>` in HTMX partials | HTMX lifecycle events in external JS | HTMX 1.x+ | Cleaner separation, no CSP issues |
| `hx-on::*` for complex handlers | External event listeners | HTMX 2.0 | Better maintainability |
| Fixed-width CLI columns | Dynamic terminal-aware truncation | Always available | No truncation on narrow terminals |
| Raw enum `.value` in templates | Jinja2 custom filters | Jinja2 2.x+ | Centralized label mapping |

## Open Questions

1. **"-- None --" in correspondent dropdown**
   - What we know: The 11-UI-REVIEW suggested "No correspondent" reads more naturally
   - What's unclear: Whether this is in scope for Phase 12 (phase title doesn't mention it)
   - Recommendation: Include as a minor fix alongside other copywriting changes

2. **Flip prompt "Cancel" vs "Abort scan"**
   - What we know: Both Phase 3 and Phase 11 UI reviews flagged this
   - What's unclear: Whether to add `hx-confirm` dialog as well
   - Recommendation: Rename to "Abort scan" without confirm dialog (keeps it simple)

3. **Scan form article missing heading**
   - What we know: Phase 3 UI review noted no `<h2>` or `<legend>` in the scan form article
   - What's unclear: Whether adding a heading changes visual layout significantly
   - Recommendation: Include as accessibility improvement -- add `<h2>Scan</h2>` to match `<h2>Job History</h2>`

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest 9.0.2+ with pytest-playwright 0.7.0+ |
| Config file | pyproject.toml `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/test_web.py tests/test_cli.py -x -q` |
| Full suite command | `uv run pytest -x -q` |

### Phase Requirements to Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| P12-01 | Humanized enum labels in history table | unit + browser | `uv run pytest tests/test_web.py -x -q -k history` | Partial (test_web.py exists) |
| P12-02 | Accessible button labels (aria-label, sr-only) | browser | `uv run pytest tests/test_browser.py -x -q -k refresh` | Partial (test_browser.py exists) |
| P12-03 | CLI table truncation handling | unit | `uv run pytest tests/test_cli.py -x -q -k devices` | Partial (test_cli.py exists) |
| P12-04 | No inline scripts in templates | unit | `uv run pytest tests/test_web.py -x -q -k script` | Wave 0 |
| P12-05 | Spacing normalized to design token grid | browser | `uv run pytest tests/test_browser.py -x -q -k spacing` | Wave 0 |

### Sampling Rate

- **Per task commit:** `uv run pytest tests/test_web.py tests/test_cli.py -x -q`
- **Per wave merge:** `uv run pytest -x -q`
- **Phase gate:** Full suite green + `uv run ruff check .` + `uv run ty check` + `uv run pyrefly check src tests`

### Wave 0 Gaps

- [ ] Test for humanize_state filter output in history response
- [ ] Test for no `<script>` tags in status partial response
- [ ] Test for CLI truncation with long device names
- [ ] Playwright test for aria-label on refresh buttons

## Sources

### Primary (HIGH confidence)

- Direct codebase inspection of all template files, CSS, routes, CLI, and job model
- Phase 3 UI Review (`.planning/phases/03-web-ui/03-UI-REVIEW.md`) -- detailed findings with file/line references
- Phase 11 UI Review (`.planning/phases/11-.../11-UI-REVIEW.md`) -- confirmed all findings as pre-existing

### Secondary (MEDIUM confidence)

- HTMX event system documentation (lifecycle events: `htmx:afterSwap`, `htmx:beforeRequest`) -- standard API
- PicoCSS v2 CSS custom properties (`--pico-border-width`, `--pico-border-radius`) -- standard tokens
- Jinja2 custom filter registration -- standard template engine feature

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- no new dependencies, all changes within existing tech
- Architecture: HIGH -- patterns are standard Jinja2/HTMX/CSS practices
- Pitfalls: HIGH -- identified from direct code inspection and UI audit findings

**Research date:** 2026-03-22
**Valid until:** 2026-04-22 (stable -- cosmetic changes only, no external API dependencies)
