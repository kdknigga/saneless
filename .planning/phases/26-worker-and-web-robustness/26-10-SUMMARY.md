---
phase: 26-worker-and-web-robustness
plan: 10
subsystem: web
tags: [fastapi, threadpool, health, cache, single-flight, validation, rejection, htmx]
requires:
  - phase: 26-01
    provides: SubmitResult, WorkerHealth, worker_health_detail, RequestRejection, TITLE_MAX_LENGTH, *_JOB_ERROR texts, ErrorCategory.REJECTED, JobStore.latest_run_job
  - phase: 26-04
    provides: non-blocking ScanWorker.submit -> SubmitResult, profile lock with profile_names/has_profile
  - phase: 26-05
    provides: RequestRejected(rejection, refresh_history) and the one error renderer
  - phase: 26-06
    provides: ScanWorker.health (DOWN / DEGRADED / HEALTHY)
provides:
  - All route handlers as plain def (threadpool), with a structural guard test
  - /health backed by WorkerHealth with distinct 503 details
  - MetadataCache.get_or_fetch single-flight
  - start_scan validation (unknown profile, title cap) before any job row
  - 429/503 rejections recorded as REJECTED job rows, history reload only when written
  - Status-area fallback that skips REJECTED rows (D-06)
  - MetadataResource Literal on /api/cache/invalidate
affects: [26-11, 26-13, 26-14]
tech-stack:
  added: []
  patterns:
    - "Route raises RequestRejected; errors.py renders (no error HTML in routes)"
    - "Total match on SubmitResult / WorkerHealth with assert_never"
    - "Per-key lock + re-check single-flight cache; failures not cached"
key-files:
  created: []
  modified:
    - src/saneless/web/routes.py
    - src/saneless/web/cache.py
    - tests/test_web.py
    - tests/test_cache.py
    - tests/test_web_errors.py
decisions:
  - "_record_rejected_submit takes a frozen _ScanForm dataclass instead of four loose fields, keeping it under ruff's PLR0913 argument limit"
  - "Health precheck mapped by a separate _unhealthy_rejection(WorkerHealth) total match returning None for HEALTHY, so start_scan keeps one assert_never(result) for SubmitResult"
  - "Degraded-worker tests patch the ScanWorker.health property instead of setting the _degraded Event, so the worker thread's idle recovery probe cannot clear it mid-request"
metrics:
  duration: ~25min
  completed: 2026-09-14
  tasks: 2
  files: 5
---

# Phase 26 Plan 10: Routes robustness (def handlers, honest /health, validation, rejections) Summary

Every route is now a threadpool `def` handler. `/health` reports `WorkerHealth` truthfully, and the metadata cache fetches once per key. `/api/scan` refuses bad input with a 422 before writing any row. A refused submit is a visible 429 or 503, recorded in history as a REJECTED row that never takes over the status area.

## Tasks

| Task | Name | Commits |
| ---- | ---- | ------- |
| 1 | def handlers, honest /health, single-flight cache (ROBU-05) | 193da1c (RED), 79cafd4 (GREEN) |
| 2 | Validation before any row, 429/503 rejection rows, D-06 lookup | 858c7cf (RED), e795fe1 (GREEN) |

## What changed

- **routes.py**
  - All 11 handlers went from `async def` to `def`. A comment above `router` explains why and names the locks that protect the shared state.
  - `/health` returns 200 `{"status": "ok"}` when the worker is healthy. Otherwise it returns 503 with `worker_health_detail(...)`.
  - `_get_cached_or_fetch` now delegates to `cache.get_or_fetch`. It still falls back to a WARNING and `[]`.
  - `start_scan` runs its checks in this order:
    1. `has_profile`: an unknown profile raises UNKNOWN_PROFILE.
    2. The empty title gets its default.
    3. Health precheck: DOWN or DEGRADED writes a REJECTED row, then raises 503.
    4. `create_job`.
    5. `submit`, matched on `SubmitResult`: ACCEPTED renders, QUEUE_FULL gives 429, DOWN and DEGRADED give 503. Each refusal writes a REJECTED row first.
  - `refresh_history` is set only when the rejection row was actually written.
  - `_current_or_recent_job` now uses `job_store.latest_run_job()`.
  - `index` lists `worker.profile_names()`.
  - `invalidate_cache` and `_get_cached_or_fetch` take `MetadataResource = Literal["tags", "correspondents"]`.
