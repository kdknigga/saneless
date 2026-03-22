---
status: investigating
trigger: "GET /api/paperless/test returns connected even with invalid token"
created: 2026-03-21T00:00:00Z
updated: 2026-03-21T00:00:00Z
---

## Current Focus

hypothesis: test_connection() hits GET /api/ which is the DRF browsable API root -- this endpoint returns 200 regardless of auth status, so the 401/403 check never triggers
test: confirmed by reading source code and Paperless-ngx API docs
expecting: /api/ returns 200 even without valid token; an authenticated endpoint like /api/tags/ would return 401/403
next_action: report root cause

## Symptoms

expected: GET /api/paperless/test should return {"status":"token_rejected"} when an invalid API token is configured
actual: GET /api/paperless/test returns {"status":"connected"} even with a fake/invalid token
errors: No errors -- the response is 200 with wrong status value
reproduction: Configure a fake Paperless-ngx API token, call GET /api/paperless/test
started: Since initial implementation

## Eliminated

(none -- root cause found on first hypothesis)

## Evidence

- timestamp: 2026-03-21
  checked: src/saneless/web/routes.py lines 119-135
  found: Route handler simply delegates to paperless.test_connection() and wraps result in {"status": result}
  implication: Bug is not in the route handler; it's in PaperlessClient.test_connection()

- timestamp: 2026-03-21
  checked: src/saneless/paperless.py lines 205-223 (test_connection method)
  found: Method does GET /api/ and checks for 401/403 status codes. If not 401/403, returns "connected". No other status code handling.
  implication: The endpoint chosen (/api/) determines whether auth is enforced

- timestamp: 2026-03-21
  checked: Paperless-ngx API documentation and behavior
  found: GET /api/ is the Django REST Framework browsable API root. It returns 200 with a list of available endpoints regardless of authentication. It does NOT enforce token auth. Authenticated endpoints like /api/tags/ or /api/documents/ DO return 401/403 for invalid tokens.
  implication: test_connection() will always get 200 from /api/ and always return "connected", regardless of token validity

- timestamp: 2026-03-21
  checked: tests/test_web.py lines 237-273
  found: All paperless test cases stub test_connection() with lambda returning hardcoded strings. Tests never exercise the real HTTP logic, so they pass regardless of the endpoint bug.
  implication: Tests are testing the route handler plumbing correctly, but not the actual connection test logic in PaperlessClient

## Resolution

root_cause: PaperlessClient.test_connection() (src/saneless/paperless.py:218) sends GET /api/ to test connectivity. The Paperless-ngx /api/ endpoint is the DRF browsable API root which returns HTTP 200 regardless of authentication status. The 401/403 check on line 219 never triggers because /api/ does not require auth. Any token (valid, invalid, or missing) gets a 200 response, so the method always returns "connected".

fix: Change test_connection() to hit an endpoint that requires authentication, such as GET /api/tags/?page_size=1. This endpoint returns 200 with valid auth and 401/403 with invalid auth, enabling correct token validation. The method structure (try/get, check status, return string) is sound -- only the URL needs to change.

verification: (not yet applied)
files_changed: []
