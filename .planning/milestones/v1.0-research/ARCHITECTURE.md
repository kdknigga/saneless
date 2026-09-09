# Architecture Research

**Domain:** SANE scanner-to-paperless-ngx bridge
**Researched:** 2026-03-20
**Confidence:** HIGH

## Component Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                      saneless process                        │
│                                                              │
│  ┌──────────────────────┐    ┌───────────────────────────┐  │
│  │   FastAPI Web Layer   │    │     Worker Thread          │  │
│  │                       │    │                            │  │
│  │  /api/scan (POST)     │───>│  queue.Queue               │  │
│  │  /api/jobs/{id} (GET) │<──>│  ┌─────────────────────┐  │  │
│  │  /api/paperless/test  │    │  │ Scanner Abstraction  │  │  │
│  │  /api/cache/invalidate│    │  │  ├─ SaneBackend      │  │  │
│  │  /health              │    │  │  └─ (future backends)│  │  │
│  │  Static UI (HTML/JS)  │    │  └─────────────────────┘  │  │
│  │                       │    │  ┌─────────────────────┐  │  │
│  │  Paperless metadata   │    │  │ Page Pipeline        │  │  │
│  │  cache (TTL per-      │    │  │  ├─ empty detection  │  │  │
│  │  resource)            │    │  │  ├─ thumbnail gen    │  │  │
│  │                       │    │  │  └─ img2pdf assembly │  │  │
│  └──────────┬────────────┘    │  └─────────────────────┘  │  │
│             │                 │  ┌─────────────────────┐  │  │
│             │                 │  │ Paperless Client     │  │  │
│             │                 │  │  ├─ upload (httpx)   │  │  │
│             │                 │  │  └─ task polling     │  │  │
│             │                 │  └─────────────────────┘  │  │
│             │                 └───────────┬───────────────┘  │
│             │                             │                   │
│  ┌──────────▼─────────────────────────────▼──────────────┐  │
│  │                    SQLite (job store)                   │  │
│  │  jobs: id, state, profile, metadata, thumbnail, error  │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐  │
│  │              Config (pydantic-settings)                 │  │
│  │  TOML file + env var overrides                         │  │
│  └────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
         │                              │
    ┌────▼────┐                   ┌─────▼──────┐
    │ Browser │                   │   saned    │
    │  / CLI  │                   │  (scanner) │
    └─────────┘                   └────────────┘
         │
    ┌────▼────────────┐
    │  paperless-ngx  │
    │  (REST API)     │
    └─────────────────┘
```

## Data Flows

### Happy Path — Single Page Flatbed Scan

1. User selects profile, fills metadata, clicks Scan
2. Web layer creates Job record (state=`queued`) in SQLite, enqueues to `queue.Queue`
3. Returns job ID immediately (< 500ms)
4. Worker dequeues, updates state to `scanning`
5. Opens SANE device via scanner abstraction, acquires single page as PIL Image
6. Generates thumbnail (JPEG, long edge ≤ 300px), stores base64 in job record
7. Updates state to `assembling`, converts to PDF via `img2pdf`
8. Updates state to `uploading`, POSTs PDF + metadata to paperless-ngx REST API
9. Polls paperless-ngx task endpoint until terminal state
10. Updates state to `done` or `error`, cleans up temp files

### ADF Manual Duplex Flow

1. Worker acquires all front pages (pass A) via `multi_scan()`
2. Records raw page count for pass A
3. Updates state to `awaiting_flip`, blocks on `threading.Event`
4. Web layer detects state, shows flip prompt with illustration
5. User flips stack (long-edge flip), clicks Continue
6. Web layer sets `threading.Event`, worker resumes
7. Worker acquires all back pages (pass B) via `multi_scan()`
8. Validates raw page count A == raw page count B (fail if mismatch)
9. Applies empty page detection to both passes
10. Interleaves: A1, B_reversed[0], A2, B_reversed[1], ...
11. Continues to PDF assembly

### Empty Page Detection

For each page image:
1. Convert to grayscale
2. Compute mean luminance L_mean and standard deviation σ_L
3. Discard if L_mean > T_mean AND σ_L < T_std
4. Both thresholds independently configurable per profile

## Component Design

### Scanner Abstraction (Protocol/ABC)

```python
class ScannerBackend(ABC):
    def get_devices(self) -> list[DeviceInfo]: ...
    def get_capabilities(self, device_id: str) -> DeviceCapabilities: ...
    def scan_pages(self, device_id: str, settings: ScanSettings) -> Iterator[Image]: ...
