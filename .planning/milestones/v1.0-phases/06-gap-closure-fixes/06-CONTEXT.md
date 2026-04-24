# Phase 6: Gap Closure Fixes - Context

**Gathered:** 2026-03-20
**Status:** Ready for planning

<domain>
## Phase Boundary

Close two remaining v1 requirement gaps: (1) expose `PaperlessClient.test_connection()` as a web route `GET /api/paperless/test` (PLSS-03), and (2) make the worker emit ASSEMBLING and UPLOADING intermediate states visible to the web UI status polling (UI-02). No new features, no UI redesign, no pipeline restructuring.

</domain>

<decisions>
## Implementation Decisions

### Paperless test route (PLSS-03)
- New route `GET /api/paperless/test` in `routes.py` returning JSON `{"status": "connected"}`, `{"status": "token_rejected"}`, or `{"status": "unreachable"}`
- Calls existing `PaperlessClient.test_connection()` — no new client logic needed
- No authentication required (consistent with `/health` endpoint and trusted LAN assumption)
- Pure API endpoint — no HTML template, no UI button (UI integration would be a separate capability)
- Catches all exceptions gracefully, returns JSON error response — never raises to caller

### Worker intermediate states (UI-02)
- Worker's `_status_cb` already maps "Awaiting flip..." to `JobState.AWAITING_FLIP` — extend this pattern to also map "Assembling PDF..." → `JobState.ASSEMBLING` and "Uploading to paperless-ngx..." → `JobState.UPLOADING`
- No pipeline changes needed — `run_pipeline()` already calls `notify("Assembling PDF...")` and `notify("Uploading to paperless-ngx...")` at the right points
- Templates in `partials/status.html` already handle ASSEMBLING and UPLOADING states with correct labels
- State transitions logged via existing `JobStore.update_state()` debug logging

### Claude's Discretion
- Exact test route error handling structure (try/except scope, logging level)
- Test organization — whether to add tests to existing test files or create new ones
- Whether to add a timeout to the paperless test connection call

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements
- `.planning/REQUIREMENTS.md` — PLSS-03 (paperless test endpoint), UI-02 (live status intermediate states)

### Existing implementation (gap sources)
- `src/saneless/paperless.py` — `PaperlessClient.test_connection()` method (line 205) — already implements the three-state logic
- `src/saneless/web/routes.py` — All existing route patterns, especially `/health` (line 103) as model for `/api/paperless/test`
- `src/saneless/worker.py` — `_status_cb` (line 132) — currently only handles "Awaiting flip...", needs ASSEMBLING/UPLOADING
- `src/saneless/pipeline.py` — `run_pipeline()` lines 265-270 — already emits "Assembling PDF..." and "Uploading to paperless-ngx..." status callbacks
- `src/saneless/job.py` — `JobState` enum (line 24) — already defines ASSEMBLING and UPLOADING values
- `src/saneless/web/templates/partials/status.html` — Already renders ASSEMBLING and UPLOADING state labels

### Prior phase context
- `.planning/phases/03-web-ui/03-CONTEXT.md` — Web UI architecture, route patterns, HTMX polling design
- `.planning/phases/05-web-server-launch/05-CONTEXT.md` — Most recent phase, confirms no changes to routes or worker

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `PaperlessClient.test_connection()`: Complete three-state connection test — route just needs to expose it
- `_status_cb` pattern in worker: String-matching callback that maps messages to JobState — add two more mappings
- `partials/status.html`: Already handles all seven JobState values including ASSEMBLING and UPLOADING

### Established Patterns
- API routes return JSON via `JSONResponse` or dict (see `/health` pattern)
- Worker state updates via `self._job_store.update_state(job_id, JobState.X)`
- Status callback strings from pipeline are stable, well-defined constants

### Integration Points
- Route registration: `router = APIRouter()` in `routes.py`, auto-included by app factory
- Worker status callback: `_status_cb` closure in `ScanWorker._run()`
- No new dependencies, no new files needed — all changes are additions to existing modules

</code_context>

<specifics>
## Specific Ideas

- Both gaps are surgical: one new route (5-10 lines), two new string matches in worker callback (4-6 lines)
- The infrastructure for both features is already built — these are just the missing wiring
- Total scope is extremely small — this could reasonably be a single plan

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 06-gap-closure-fixes*
*Context gathered: 2026-03-20*
