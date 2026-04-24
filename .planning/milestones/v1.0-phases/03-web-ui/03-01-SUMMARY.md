---
phase: 03-web-ui
plan: 01
subsystem: web
tags: [fastapi, jinja2, htmx, picocss, uvicorn, sqlite]

requires:
  - phase: 02-pipeline-worker
    provides: "ScanWorker, JobStore, PaperlessClient, pipeline infrastructure"
provides:
  - "FastAPI app factory with lifespan (create_app)"
  - "MetadataCache with TTL get/set/invalidate"
  - "All API route handlers (health, scan, status, tags, correspondents, cache, history, flip)"
  - "JobStore.list_recent() and prune() methods"
  - "PaperlessClient.get_tags() and get_correspondents() methods"
  - "ScanWorker.is_alive property"
  - "OutputConfig web_host, web_port, paperless_cache_ttl_seconds fields"
  - "Template stubs for base, index, and all HTMX partials"
affects: [03-02, 03-03, 04-packaging]

tech-stack:
  added: [fastapi, uvicorn, python-multipart, jinja2]
  patterns: [app-factory-with-lifespan, ttl-metadata-cache, htmx-partial-templates, graceful-paperless-degradation]

key-files:
  created:
    - src/saneless/web/__init__.py
    - src/saneless/web/app.py
    - src/saneless/web/cache.py
    - src/saneless/web/routes.py
    - src/saneless/web/static/app.css
    - src/saneless/web/templates/base.html
    - src/saneless/web/templates/index.html
    - src/saneless/web/templates/partials/status.html
    - src/saneless/web/templates/partials/flip.html
    - src/saneless/web/templates/partials/history.html
    - src/saneless/web/templates/partials/tags.html
    - src/saneless/web/templates/partials/correspondents.html
  modified:
    - src/saneless/job.py
    - src/saneless/worker.py
    - src/saneless/paperless.py
    - src/saneless/config.py
    - pyproject.toml

key-decisions:
  - "response_model=None on /health to allow union return type (dict | JSONResponse)"
  - "Module-level Form default singleton to avoid B008 mutable default in function signature"
  - "S104 per-file ignore for config.py bind-all-interfaces (intentional LAN app)"

patterns-established:
  - "App factory pattern: create_app(settings, scanner) returns configured FastAPI"
  - "Graceful degradation: paperless API errors caught and return empty lists"
  - "HTMX partial templates: small HTML fragments for swap-based UI updates"
  - "Metadata cache: in-memory TTL cache with per-resource invalidation"

requirements-completed: [HLTH-01, HLTH-02, PLSS-05, UI-05, UI-06]

duration: 8min
completed: 2026-03-20
---

# Phase 03 Plan 01: Web Backend and FastAPI Scaffold Summary

**FastAPI app factory with health endpoint, metadata cache, scan/status/history API routes, and graceful paperless degradation**

## Performance

- **Duration:** 8 min
- **Started:** 2026-03-20T20:50:17Z
- **Completed:** 2026-03-20T20:58:40Z
- **Tasks:** 2
- **Files modified:** 17

## Accomplishments
- Extended JobStore with list_recent() and prune() for job history management
- Extended PaperlessClient with get_tags() and get_correspondents() for metadata dropdowns
- Created complete FastAPI web subpackage with app factory, TTL cache, and 10 route handlers
- Health endpoint returns 200/503 based on worker thread state
- All routes handle paperless-ngx unavailability gracefully with empty lists

## Task Commits

Each task was committed atomically:

1. **Task 1: Extend existing modules with web-layer methods** - `e469c5a` (feat)
2. **Task 2: Create FastAPI web subpackage with app factory, cache, and routes** - `055426f` (feat)

## Files Created/Modified
- `src/saneless/job.py` - Added list_recent() and prune() methods to JobStore
- `src/saneless/worker.py` - Added is_alive property to ScanWorker
- `src/saneless/paperless.py` - Added get_tags() and get_correspondents() to PaperlessClient
- `src/saneless/config.py` - Added paperless_cache_ttl_seconds, web_host, web_port to OutputConfig
- `pyproject.toml` - Added fastapi, uvicorn, python-multipart, jinja2 deps; S104 per-file ignore
- `src/saneless/web/__init__.py` - Web subpackage init with create_app export
- `src/saneless/web/app.py` - FastAPI app factory with lifespan management
- `src/saneless/web/cache.py` - MetadataCache with TTL-based get/set/invalidate
- `src/saneless/web/routes.py` - All 10 route handlers with graceful error handling
- `src/saneless/web/static/app.css` - Placeholder CSS overrides
- `src/saneless/web/templates/base.html` - Base template with PicoCSS + HTMX CDN
- `src/saneless/web/templates/index.html` - Main page template stub
- `src/saneless/web/templates/partials/*.html` - 5 partial templates for HTMX swaps

## Decisions Made
- Used `response_model=None` on /health endpoint to allow union return type (dict | JSONResponse) which FastAPI cannot serialize as a single Pydantic model
- Extracted Form(default=[]) to module-level `_TAGS_FORM_DEFAULT` singleton to satisfy ruff B008 (mutable default in function signature)
- Added S104 per-file ignore for config.py since binding to 0.0.0.0 is intentional for this LAN-only scanner appliance

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed FastAPI response model error on /health endpoint**
- **Found during:** Task 2 (routes.py)
- **Issue:** `dict[str, str] | JSONResponse` return type caused FastAPI to fail at import time with "Invalid args for response field"
- **Fix:** Added `response_model=None` to the `@router.get("/health")` decorator
- **Verification:** Import succeeds, app starts cleanly
- **Committed in:** 055426f (Task 2 commit)

**2. [Rule 1 - Bug] Fixed ruff B008 mutable default in Form parameter**
- **Found during:** Task 2 (routes.py)
- **Issue:** `Form(default=[])` in function signature triggers B008 (function call in default)
- **Fix:** Extracted to module-level `_TAGS_FORM_DEFAULT` singleton
- **Verification:** ruff check passes clean
- **Committed in:** 055426f (Task 2 commit)

---

**Total deviations:** 2 auto-fixed (2 bugs)
**Impact on plan:** Both fixes necessary for correctness. No scope creep.

## Issues Encountered
None beyond the auto-fixed items above.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- FastAPI backend complete with all route handlers
- Template stubs in place, ready for Plan 02 (full UI templates with HTMX)
- All existing 153 tests still pass

---
*Phase: 03-web-ui*
*Completed: 2026-03-20*
