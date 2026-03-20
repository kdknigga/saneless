# Architecture

**Analysis Date:** 2026-03-20

## Pattern Overview

**Overall:** Two-tier layered architecture with a FastAPI web layer and background worker thread.

**Key Characteristics:**
- Single process with two internal components: web layer (FastAPI) and worker thread
- Web layer is always responsive and never blocks on hardware I/O
- Worker thread owns the scanner resource and runs the full pipeline (scan → assemble → ingest)
- Job queue using `queue.Queue` for coordination between layers
- Scanner access abstracted behind an interface layer to support multiple backends (SANE, future driverless backends)
- All job state persisted to SQLite for durability and history tracking

## Layers

**Web Layer (FastAPI):**
- Purpose: Handles HTTP requests from browser/CLI, serves web UI, enqueues scan jobs, exposes job status API
- Location: `src/saneless/` (to be created)
- Contains: FastAPI application, request handlers, health checks, metadata fetching (tags/correspondents), job status endpoints
- Depends on: Job queue, SQLite database for job history
- Used by: Browser clients, CLI tools, external health check systems

**Worker Thread:**
- Purpose: Processes queued scan jobs through complete pipeline without blocking web layer
- Location: `src/saneless/` (to be created)
- Contains: Job dequeue loop, scanner abstraction calls, image processing, PDF assembly, paperless-ngx API calls, job state updates
- Depends on: Scanner abstraction layer, `img2pdf`, `httpx` client, SQLite
- Used by: Web layer via job queue

**Scanner Abstraction Layer:**
- Purpose: Isolate scanner access behind a pluggable interface; first implementation uses `python-sane`/`saned`, future implementations can support driverless protocols
- Location: `src/saneless/` (to be created)
- Contains: Device enumeration, capability queries, page acquisition methods
- Depends on: `python-sane` (v1 implementation)
- Used by: Worker thread, CLI device listing commands

**Configuration Layer:**
- Purpose: Parse and manage configuration from TOML file and environment variables
- Location: `src/saneless/` (to be created)
- Contains: Pydantic settings classes for scanner, paperless, output, and profile definitions
- Depends on: `pydantic-settings`
- Used by: All layers for accessing settings

**Persistence Layer:**
- Purpose: Store and retrieve job history and state
- Location: `src/saneless/` (to be created)
- Contains: SQLite schema, job record model, query methods
- Depends on: `sqlite3` (stdlib)
- Used by: Web layer (history display), worker thread (state updates)

## Data Flow

**Scan Job Submission:**

1. User clicks Scan button in web UI (or invokes `saneless scan` CLI)
2. Web handler validates metadata, creates Job record in SQLite, enqueues job to `queue.Queue`
3. Web handler immediately returns job ID to client; client begins polling `/api/jobs/{id}`
4. Control returns to web handler; request completes within 500ms

**Scan Job Processing:**

1. Worker thread dequeues job from `queue.Queue`
2. Updates job state to "scanning" in SQLite
3. Acquires pages via scanner abstraction layer (handles single page, ADF, ADF duplex, ADF manual duplex)
4. For ADF Manual Duplex: after pass A completes, sets job state to "awaiting_flip" and blocks on `threading.Event`
5. Web layer detects "awaiting_flip" state and shows UI prompt
6. User clicks Continue; web layer sets event; worker resumes and acquires pass B
7. After page acquisition, generates thumbnail and stores as base64 in job record
8. Updates job state to "assembling", calls `img2pdf` to create PDF from pages
9. Updates job state to "uploading", sends PDF to paperless-ngx via REST API
10. Polls `GET /api/tasks/` until task reaches terminal state
11. Updates job state to "done" or "error" with final status
12. Web client polls status endpoint and displays result

**State Management:**

- Job state is the source of truth; stored in SQLite with JSON field for details
- Worker thread updates state after each phase completion
- Web layer reads state from database when serving status API responses
- No in-memory state shared between requests; all state persisted
- Thumbnails and error messages stored as JSON in job record for retrieval

## Key Abstractions

