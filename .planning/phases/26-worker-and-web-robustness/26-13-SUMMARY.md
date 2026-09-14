---
phase: 26-worker-and-web-robustness
plan: 13
subsystem: testing
tags: [playwright, browser-tests, htmx, status-message, cross-origin, accessibility]
requires:
  - phase: 26-12
    provides: "egress gate, gated _BrowserTestScanner, delivering_paperless, scan_harness"
  - phase: 26-07
    provides: "CrossOriginGuard with the Origin fallback (D-20 branch 2)"
  - phase: 26-11
    provides: "status_response.html slot clear on successful scan only"
provides:
  - "TestRequestErrorSlot: B8, B9 (light/dark), B10, B11, B12 (1280/375), B13"
  - "TestPlainHttpLanOrigin: real-browser proof of D-20 branch 2 on a LAN address"
  - "Shared browser-test helpers: _start_uvicorn/_stop_uvicorn, _browser_test_settings, _make_paperless_deliver, _JS_BACKGROUND_STACK_OF, _READ_ELEMENT_CONTRAST"
affects: [26-verification]
tech-stack:
  added: []
  patterns:
    - "Read request headers on the server with an ASGI wrapper; Playwright's all_headers() omits Sec-Fetch-* under context.route"
    - "Fixture returns an opener so colour scheme and viewport are set before goto"
key-files:
  created: []
  modified:
    - tests/test_browser.py
key-decisions:
  - "Sec-Fetch-Site is asserted from server-received headers, not Request.all_headers(). Under the egress gate's route interception, Playwright reports no Sec-Fetch-* even when the server receives same-origin, so the planned assertion could never fail"
  - "queue_full_page builds on scan_harness for teardown. scan_harness now also asserts that no created job rows survive"
  - "B10 re-reads the slot once after the 2.5 s window (inner_text), not with a retrying expect. The claim is that the message is still there, not that it can be found again"
requirements-completed: [ROBU-02, ROBU-10]
duration: ~40min
completed: 2026-09-14
---

# Phase 26 Plan 13: Request-error visibility and LAN-origin scans in Chromium Summary

**Chromium tests for the error slot. A 429 shows in `#status-message` as legible, aligned text without its own `role="alert"`, and Job History records the attempt. Status polling does not erase it and a successful scan clears it. A separate test proves that a page on a plain-HTTP LAN address can start a scan through the guard's Origin branch.**

## Performance

- **Duration:** about 40 min
- **Completed:** 2026-09-14
- **Tasks:** 3
- **Files modified:** 1

## Accomplishments

- **B8** `test_queue_full_message_is_visible_and_recorded`: loads an idle page, closes the gate and fills the queue over HTTP until a 429 comes back (at most 15 tries). It then clicks Scan and checks that the slot text is exactly `✗ The scan queue is full. Wait for a scan to finish, then try again.` It also checks: one `p.status-error`, zero `[role="alert"]` inside the slot, one `#status-area`, Scan enabled, focus not in the slot, and the first history row reading `Failed` with class `status-error` within 5 s.
- **B9** (light and dark): the slot `<p>` computes to `rgb(136, 57, 53)` / `rgb(206, 126, 123)`. Contrast against the flattened background stack of the real element is at least 4.5:1. The background walk is now one JS snippet shared with the DARK probe.
- **B12** (1280 and 375 px): the left edge of the first glyph in the slot `<p>` and in the `#status-area` `<p>` differ by at most 1 px.
- **B13**: at 375x812, `worker.health` is replaced with a DEGRADED property. The 503 message appears in the slot and wraps (more than one line box), and `main.scrollWidth <= main.clientWidth`.
- **B10**: during a gated scan, `htmx.ajax` posts an unknown profile. The message appears and does not echo `zz-nonexistent-profile`. It is still there after 2.5 s with at least two status poll responses in that window, and `#status-area` still exists.
- **B11**: after the same unknown-profile error, a successful Scan empties the slot (`childNodes.length === 0`). It is back to zero height, still `role="alert"`, and the scan reaches DONE.
- **TestPlainHttpLanOrigin**: runs a separate app on `0.0.0.0` with a free port, reached at the runner's non-loopback IPv4 address. A Scan click gets 200. The server received no `Sec-Fetch-Site`, and `Origin` equals the page origin. The slot is empty and the scan reaches DONE. The test skips with `no non-loopback IPv4 address` when no such address is found.

## Task Commits

1. **Task 1: queue-full visibility, colour, alignment, narrow wrap (B8, B9, B12, B13)**: `e0de7e3` (test)
2. **Task 2: slot survives polling and clears on success (B10, B11)**: `670bf62` (test)
3. **Task 3: plain-HTTP LAN origin can scan (D-20 branch 2)**: `79605d6` (test)

## Executor Self-Checks (run locally, not committed)

