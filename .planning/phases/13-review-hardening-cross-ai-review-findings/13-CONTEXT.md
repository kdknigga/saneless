# Phase 13: Review Hardening — Context

**Gathered:** 2026-03-22
**Status:** Ready for planning

<domain>
## Phase Boundary

Address 9 hardening items identified by cross-AI plan review (Gemini CLI). These are robustness, safety, and operational improvements — not new features. Each requirement (RH-01 through RH-09) maps to a specific concern raised during review of phases 1–12.

</domain>

<decisions>
## Implementation Decisions

### Disk space pre-flight (RH-01)
- **D-01:** Fixed headroom check — verify minimum free space in `tmp_dir` before starting any scan (flatbed or ADF)
- **D-02:** Hard fail — raise a clear error if insufficient space, no partial output, user retries after freeing space
- **D-03:** Threshold is configurable via `min_free_space_mb` config field (sensible default, e.g., 500MB)
- **D-04:** Check applies to all scan types (flatbed and ADF), not just multi-page

### Manual duplex mismatch recovery (RH-02)
- **D-05:** On page count mismatch, save both passes as separate PDFs (fronts.pdf and backs.pdf)
- **D-06:** Both partial PDFs are uploaded to paperless-ngx (not just consume directory)
- **D-07:** Job completes with DONE state (not ERROR), with a warning message like "Page count mismatch: 25 fronts, 24 backs. Partial PDFs saved."

### Typed state machine events (RH-03)
- **D-08:** Create a typed enum for worker state transition events (replaces string matching against log messages)
- **D-09:** Pipeline status callback passes enum events, not human-readable strings

### Exception sanitization (RH-04)
- **D-10:** `GET /api/paperless/test` 502 response returns sanitized error class name, never raw exception strings
- **D-11:** Full exception detail logged server-side only

### Config writability validation (RH-05)
- **D-12:** At startup, validate that `tmp_dir` and `consume_dir` (if set) are writable
- **D-13:** Fail fast with `ConfigError` if permission check fails — don't wait until mid-scan

### Periodic job pruning (RH-06)
- **D-14:** Prune job history after each scan job completes (piggyback, no extra threads)
- **D-15:** No configurable interval — prune runs on every job completion using existing max_age/max_rows settings

### Empty page detection toggle (RH-07)
- **D-16:** Add `enable_empty_page_detection: bool = True` to `ProfileConfig`
- **D-17:** When False, skip empty page filtering entirely — no threshold manipulation needed

### Uvicorn signal handler fix (RH-08)
- **D-18:** Test server Uvicorn config disables signal handlers when running in non-main thread

### Docker Compose documentation (RH-09)
- **D-19:** Add comments to docker-compose.yml warning that config.toml must exist on host before first run

### Claude's Discretion
- Exact disk space estimation formula and default threshold value
- Enum naming and member names for typed state events
- Sanitization function implementation details
- Config validation approach (test write vs. `os.access`)
- File naming pattern for partial duplex PDFs (e.g., `{title}_fronts.pdf`)
- Where to place the prune() call in the worker flow

</decisions>

<specifics>
## Specific Ideas

- Duplex mismatch recovery: user explicitly wants both passes saved as separate PDFs, not best-effort interleave — the user can manually combine later in paperless-ngx
- Duplex mismatch should still upload to paperless-ngx — user prefers reviewing in paperless over hunting in consume directory
- Periodic pruning: piggyback on job completion, no daemon threads — keeps the system simple

</specifics>

<canonical_refs>
## Canonical References

### Review findings (source of all 9 requirements)
- `.planning/REVIEWS.md` — Full cross-AI review with per-phase concerns and suggestions

### Requirements
- `.planning/REQUIREMENTS.md` — RH-01 through RH-09 requirement definitions

### Existing implementation (integration points)
- `src/saneless/pipeline.py` — Scan pipeline, disk space check insertion point, duplex interleave logic
- `src/saneless/worker.py` — State transitions (currently string-based), job completion flow
- `src/saneless/config.py` — ProfileConfig, Settings, config validation
- `src/saneless/web/routes.py` — Paperless test endpoint (502 response)
- `src/saneless/web/app.py` — App factory, lifespan (current startup-only pruning)
- `src/saneless/job.py` — JobState enum, ErrorCategory enum (pattern for new enums)
- `src/saneless/pdf.py` — PDF assembly (reusable for partial PDFs)
- `src/saneless/paperless.py` — Consume directory fallback pattern
- `docker-compose.yml` — Docker Compose config (RH-09)
- `tests/test_browser.py` — Uvicorn test server setup (RH-08)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `assemble_pdf()` in `pdf.py` — already handles arbitrary page lists, reusable for partial duplex PDFs
- `JobState` and `ErrorCategory` enums in `job.py` — established `StrEnum` pattern for RH-03
- `ConfigError` in `exceptions.py` — ready for RH-05 validation failures
- Consume directory fallback in `paperless.py` — `shutil.copy2` pattern, though RH-02 will upload instead
- `@field_validator` pattern in `config.py` — template for RH-05 writability validators

### Established Patterns
- `StrEnum` for all enums (Python 3.14 stdlib)
- Pydantic field validators for config validation
- `notify()` callback in pipeline for status updates
- Worker `_status_cb()` for mapping pipeline events to job state transitions
- `_transition_event` threading.Event for signaling state changes

### Integration Points
- Pipeline `run_pipeline()` — insert disk space check at top, before temp directory creation
- Pipeline `_interleave_duplex()` — replace raise with partial PDF assembly + upload
- Worker `_status_cb()` — refactor from string matching to enum dispatch
- Worker `_run_job()` — add `job_store.prune()` call after job completion
- Routes `paperless_test()` — wrap exception in sanitization function
- Settings class — add `min_free_space_mb` field and writability validator
- ProfileConfig — add `enable_empty_page_detection` field
- Pipeline empty page filter — gate on new boolean field

</code_context>

<deferred>
## Deferred Ideas

- SQLite WAL mode (raised in Phase 3 review) — not in RH requirements, could be separate phase
- Orphaned timeout threads in scanner executor (Phase 2 review) — complex, not in scope
- Port conflict handling for `saneless serve` (Phase 5 review) — nice-to-have, not in scope

</deferred>

---

*Phase: 13-review-hardening-cross-ai-review-findings*
*Context gathered: 2026-03-22*
