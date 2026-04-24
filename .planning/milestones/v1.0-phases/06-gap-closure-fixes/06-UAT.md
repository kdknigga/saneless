---
status: diagnosed
phase: 06-gap-closure-fixes
source: 06-01-SUMMARY.md
started: 2026-03-21T02:00:00Z
updated: 2026-03-21T02:03:00Z
---

## Current Test

[testing complete]

## Tests

### 1. Paperless Connection Test — Connected
expected: With the app running and Paperless-ngx configured and reachable, GET /api/paperless/test returns JSON with status "connected".
result: pass

### 2. Paperless Connection Test — Token Rejected
expected: With an invalid Paperless-ngx API token configured, GET /api/paperless/test returns JSON with status "token_rejected".
result: issue
reported: "with a fake token, I'm still getting {\"status\":\"connected\"}."
severity: major

### 3. Paperless Connection Test — Unreachable
expected: With Paperless-ngx URL pointing to a non-existent server, GET /api/paperless/test returns JSON with status "unreachable".
result: pass

### 4. Worker ASSEMBLING State
expected: When a scan job reaches the "Assembling PDF..." stage, the job state transitions to ASSEMBLING. This should be visible in the job status API or UI.
result: skipped
reason: deferred

### 5. Worker UPLOADING State
expected: When a scan job reaches the "Uploading to paperless-ngx..." stage, the job state transitions to UPLOADING. This should be visible in the job status API or UI.
result: skipped
reason: deferred

## Summary

total: 5
passed: 2
issues: 1
pending: 0
skipped: 2

## Gaps

- truth: "With an invalid Paperless-ngx API token configured, GET /api/paperless/test returns JSON with status token_rejected"
  status: failed
  reason: "User reported: with a fake token, I'm still getting {\"status\":\"connected\"}."
  severity: major
  test: 2
  root_cause: "PaperlessClient.test_connection() hits GET /api/ which is the DRF browsable API root — returns 200 regardless of auth. The 401/403 check never triggers because /api/ doesn't enforce authentication."
  artifacts:
    - path: "src/saneless/paperless.py"
      issue: "test_connection() uses /api/ endpoint which doesn't require auth"
  missing:
    - "Change test endpoint from /api/ to an auth-requiring endpoint like /api/tags/?page_size=1"
  debug_session: ".planning/debug/paperless-token-test.md"