**Job Model:**
- Purpose: Represents a single scan task with metadata, state, and results
- Examples: `src/saneless/models.py` (to be created)
- Pattern: Pydantic dataclass with SQLite serialization; immutable input data + mutable status/results

**ScannerBackend (Interface):**
- Purpose: Abstract scanner operations for pluggability
- Examples: `src/saneless/scanner/backend.py` (interface), `src/saneless/scanner/sane.py` (implementation)
- Pattern: Abstract base class with methods: `get_devices()`, `get_capabilities(device)`, `scan(device, settings)` yields pages

**Page Assembly Pipeline:**
- Purpose: Convert raw PIL Images to final PDF with metadata
- Examples: `src/saneless/pipeline/pages.py` (processing), `src/saneless/pipeline/pdf.py` (assembly)
- Pattern: Generator-based page processing (empty page detection, thumbnail extraction), then batch assembly with `img2pdf`

**Paperless Ingestion:**
- Purpose: Send scanned PDF to paperless-ngx with metadata
- Examples: `src/saneless/paperless/client.py` (REST API client)
- Pattern: Async HTTP client (httpx) for document upload and task polling; retry logic with exponential backoff

**Profile Configuration:**
- Purpose: Named scan settings templates (resolution, color mode, default metadata)
- Examples: In `src/saneless/config.py` as Pydantic nested models
- Pattern: TOML-nested config sections mapped to dataclasses; profiles referenced by name in web UI and CLI

## Entry Points

**Web Server:**
- Location: `src/saneless/app.py` (to be created)
- Triggers: Running `saneless` CLI without subcommand, or `saneless serve`
- Responsibilities: Start FastAPI application on configurable port (default 8080), create worker thread, expose web UI and REST API

**CLI Commands:**
- Location: `src/saneless/cli.py` (to be created)
- Triggers: `saneless scan`, `saneless devices`, `saneless jobs`
- Responsibilities: Parse arguments, communicate with web server REST API or run directly if server not running

**Worker Thread:**
- Location: Worker thread created by web app in `src/saneless/app.py`
- Triggers: Automatically started on application initialization
- Responsibilities: Continuously dequeue and process jobs from `queue.Queue`

## Error Handling

**Strategy:** Three-tier error handling:
1. Fail fast with specific user-facing messages (out-of-paper, network unreachable)
2. Log all errors with full context to rotating log file
3. Store error details in job record for UI display

**Patterns:**

- **SANE errors:** Catch `sane.error` on first `multi_scan()` iteration; inspect error string for out-of-paper indicators; translate to "No paper detected in feeder" message. Other SANE errors reported with full error string.
- **Network errors:** Distinguish between unreachable server (DNS/connection error), token rejected (401/403), and valid connection. Reported as "Server unreachable", "Token rejected", or "Connection successful" in Test Connection UI.
- **File I/O errors:** Cleanup temporary files on error; log with full traceback.
- **API errors:** Log HTTP status and response body; display user-friendly message in UI.
- **Empty page detection failure:** Log configuration parameters and computed values; help user adjust thresholds.

## Cross-Cutting Concerns

**Logging:**
- Framework: Python `logging` module with rotating file handler
- Path: Configurable, default `/var/log/saneless/saneless.log`
- Level: Configurable (`DEBUG`, `INFO`, `WARNING`, `ERROR`); default `INFO`
- Format: Timestamp, level, module name, message
- Used by: All layers for audit trail and debugging

**Validation:**
- Configuration validation: Pydantic at startup; invalid config raises `ValidationError` before server starts
- Job metadata validation: FastAPI request validation; invalid requests return 422 Unprocessable Entity
- API response validation: Pydantic models for paperless-ngx API responses

**Authentication:**
- Scanner access: No authentication; assumes `saned` on trusted LAN
- Paperless-ngx API: Token-based authentication via `Authorization: Token` header
- Web UI: No authentication in v1 (assumes trusted LAN); authentication deferred to reverse proxy

---

*Architecture analysis: 2026-03-20*
