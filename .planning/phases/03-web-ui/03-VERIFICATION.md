---
phase: 03-web-ui
verified: 2026-03-20T21:17:57Z
status: passed
score: 14/14 must-haves verified
re_verification: false
human_verification:
  - test: "Load http://<host>:8080 in a browser on a separate LAN device"
    expected: "Page renders with PicoCSS styling, profile dropdown, tags multi-select, correspondent dropdown, and Scan button. No layout breaks."
    why_human: "CDN-loaded PicoCSS and HTMX require a real browser to verify rendering, responsive layout, and actual interactive behavior."
  - test: "Submit a scan with a real scanner attached"
    expected: "Status area updates live every 1s during scanning, thumbnail appears after first page, flip prompt appears for duplex, history table refreshes on completion."
    why_human: "Real-time HTMX polling behavior and actual scanner integration cannot be exercised by unit tests."
---

# Phase 3: Web UI Verification Report

**Phase Goal:** Users can perform all scanning operations from a browser on any device on the LAN, with live feedback, metadata entry, and job history
**Verified:** 2026-03-20T21:17:57Z
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | User can open the web UI and see profile dropdown, title field, tags multi-select, correspondent dropdown, and Scan button | VERIFIED | `test_page_loads` passes; index.html contains `name="profile"`, `name="title"`, `name="tags" multiple`, `name="correspondent"`, `id="scan-btn"` |
| 2 | Status area polls every 1s during active job states and stops polling when idle/done/error | VERIFIED | `status.html` sets `hx-trigger="every 1s"` conditionally on active_states list; `test_status_polling_active_job` passes |
| 3 | For manual duplex, status area shows flip prompt with exact PRD wording, SVG illustration, Continue and Cancel buttons | VERIFIED | `flip.html` contains "flip the stack over the long edge", inline SVG, `hx-post="/api/flip/continue"`, `hx-post="/api/flip/abort"`; `test_flip_prompt` passes |
| 4 | First-page thumbnail displayed inline as base64 JPEG img tag | VERIFIED | `status.html` contains `data:image/jpeg;base64,{{ job.thumbnail }}`; `test_thumbnail_display` passes |
| 5 | Job history table showing recent jobs, persisted to SQLite | VERIFIED | `test_job_history` passes; `GET /api/jobs/history` returns `list_recent(limit=50)` from SQLite |
| 6 | Old jobs automatically pruned by age and count on app startup | VERIFIED | `prune()` called in lifespan startup; `test_prune_by_age` and `test_prune_by_count` pass |
| 7 | Scan button disabled while a job is in progress | VERIFIED | `index.html` disables button for active states; `hx-on::before-request` disables immediately on click; `test_scan_button_disabled_during_active_job` passes |
| 8 | Tag and correspondent dropdowns have refresh icons for per-resource cache invalidation | VERIFIED | index.html contains `hx-post="/api/cache/invalidate?resource=tags"` and `hx-post="/api/cache/invalidate?resource=correspondents"` refresh buttons |
| 9 | Error messages from failed jobs displayed in status area | VERIFIED | `status.html` renders `role="alert"` paragraph with `job.error`; `test_error_display` passes |
| 10 | Profile dropdown populated from settings.profiles keys | VERIFIED | Routes passes `list(settings.profiles.keys())`; `test_profile_dropdown` asserts each profile name appears |
| 11 | GET /health returns 200 when worker alive, 503 when down | VERIFIED | `routes.py` checks `worker.is_alive`; `test_health_endpoint_ok` passes; worker `is_alive` property confirmed in worker.py |
| 12 | Health endpoint requires no authentication | VERIFIED | `test_health_endpoint_no_auth` passes; no auth middleware on route |
| 13 | Metadata cache respects TTL and supports per-resource invalidation | VERIFIED | `test_cache_ttl_expiry`, `test_cache_invalidate`, `test_cache_per_resource` all pass |
| 14 | Page loads even when paperless-ngx is unreachable (empty tags/correspondents) | VERIFIED | `_get_cached_or_fetch` wraps all API calls in try/except, returns `[]` on error; routes.py confirmed |

