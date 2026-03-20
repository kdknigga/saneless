# Phase 3: Web UI - Context

**Gathered:** 2026-03-20
**Status:** Ready for planning

<domain>
## Phase Boundary

Browser interface for all scanning operations accessible from any device on the LAN. Includes profile selector, metadata entry (title, tags, correspondent), scan button with live status feedback, manual duplex flip prompt, first-page thumbnail preview, job history table with SQLite persistence and pruning, and a health endpoint. No authentication (trusted LAN). Packaging, consume directory fallback, and CLI `jobs` command are separate phases.

</domain>

<decisions>
## Implementation Decisions

### Rendering approach
- Jinja2 server-side templates + HTMX for dynamic behavior — no client-side SPA, no JS build step
- Single-page layout: all UI (scan form, status area, job history) on one page
- HTMX handles all dynamic interactions: form submission, status polling, dropdown refresh, flip prompt actions
- Status polling via `hx-trigger="every 1s"` on the status area during active scan; no polling when idle (conditionally enabled via HTMX response headers or CSS class toggle)
- Tag/correspondent dropdowns use `hx-get` for initial population and per-resource refresh — clicking refresh icon triggers `POST /api/cache/invalidate?resource=tags` then `hx-get` to re-fetch options, swapping just that dropdown without full page reload
- Flip prompt rendered as an HTMX-swapped partial: when status endpoint returns `awaiting_flip`, the status area swaps to show the flip prompt with Continue and Cancel buttons that POST back to the API
- Flip illustration as inline SVG embedded in the template — shows correct long-edge flip vs incorrect short-edge flip per PRD §7.6

### Styling & design
- PicoCSS as the CSS foundation — classless/minimal, clean modern look, no build step required
- Auto dark/light mode via `prefers-color-scheme` (PicoCSS supports natively)
- Responsive from the start — single-column layout on mobile, form and status stack vertically
- Clean utility aesthetic — minimal chrome, content-first, appropriate for self-hoster/homelab audience
- Custom CSS limited to app-specific layout (status area, thumbnail placement, flip illustration sizing)

### Status feedback
- Live status indicator in a dedicated status area below the scan form
- Shows current state text + appropriate visual indicator (spinner for scanning/assembling/uploading, checkmark for done, X for error)
- Thumbnail appears inline in status area as soon as first page is scanned, stays visible through job completion
- Error display: inline error banner in status area with full error message for current/most recent job
- Scan button disabled with visual change (text changes to "Scanning..." or similar) while job in progress; re-enabled on completion or error

### Job history
- Simple HTML table below status area: timestamp, profile, title, status columns
- Most recent jobs first, no client-side pagination (SQLite pruning keeps row count <= 500)
- Table refreshed via HTMX after each scan job completes
- Status column shows success/error with color coding

### Health endpoint
- `GET /health` returns 200 `{"status": "ok"}` when web layer alive and worker thread running
- Returns 503 when worker thread is down or deadlocked
- No authentication required
- Separate from the UI — pure JSON API for container orchestration health checks

### Claude's Discretion
- Exact HTMX attribute patterns and swap strategies
- PicoCSS customization variables (accent color, border radius)
- SVG flip illustration design details
- Polling conditional logic implementation (how to start/stop polling based on job state)
- FastAPI route organization (single router vs multiple)
- Jinja2 template structure (single file vs partials)
- Status area transition animations (if any)
- Job history table column widths and date formatting

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Product requirements
- `docs/PRD.md` — Full product requirements document
- `docs/PRD.md` §7.6 — Web UI requirements: health endpoint, test connection, profile selector, metadata fields, scan button, live status, flip prompt with illustration, thumbnail preview, job history with pruning
- `docs/PRD.md` §7.8 — Error handling: all errors displayed in web UI for current/most recent job
- `docs/PRD.md` §8 — Non-functional requirements: 500ms response time for scan button, single concurrent scan
- `docs/PRD.md` §9.1 — Process model: FastAPI web layer + worker thread architecture
- `docs/PRD.md` §9.2 — Component diagram: web layer, job queue, worker thread, SQLite

### Prior phase context
- `.planning/phases/01-core-pipeline/01-CONTEXT.md` — Phase 1 decisions: CLI output, scanner abstraction, config, paperless upload behavior
- `.planning/phases/02-adf-and-multi-page/02-CONTEXT.md` — Phase 2 decisions: ADF page acquisition, manual duplex coordination, empty page detection, thumbnail generation

### Research findings
- `.planning/research/PITFALLS.md` — Scanner and library pitfalls that affect web layer error handling
- `.planning/research/ARCHITECTURE.md` — Component boundaries and data flow

### Code conventions
- `.planning/codebase/CONVENTIONS.md` — Naming, style, imports, error handling, docstring patterns

### Requirements
- `.planning/REQUIREMENTS.md` — Full requirements list with traceability (UI-01 through UI-08, PROF-03, PLSS-04, PLSS-05, HLTH-01, HLTH-02, LOG-03)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `src/saneless/job.py`: `Job` dataclass with full lifecycle states including `AWAITING_FLIP`, `JobStore` with SQLite persistence — direct reuse for all job operations
- `src/saneless/worker.py`: `ScanWorker` with `queue.Queue`, `threading.Event` for flip coordination — web layer submits to this queue and reads job state
- `src/saneless/config.py`: `Settings` with `ProfileConfig`, `PaperlessConfig`, `ScannerConfig` — profiles list available for dropdown population
- `src/saneless/paperless.py`: `PaperlessClient` with connection test, tag/correspondent fetching — reuse for metadata dropdowns and test connection button
- `src/saneless/pipeline.py`: `run_pipeline()` with status callbacks — callbacks can update job state for web polling
- `src/saneless/pages.py`: Thumbnail generation (base64 JPEG) — already produces the format needed for web display

### Established Patterns
- Job state machine: PENDING → SCANNING → ASSEMBLING → UPLOADING → DONE (+ AWAITING_FLIP branch, ERROR terminal)
- SQLite `check_same_thread=False` for cross-thread access (worker writes, web reads)
- pydantic-settings for all configuration with TOML + env var override
- Custom exception hierarchy: `SanelessError` → `ScanError`, `ConfigError`, `PaperlessError`
- `logging.getLogger(__name__)` at module level in every module

### Integration Points
- `ScanWorker.submit(job)` — web handler creates Job, submits to worker queue
- `JobStore.get(job_id)` / `JobStore.list_recent()` — web handler reads job state for status endpoint and history
- `ScanWorker._flip_event` — web handler signals continue/cancel for manual duplex
- `Settings.profiles` — web handler reads profile list for dropdown
- `PaperlessClient.get_tags()` / `PaperlessClient.get_correspondents()` — web handler fetches for dropdowns with TTL cache

</code_context>

<specifics>
## Specific Ideas

- PRD specifies exact flip prompt wording: "Keep the pages in the same order, then flip the stack over the long edge (the left or right side, not the top or bottom)"
- PRD requires both written instruction AND illustrative graphic showing correct vs incorrect flip axis
- Tag/correspondent cache refresh must be per-resource (`POST /api/cache/invalidate?resource=tags`) not global flush
- Job history pruning: age (default 7 days) AND count (default 500) — whichever is more restrictive

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 03-web-ui*
*Context gathered: 2026-03-20*
