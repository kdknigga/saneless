---
status: complete
phase: 03-web-ui
source: [03-00-SUMMARY.md, 03-01-SUMMARY.md, 03-02-SUMMARY.md, 03-03-SUMMARY.md]
started: 2026-03-21T00:00:00Z
updated: 2026-03-21T12:15:00Z
---

## Current Test

[testing complete]

## Tests

### 1. Cold Start Smoke Test
expected: Kill any running server. Start with `uv run fathom`. Server boots without errors. Health endpoint returns JSON. Main page loads in browser.
result: issue
reported: "sqlite3.OperationalError: unable to open database file — create_app doesn't mkdir tmp_dir before opening SQLite DB"
severity: blocker
fix: Added Path(tmp_dir).mkdir(parents=True, exist_ok=True) in create_app()

### 2. Scan Form UI
expected: Main page shows a scan form with profile dropdown, title text input, tags multi-select, correspondent dropdown, and a "Scan" button.
result: pass

### 3. Live Status Polling
expected: After submitting a scan, the status area polls automatically (~1s). Shows a spinner/progress during scanning, a checkmark on success, or an error indicator on failure. A thumbnail of the scanned document appears on completion.
result: skipped
reason: Scanner source/auto-detect issues prevent completing a full scan cycle; user plans scanner rework in a future phase

### 4. Manual Duplex Flip Prompt
expected: When scanning a duplex document that requires manual flip, a prompt appears with an SVG illustration showing correct long-edge flip orientation. "Continue" and "Cancel" buttons are present.
result: skipped
reason: Scanner source/auto-detect issues prevent completing a full scan cycle; user plans scanner rework in a future phase

### 5. Job History Table
expected: Completed scan jobs appear in a history table with formatted timestamps and color-coded status (success/error). Table auto-refreshes when a job completes.
result: skipped
reason: Scanner source/auto-detect issues prevent completing a full scan cycle; user plans scanner rework in a future phase

### 6. Tags & Correspondents Dropdowns
expected: Tags and correspondent dropdowns are populated from paperless-ngx metadata. Tags support multi-select. Correspondent has a "None" default option.
result: pass

### 7. Graceful Paperless Degradation
expected: When paperless-ngx is unreachable, the UI still loads and functions. Tag and correspondent dropdowns show empty lists instead of errors. Scanning still works.
result: pass

### 8. Health Endpoint
expected: GET /health returns 200 with status info when worker is alive. Returns 503 when worker thread is down.
result: pass

### 9. Responsive Mobile Layout
expected: On a narrow viewport (~375px width), the UI stacks vertically and remains usable. Form elements and history table adapt to small screens.
result: issue
reported: "History table overflows on narrow screens; stray '-- None --' option rendered outside correspondent select into status area"
severity: major
fix: Added overflow-x:auto wrapper for table, fixed HTMX target inheritance with hx-target="this" on tag/correspondent selects

### 10. Scan Button Disable on Submit
expected: Clicking "Scan" immediately disables the button (before server response) to prevent double-submission. Button re-enables after job completes or errors.
result: pass

## Summary

total: 10
passed: 5
issues: 2
pending: 0
skipped: 3

## Gaps

- truth: "Server boots without errors on cold start and serves health endpoint"
  status: fixed
  reason: "User reported: sqlite3.OperationalError: unable to open database file"
  severity: blocker
  test: 1
  root_cause: "create_app() in web/app.py calls JobStore without ensuring tmp_dir exists"
  artifacts:
    - path: "src/saneless/web/app.py"
      issue: "Missing mkdir for tmp_dir before JobStore instantiation"
  missing: []
  debug_session: ""

- truth: "History table fits within mobile viewport; no stray elements rendered outside form controls"
  status: fixed
  reason: "User reported: table overflows on narrow screens; stray option element in status area"
  severity: major
  test: 9
  root_cause: "1) No overflow containment on table wrapper. 2) Tag/correspondent selects inherited form's hx-target='#status-area' causing HTMX response to swap into wrong element"
  artifacts:
    - path: "src/saneless/web/static/app.css"
      issue: "No overflow-x on table wrapper"
    - path: "src/saneless/web/templates/index.html"
      issue: "Missing hx-target='this' on tag/correspondent selects"
  missing: []
  debug_session: ""

## Additional Fixes (discovered during UAT)

- **Port conflict silent exit**: Added socket bind check before uvicorn.run in cli.py serve command
- **Scan button stuck in throbber on load**: Fixed hx-on::before-request to only fire for form submission, not child element HTMX requests
- **PENDING state not polled**: Added PENDING to active_states in status.html so polling starts immediately after scan submission
- **Scanner auto-detection**: Pipeline now auto-detects scanner when settings.scanner.device is empty
- **Scanner source fallback**: Falls back to 'Auto' source when configured source not available on device
- **Auto source ADF detection**: When source is 'Auto' and device has no Flatbed, uses ADF multi_scan mode