**Score:** 14/14 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/saneless/web/__init__.py` | Web subpackage init exporting `create_app` | VERIFIED | `from .app import create_app` present |
| `src/saneless/web/app.py` | FastAPI app factory with lifespan | VERIFIED | `create_app()`, lifespan with worker start/stop, `app.state` wired fully |
| `src/saneless/web/cache.py` | TTL metadata cache | VERIFIED | `MetadataCache` with `get`, `set`, `invalidate` using `time.monotonic()` |
| `src/saneless/web/routes.py` | All route handlers | VERIFIED | 10 routes registered: `/`, `/health`, `/api/scan`, `/api/jobs/current/status`, `/api/tags`, `/api/correspondents`, `/api/cache/invalidate`, `/api/jobs/history`, `/api/flip/continue`, `/api/flip/abort` |
| `src/saneless/web/templates/base.html` | HTML shell with PicoCSS, HTMX | VERIFIED | PicoCSS `pico@2` and HTMX `htmx.org@2.0.8` CDN links present |
| `src/saneless/web/templates/index.html` | Main page with form, status, history | VERIFIED | Form with `hx-post="/api/scan"`, status include, history table |
| `src/saneless/web/templates/partials/status.html` | Job status with conditional polling | VERIFIED | `hx-trigger="every 1s"`, `hx-get="/api/jobs/current/status"`, `id="status-area"`, base64 thumbnail |
| `src/saneless/web/templates/partials/flip.html` | Flip prompt with SVG | VERIFIED | PRD wording, inline SVG with correct/incorrect illustrations, Continue/Cancel buttons |
| `src/saneless/web/templates/partials/history.html` | Job history table body | VERIFIED | `id="history-body"`, job rows with date/profile/title/state |
| `src/saneless/web/templates/partials/tags.html` | Tag dropdown options | VERIFIED | Option elements from `tags` context variable |
| `src/saneless/web/templates/partials/correspondents.html` | Correspondent dropdown options | VERIFIED | Option elements from `correspondents` context variable |
| `src/saneless/web/static/app.css` | App-specific CSS (min 10 lines) | VERIFIED | 75 lines; contains `#status-area`, `.status-done`, `.status-error`, `.thumbnail`, `.flip-illustration`, `.refresh-btn`, `@media` |
| `src/saneless/job.py` | Extended with `list_recent` and `prune` | VERIFIED | Both methods present at lines 210 and 241 |
| `src/saneless/worker.py` | Extended with `is_alive` property | VERIFIED | Property at line 97 |
| `src/saneless/paperless.py` | Extended with `get_tags` and `get_correspondents` | VERIFIED | Methods at lines 221 and 237 |
| `src/saneless/config.py` | Extended with cache TTL and web host/port fields | VERIFIED | `paperless_cache_ttl_seconds`, `web_host`, `web_port` at lines 75-77 |
| `tests/test_web.py` | FastAPI route tests (min 100 lines) | VERIFIED | 247 lines, 15 passing tests, no xfail stubs |
| `tests/test_cache.py` | MetadataCache unit tests | VERIFIED | 51 lines, 5 passing tests, no xfail stubs |
| `tests/test_job.py` | Extended with list_recent and prune tests | VERIFIED | `test_list_recent`, `test_prune_by_age`, `test_prune_by_count` present and passing |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `web/app.py` | `worker.py` | `ScanWorker` created in lifespan, stored on `app.state.worker` | WIRED | `app.state.worker = worker` confirmed; `worker.start()` in lifespan |
| `web/routes.py` | `job.py` | `job_store.list_recent()` and `job_store.get_job()` | WIRED | Both calls present in routes; tested by `test_job_history` and `test_status_polling` |
| `web/routes.py` | `web/cache.py` | `_get_cached_or_fetch()` uses `cache.get`, `cache.set`, `cache.invalidate` | WIRED | All three cache methods called; tested by `test_cache_invalidate` |
| `templates/index.html` | `/api/scan` | `hx-post="/api/scan"` on form | WIRED | Confirmed in index.html line 6 |
| `templates/partials/status.html` | `/api/jobs/current/status` | `hx-get="/api/jobs/current/status"` with `hx-trigger="every 1s"` | WIRED | Confirmed in status.html lines 4-6 |
| `templates/partials/flip.html` | `/api/flip/continue` | `hx-post="/api/flip/continue"` on Continue button | WIRED | Confirmed in flip.html line 42 |
| `tests/test_web.py` | `web/app.py` | `TestClient(create_app(settings, mock_scanner))` | WIRED | `from saneless.web.app import create_app`; client fixture uses `TestClient(app)` |
| `tests/test_cache.py` | `web/cache.py` | Direct `MetadataCache` instantiation | WIRED | `from saneless.web.cache import MetadataCache` |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|-------------|--------|----------|
| UI-01 | 03-00, 03-01, 03-02, 03-03 | Web UI accessible from any browser with profile selector, metadata fields, Scan button | SATISFIED | `test_page_loads` passes; index.html verified |
| UI-02 | 03-00, 03-01, 03-02, 03-03 | Live status indicator via polling | SATISFIED | `hx-trigger="every 1s"` in status.html; polling tests pass |
| UI-03 | 03-00, 03-01, 03-02, 03-03 | Flip prompt with PRD wording, illustration, Continue/Cancel | SATISFIED | `test_flip_prompt` passes; PRD wording confirmed in flip.html |
| UI-04 | 03-00, 03-01, 03-02, 03-03 | First-page thumbnail in status area | SATISFIED | `test_thumbnail_display` passes; base64 img tag in status.html |
| UI-05 | 03-00, 03-01, 03-02, 03-03 | Job history table with SQLite persistence | SATISFIED | `test_job_history` and `test_list_recent` pass |
| UI-06 | 03-01, 03-03 | Job history pruned by age and count | SATISFIED | `test_prune_by_age` and `test_prune_by_count` pass; prune called in lifespan |
| UI-07 | 03-00, 03-01, 03-02, 03-03 | Scan button disabled during active job | SATISFIED | `test_scan_button_disabled_during_active_job` passes; server-side disable + client-side `hx-on::before-request` |
| UI-08 | 03-00, 03-01, 03-02, 03-03 | Refresh icon for manual cache invalidation | SATISFIED | `test_cache_invalidate` passes; refresh buttons in index.html |
| PROF-03 | 03-00, 03-01, 03-02, 03-03 | Profile names from settings in dropdown | SATISFIED | `test_profile_dropdown` passes |
| PLSS-04 | 03-00, 03-01, 03-02, 03-03 | Title, tags, correspondent fields before scanning | SATISFIED | `test_scan_form_submit` passes; all fields in index.html form |
| PLSS-05 | 03-00, 03-01, 03-03 | Tag/correspondent cache with TTL and per-resource refresh | SATISFIED | All 5 cache tests pass; `MetadataCache` with TTL confirmed |
| HLTH-01 | 03-00, 03-01, 03-03 | GET /health returns 200 or 503 based on worker state | SATISFIED | `test_health_endpoint_ok` passes; 503 path confirmed in routes.py |
| HLTH-02 | 03-00, 03-01, 03-03 | Health endpoint requires no auth | SATISFIED | `test_health_endpoint_no_auth` passes |
| LOG-03 | 03-00, 03-01, 03-02, 03-03 | Scan/API/assembly errors displayed in web UI | SATISFIED | `test_error_display` passes; `role="alert"` error paragraph in status.html |