```

- v1 implementation: `SaneBackend` wrapping `python-sane`
- Device opened per-job, closed after scan completes (never held open between jobs)
- `scan_pages` yields PIL Images; caller handles assembly

### Job State Machine

```
queued → scanning → [awaiting_flip →] assembling → uploading → done
                                                              ↘ error
Any state can transition to error.
```

States stored in SQLite `jobs.state` column. Additional data (thumbnail, error message, page counts) stored in JSON `jobs.details` column.

### Paperless-ngx Client

- `test_connection()` → 3 outcomes: unreachable, token_rejected, connected
- `upload_document(pdf_path, metadata)` → task_id
- `poll_task(task_id)` → terminal state
- `get_tags()` / `get_correspondents()` → cached with per-resource TTL
- Uses `httpx` (sync client in worker thread, no async needed)

### SQLite Schema

```sql
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'queued',
    profile TEXT NOT NULL,
    title TEXT,
    tags TEXT,  -- JSON array of IDs
    correspondent_id INTEGER,
    thumbnail TEXT,  -- base64 JPEG
    error TEXT,
    details TEXT,  -- JSON blob for extensible data
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_jobs_state ON jobs(state);
CREATE INDEX idx_jobs_created ON jobs(created_at);
```

## Patterns to Follow

| Pattern | Why | Where |
|---------|-----|-------|
| Dedicated worker thread (not asyncio for scanning) | `python-sane` is blocking C code; running in asyncio event loop risks deadlocks | Worker thread |
| Context-managed scanner sessions | Open device → scan → close device per job; never hold open between jobs | Scanner abstraction |
| Temp directory per job | Isolates temp files; easy cleanup on error via `shutil.rmtree` | Worker pipeline |
| State machine with explicit transitions | Clear job lifecycle; easy to debug; UI can show appropriate controls per state | Job model |
| Per-resource cache invalidation | Refreshing tags doesn't evict correspondents and vice versa | Paperless client |

## Anti-Patterns to Avoid

| Anti-Pattern | Risk | Mitigation |
|--------------|------|------------|
| Running python-sane on asyncio event loop | Blocks entire event loop during scan; deadlocks possible | Always use dedicated thread |
| Holding SANE device open between jobs | Device locked; other tools can't access scanner; resource leak | Open/close per job |
| Storing page images in SQLite | Database bloat; slow queries | Store only thumbnail (base64); pages in temp dir |
| Polling paperless-ngx without backoff | Hammers server; may trigger rate limiting | Exponential backoff with cap |
| Shared mutable state between web and worker | Race conditions | Communicate only via queue.Queue and SQLite |
| Inline scanning in request handler | Blocks web server; request timeout | Always enqueue, return job ID |

## Suggested Build Order

Dependencies flow bottom-up — each layer depends on the one below.

| Order | Component | Depends On | Rationale |
|-------|-----------|------------|-----------|
| 1 | Config (pydantic-settings) | Nothing | Everything reads config; build first |
| 2 | Scanner abstraction + SaneBackend | Config | Core capability; needed to prove pipeline |
| 3 | Page pipeline (empty detection, thumbnail, img2pdf) | Scanner abstraction | Transforms raw pages to PDF |
| 4 | Paperless client (upload, poll, test) | Config | Ingestion target; needed for end-to-end |
| 5 | SQLite job store + worker thread + queue | Config, scanner, pipeline, paperless | Orchestrates full pipeline |
| 6 | FastAPI web layer + CLI | Everything above | Thin layer over existing components |

**Key insight from scanservjs architecture:** scanservjs uses a similar pattern — Express.js web layer with a synchronous scanner process. The critical lesson is that scanner operations MUST be isolated from the web server thread/process. saneless's worker thread approach is validated by this pattern.

## Comparison with Similar Tools

| Aspect | scanservjs | NAPS2 | saneless (planned) |
|--------|-----------|-------|-------------------|
| Scanner access | Direct SANE CLI (`scanimage`) | TWAIN/WIA/SANE | `python-sane` library |
| Web UI | Yes (Express + Vue) | No (desktop) | Yes (FastAPI + vanilla JS) |
| Job queue | In-memory, single job | N/A | queue.Queue + SQLite |
| DMS integration | File copy only | File save only | Native paperless-ngx API |
| Duplex handling | Hardware only | Hardware + software | Hardware + manual two-pass |
| Container support | Yes (Docker) | No | Yes (OCI) |

---
*Architecture research for: saneless*
*Researched: 2026-03-20*