- **cache.py:** `get_or_fetch` uses a per-key lock, created under `_locks_guard`, and re-checks the cache after taking the lock. A raising fetch caches nothing. The docstring notes the known limitation that threads retry one after another while Paperless is unreachable (deferred, M-01 second half).

## Verification

- `uv run pytest -m "not browser and not sane_hardware" -q`: 1342 passed
- `uv run ruff check src tests`, `uv run ruff format --check .`, `uv run ty check`, `uv run pyrefly check src tests`: all clean. pyrefly shows 4 warnings, all in untouched files (pipeline.py, sane_backend.py, fake_sane.py).
- `uv run prek run --stage pre-push --all-files`: passed
- Acceptance greps:
  - 0 `^async def`
  - one each of `latest_run_job()`, `Form(max_length=TITLE_MAX_LENGTH)`, the `MetadataResource` Literal, `assert_never(result)` and `worker_health_detail(`
  - no `list_recent(limit=1)` and no `state.settings.profiles`
- `-k "422 or 429 or queue_full or 503"` in test_web_errors.py selects 40 tests. `-k rejected` in test_web.py selects 2. `-k get_or_fetch` selects 3.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Structural coroutine test leaked an unclosed sqlite connection**
- **Found during:** Task 1 GREEN
- **Issue:** `test_no_route_handler_is_a_coroutine` used the bare `app` fixture, so the lifespan never ran and the JobStore connection was never closed. The next test's setup then failed with `PytestUnraisableExceptionWarning`.
- **Fix:** The test now uses the `client` fixture, so the lifespan closes the store.
- **Files modified:** tests/test_web.py
- **Commit:** 79cafd4

**2. [Rule 1 - Bug] Degraded /health test could race the idle recovery probe**
- **Found during:** Task 2 RED
- **Issue:** Setting `worker._degraded` directly could be cleared by the worker thread's idle probe (26-06), which would make the test flaky.
- **Fix:** The test now monkeypatches the `ScanWorker.health` property instead. The new 503 route tests use the same approach.
- **Files modified:** tests/test_web.py, tests/test_web_errors.py
- **Commit:** 858c7cf

**3. [Rule 3 - Blocking] ty could not prove the SubmitResult match exhaustive**
- **Found during:** Task 2 GREEN
- **Issue:** `request.app.state` is `Any`, so ty rejected `assert_never(result)`.
- **Fix:** Annotated `result: SubmitResult`.
- **Commit:** e795fe1

### Additions

- Added `test_metadata_fetch_failure_falls_back_to_an_empty_list` to cover the plan's "unchanged fallback" behaviour, which had no test.
- Added `test_scan_unhealthy_worker_is_503_before_submit[down|degraded]`. It covers the health-precheck path when the store accepts the row: the row is written, the loader is present and `submit` is never called.

## TDD Gate Compliance

Both tasks follow RED then GREEN in git history: 193da1c then 79cafd4, and 858c7cf then e795fe1. A few RED-phase tests passed before implementation because they describe behaviour that already existed:
- `test_health_reports_down_worker`: the old handler already returned "worker thread is down".
- `test_scan_title_at_the_cap_is_not_422`: at-cap titles were already accepted.
- `test_index_lists_the_worker_profiles`: the worker's settings object is shared with `app.state.settings`.

Every test covering new behaviour failed in RED.

## Known Stubs

None.

## Self-Check: PASSED

- src/saneless/web/routes.py, src/saneless/web/cache.py, tests/test_web.py, tests/test_cache.py, tests/test_web_errors.py: present
- Commits 193da1c, 79cafd4, 858c7cf, e795fe1: present in `git log`
