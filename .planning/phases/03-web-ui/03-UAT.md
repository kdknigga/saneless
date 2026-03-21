---
status: complete
phase: 03-web-ui
source: [03-00-SUMMARY.md, 03-01-SUMMARY.md, 03-02-SUMMARY.md, 03-03-SUMMARY.md]
started: 2026-03-21T00:00:00Z
updated: 2026-03-21T00:01:00Z
---

## Current Test

[testing complete]

## Tests

### 1. Cold Start Smoke Test
expected: Kill any running server. Start with `uv run fathom`. Server boots without errors. Health endpoint returns JSON. Main page loads in browser.
result: issue
reported: "sqlite3.OperationalError: unable to open database file — create_app doesn't mkdir tmp_dir before opening SQLite DB, unlike pipeline.py which does"
severity: blocker

### 2. Scan Form UI
expected: Main page shows a scan form with profile dropdown, title text input, tags multi-select, correspondent dropdown, and a "Scan" button.
result: skipped
reason: Server won't start due to Test 1 blocker

### 3. Live Status Polling
expected: After submitting a scan, the status area polls automatically (~1s). Shows a spinner/progress during scanning, a checkmark on success, or an error indicator on failure. A thumbnail of the scanned document appears on completion.
result: skipped
reason: Server won't start due to Test 1 blocker

### 4. Manual Duplex Flip Prompt
expected: When scanning a duplex document that requires manual flip, a prompt appears with an SVG illustration showing correct long-edge flip orientation. "Continue" and "Cancel" buttons are present.
result: skipped
reason: Server won't start due to Test 1 blocker

### 5. Job History Table
expected: Completed scan jobs appear in a history table with formatted timestamps and color-coded status (success/error). Table auto-refreshes when a job completes.
result: skipped
reason: Server won't start due to Test 1 blocker

### 6. Tags & Correspondents Dropdowns
expected: Tags and correspondent dropdowns are populated from paperless-ngx metadata. Tags support multi-select. Correspondent has a "None" default option.
result: skipped
reason: Server won't start due to Test 1 blocker

### 7. Graceful Paperless Degradation
expected: When paperless-ngx is unreachable, the UI still loads and functions. Tag and correspondent dropdowns show empty lists instead of errors. Scanning still works.
result: skipped
reason: Server won't start due to Test 1 blocker

### 8. Health Endpoint
expected: GET /health returns 200 with status info when worker is alive. Returns 503 when worker thread is down.
result: skipped
reason: Server won't start due to Test 1 blocker

### 9. Responsive Mobile Layout
expected: On a narrow viewport (~375px width), the UI stacks vertically and remains usable. Form elements and history table adapt to small screens.
result: skipped
reason: Server won't start due to Test 1 blocker

### 10. Scan Button Disable on Submit
expected: Clicking "Scan" immediately disables the button (before server response) to prevent double-submission. Button re-enables after job completes or errors.
result: skipped
reason: Server won't start due to Test 1 blocker

## Summary

total: 10
passed: 0
issues: 1
pending: 0
skipped: 9

## Gaps

- truth: "Server boots without errors on cold start and serves health endpoint"
  status: failed
  reason: "User reported: sqlite3.OperationalError: unable to open database file — create_app doesn't mkdir tmp_dir before opening SQLite DB, unlike pipeline.py which does"
  severity: blocker
  test: 1
  root_cause: ""
  artifacts: []
  missing: []
  debug_session: ""
