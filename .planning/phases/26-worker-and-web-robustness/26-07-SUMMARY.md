---
phase: 26-worker-and-web-robustness
plan: 07
subsystem: web
tags: [fastapi, starlette, asgi-middleware, csrf, fetch-metadata, security]
requires:
  - phase: 26-01
    provides: RequestRejection.CROSS_SITE, rejection_status_code
  - phase: 26-05
    provides: render_error (HX and JSON error branches)
provides:
  - web/cross_origin.py with is_cross_origin_request(method, headers) and the CrossOriginGuard pure ASGI middleware
  - app-wide cross-site guard wired in create_app via app.add_middleware(CrossOriginGuard)
  - tests/test_cross_origin.py (23-case verdict table, generative unsafe-route test, log-line tests)
affects: [26-13, 26-14]
tech-stack:
  added: []
  patterns:
    - "Security middleware is pure ASGI and calls render_error directly; it never raises, because ExceptionMiddleware sits inside user middleware"
    - "Attacker-chosen values (path, headers) are logged with %r"
key-files:
  created:
    - src/saneless/web/cross_origin.py
    - tests/test_cross_origin.py
  modified:
    - src/saneless/web/app.py
key-decisions:
  - "The Sec-Fetch-Site value is lowercased before comparison, and hosts are compared case-insensitively; scheme and default ports are not normalised (as in Go)"
  - "The log line formats the request path with %r as well as the four headers, so a decoded control character in the path (for example an ANSI escape) is escaped"
  - "is_cross_origin_request uses a single-return if/elif chain with a helper _origin_matches_host, keeping ruff's return-count and complexity rules quiet without suppression"
requirements-completed: [ROBU-10]
duration: 12min
completed: 2026-09-14
---

# Phase 26 Plan 07: Cross-Origin Guard Summary

**Every POST/PUT/PATCH/DELETE now passes a Go 1.25 `CrossOriginProtection`-style check in a pure ASGI `CrossOriginGuard`: `Sec-Fetch-Site` allows only `same-origin`/`none`, and when it is absent (plain HTTP to a LAN IP) `Origin` must match `Host` or an `X-Forwarded-Host` entry; a rejection is a CROSS_SITE 403 from `render_error` with one `%r`-escaped WARNING line.**

## Performance

- **Duration:** about 12 min
- **Completed:** 2026-09-14
- **Tasks:** 2 (both TDD, RED then GREEN)
- **Files:** 2 created, 1 modified

## Accomplishments

- `is_cross_origin_request(method, headers)` implements all three D-20 branches and D-21's comma-separated `X-Forwarded-Host`; `Origin: null` and a same-host other-port Origin are rejected.
- `CrossOriginGuard` is installed app-wide right after `install_error_handlers`, so any route added later is covered (D-23). It renders the 403 through `render_error` (HX retarget or JSON) instead of raising.
- The generative test walks every `APIRoute` with an unsafe method (it finds `/api/scan`, `/api/cache/invalidate`, `/api/flip/continue`, `/api/flip/abort`) and asserts 403 under `Sec-Fetch-Site: cross-site`. A route added to the running app afterwards is also rejected.
- A rejected `/api/scan` writes no job row. The plain-HTTP branch is exercised through the app with `Origin: http://evil.example` (403) against `Origin: http://testserver` (200).
- Full non-browser suite: 1303 passed. Existing TestClient posts carry no Origin or Sec-Fetch-Site, so they land in branch 3 and pass unchanged.

## Task Commits

1. **Task 1: The verdict function**
   - `648b403` test(26-07): add failing table for the cross-origin verdict
   - `8b3cca0` feat(26-07): implement the cross-origin verdict function
2. **Task 2: CrossOriginGuard middleware, wiring, 403 rendering and the log line**
   - `accf6f3` test(26-07): add failing tests for the app-wide cross-origin guard
   - `dcf7532` feat(26-07): guard every unsafe request with CrossOriginGuard

## Files Created/Modified

- `src/saneless/web/cross_origin.py`: the verdict function, the `_origin_matches_host` helper and `CrossOriginGuard`. The module docstring covers the Go algorithm, the W3C "potentially trustworthy URL" reason branch 2 is needed, why a browser cannot forge `X-Forwarded-Host`, and the no-normalisation rule.
- `src/saneless/web/app.py`: `app.add_middleware(CrossOriginGuard)` with the D-23 comment.
- `tests/test_cross_origin.py`: the verdict table plus 9 app-level tests, using a copy of the fixture chain from `test_web_errors.py`.

## Decisions Made

See `key-decisions` in the frontmatter.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Security] The request path is logged with `%r`, not `%s`**
- **Found during:** Task 2
- **Issue:** The plan's format string logged the path with `%s`. The path is attacker-chosen. The standard library's URL parser drops `\t\r\n`, but other control characters survive decoding, for example an ANSI escape (`%1B`). That is the log-injection surface T-26-29 covers.
- **Fix:** The format is now `"Blocked cross-site %s %r: Origin=%r Host=%r X-Forwarded-Host=%r Sec-Fetch-Site=%r"`. A test sends a scope straight into the guard and checks that an escape sequence in the path shows up escaped. The scope goes in directly because httpx strips control characters from URLs, so TestClient cannot send one. This test was added in the GREEN commit, not the RED one.
- **Files modified:** src/saneless/web/cross_origin.py, tests/test_cross_origin.py
- **Commit:** dcf7532

**2. [Process] Import grouping in the RED commit**
- **Found during:** Task 1 RED
- **Issue:** Before `saneless.web.cross_origin` existed, ruff's isort treated the import as third-party and merged it into the third-party block.
- **Fix:** The RED commit uses ruff's grouping. The GREEN commit re-sorted it into the first-party block once the module existed. No suppression was used.

## Issues Encountered

- The worktree started from the wrong base (`a87b3dd`) and was reset to `7cd9ed7` as the branch check instructs.
- The RTK shell hook rewrites `git add/commit/status` into `rtk git ...`, and the worktree isolation guard refuses those. Git was run as `/usr/bin/git` from the worktree root. Hooks ran normally, with no `--no-verify`.

## Known Stubs

None.

## Threat Flags

None. The new surface (middleware on every HTTP request, header logging) is what T-26-26 through T-26-31 already cover.

## TDD Gate Compliance

For both tasks, a `test(26-07)` commit comes before the matching `feat(26-07)` commit. No REFACTOR commits were needed.

## Self-Check: PASSED

- FOUND: src/saneless/web/cross_origin.py
- FOUND: tests/test_cross_origin.py
- FOUND: src/saneless/web/app.py (add_middleware(CrossOriginGuard) at line 94)
- FOUND commits: 648b403, 8b3cca0, accf6f3, dcf7532