| Mutation | Expected failure | Observed |
|---|---|---|
| `role="alert"` on the error partial's `<p>` | B8 | `Locator expected to have count '0'` / `unexpected value "1"` on `#status-message [role="alert"]`: FAILED as expected |
| Drop `HX-Retarget` from `render_error` | B8 | `#status-message` stayed `""` (message not placed in the slot): FAILED as expected |
| `clear_message: True` in the poll route's context | B10 | `AssertionError: a poll erased the error` (`'' `after the window): FAILED as expected |
| Guard branch 2 always rejects (`rejected = True`) | LAN test | `assert 403 == 200`: FAILED as expected |
| LAN fixture pointed at `127.0.0.1` | LAN test (branch-2 message) | `Chromium sent Sec-Fetch-Site='same-origin' to http://127.0.0.1:…, so this test no longer exercises D-20 branch 2`: FAILED as expected |
| Address discovery forced to None | clean skip | `SKIPPED ... no non-loopback IPv4 address`: skipped as expected |

After each mutation the file was restored with `git checkout -- <file>`, and `git ls-files --modified` then listed only the intended test file.

**LAN test locally:** it ran and PASSED on `192.168.2.127`; it was not skipped. On a CI runner it runs or skips depending on whether that runner has a default route. The TestClient branch-2 tests in `tests/test_cross_origin.py` remain the required coverage either way.

**Assumption A6:** confirmed by measurement. A scratch probe outside the repo compared server-received headers with Playwright's view:

| Origin | Route intercept | Server got `Sec-Fetch-Site` | `all_headers()` showed it |
|---|---|---|---|
| 127.0.0.1 | no | `same-origin` | `same-origin` |
| 127.0.0.1 | yes | `same-origin` | none |
| LAN IP | no | none | none |
| LAN IP | yes | none | none |

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] The planned `Sec-Fetch-Site` assertion could never fail**
- **Found during:** Task 3
- **Issue:** The plan asserted `"sec-fetch-site" not in req_info.value.all_headers()`. Every page in this module is behind the egress gate's `context.route`, and under route interception Playwright's `all_headers()` does not show `Sec-Fetch-*`. The test passed even when pointed at 127.0.0.1, where the server does receive `same-origin`.
- **Fix:** Added `_ScanHeaderRecorder`, a pass-through ASGI wrapper around the LAN app that records the headers of each `POST /api/scan`. The test asserts on those headers: exactly one submit, no `sec-fetch-site`, and `origin == lan_url`. `expect_request` was dropped as redundant. `_start_uvicorn` accepts any `ASGIApp`.
- **Files modified:** tests/test_browser.py
- **Commit:** 79605d6

**2. [Rule 2 - Missing check] Teardown now asserts that no rows are left behind**
- **Found during:** Task 1 (acceptance criterion "asserted in teardown")
- **Issue:** `scan_harness` deleted created rows but never checked the result.
- **Fix:** After its cleanup succeeds, `scan_harness` asserts `created_job_ids() == []`. The check does not run when a wait fails, so it cannot hide that failure.
- **Commit:** e0de7e3

### Structural choices within plan latitude

- `queue_full_page` returns an opener `(scheme, width) -> Page` built on `scan_harness`, so B9 and B12 can set scheme and viewport before `goto`. The plan allowed either this or loading the page inside the test.
- The uvicorn start/stop, settings and Paperless stub were pulled out of `browser_server` / `delivering_paperless` so the LAN fixture reuses them. `host="0.0.0.0"` still appears exactly once.

### Process note

- A Serena `insert_after_symbol` call landed in the main checkout's `tests/test_browser.py`, because Serena's project root is the main checkout, not this worktree. The block was re-applied in the worktree with Edit and removed from the main checkout. Afterwards the main checkout's file was byte-identical (`cmp`) to `ba2e16f:tests/test_browser.py`. No further Serena edits were used.

## Verification

- `uv run pytest tests/test_browser.py -m browser -q -k TestRequestErrorSlot`: 8 passed (B8, B9 x2, B10, B11, B12 x2, B13)
- `uv run pytest tests/test_browser.py -m browser -q -k PlainHttpLanOrigin -rs`: 1 passed
- `uv run pytest -m browser -q`: 48 passed; `uv run pytest tests/test_browser.py -q`: 57 passed
- `uv run ruff check tests`, `uv run ruff format --check .`, `uv run ty check`, `uv run pyrefly check src tests`: clean (the 4 pyrefly warnings are pre-existing, in other files)
- `uv run prek run --stage pre-push --all-files`: all passed

## Known Stubs

None.

## Threat Flags

None beyond the plan's register. The 0.0.0.0 test server is T-26-56 (accepted): it is function-scoped, uses a stub scanner, stubbed Paperless and a free port, and is shut down with the thread-stop asserted.

## Self-Check: PASSED

- tests/test_browser.py: FOUND (`class TestRequestErrorSlot` x1, `class TestPlainHttpLanOrigin` x1, `host="0.0.0.0"` x1, `egress_allowlist.append` present)
- Commits e0de7e3, 670bf62, 79605d6: FOUND in `git log`