All 14 requirements SATISFIED. No orphaned requirements.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `src/saneless/web/routes.py` | 90, 153, 174+ | `TemplateResponse(name, context_dict)` — deprecated Starlette API (name should be second arg, request first) | Warning | Tests pass with 13 deprecation warnings; no functional impact. Will break on future Starlette major version bump. |

No blocker anti-patterns found. No TODO/FIXME/placeholder comments in production code. No stub implementations.

### Human Verification Required

#### 1. Browser Rendering on LAN Device

**Test:** Open `http://<server-ip>:8080` in a browser on a separate device on the same LAN.
**Expected:** Page renders with PicoCSS styles applied, responsive layout, all form elements visible, dark/light mode toggle works via `data-theme="auto"`.
**Why human:** CDN-loaded PicoCSS and HTMX cannot be verified by unit tests. Network accessibility from another device requires a real environment.

#### 2. Live HTMX Polling During Active Scan

**Test:** Submit a scan job from the web UI with a real or stubbed scanner.
**Expected:** Status area updates every ~1 second showing state transitions (PENDING -> SCANNING -> ASSEMBLING -> UPLOADING -> DONE). Polling stops when terminal state is reached. History table refreshes automatically on DONE/ERROR.
**Why human:** Real-time polling behavior, HTMX swap animations, and actual state transitions require a live browser session.

### Gaps Summary

No gaps found. All 14 phase requirements are satisfied by substantive, wired implementations. The complete test suite (178 tests) passes with zero failures. Only item to note is the Starlette `TemplateResponse` deprecation warning — functional today, but worth addressing before a Starlette upgrade.

---

_Verified: 2026-03-20T21:17:57Z_
_Verifier: Claude (gsd-verifier)_
