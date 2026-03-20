# saneless — Product Requirements Document
 
**Version:** 0.1 (Draft)  
**Status:** In Review  
**Last Updated:** 2026-03-20
 
---
 
## 1. Overview
 
saneless is an open-source tool that bridges SANE-compatible network scanners (exposed via `saned`) and the [paperless-ngx](https://docs.paperless-ngx.com/) document management system. It provides a web UI and CLI for triggering scans, assembling multi-page PDFs, and ingesting documents into paperless-ngx with rich metadata — all from any device on the local network.
 
---
 
## 2. Problem Statement
 
paperless-ngx is an excellent self-hosted document archive, but it has no native scanner integration. Users must either manually scan documents using a separate application, save files to disk, and move them into the consume directory — or build their own ad-hoc scripts. This friction discourages regular use and defeats the goal of a paperless home or office.
 
SANE (`saned`) is the de facto standard for sharing scanners over a network on Linux, but it has no web-accessible frontend suited to the "scan and forget" workflow that paperless-ngx encourages.
 
saneless fills this gap.
 
---
 
## 3. Goals
 
- Provide a self-hosted web UI accessible from any browser on the LAN to trigger scans and monitor progress.
- Support all common SANE-compatible scanner types: flatbed, ADF (single-sided), and ADF duplex.
- Ingest scanned documents into paperless-ngx via its REST API, with metadata (title, tags, correspondent) settable per scan.
- Be deployable as either a pip-installable Python package or an OCI container image.
- Work whether saneless runs on the same machine as `saned` or a separate machine on the same LAN.
 
## 4. Non-Goals
 
- saneless does not replace or configure `saned` itself — users are expected to have a working `saned` setup.
- saneless does not perform its own OCR — this is delegated to paperless-ngx.
- saneless does not support cloud storage targets, email delivery, or non-paperless-ngx destinations (in v1).
- saneless does not support driverless scanning protocols (eSCL, WSD, IPP) or backends such as `sane-airscan` in v1. The architecture must not preclude adding these in a future version.
- saneless does not manage paperless-ngx users, permissions, or tags — it only consumes the existing API.
 
---
 
## 5. Users & Deployment Context
 
saneless targets self-hosters deploying paperless-ngx on a home or small-office LAN. The intended audience ranges from technical homelab users to less technical household members who will interact only with the web UI. The project will be released as open source.
 
**Typical deployment topology:**
 
```
[ USB Scanner ]
      |
[ saned server ]  <----network---->  [ saneless ]  <----HTTP---->  [ paperless-ngx ]
      |                                    |
      +---- same machine possible ----------+
```
 
saneless must work when co-located with `saned` and when running on a separate host. Configuration covers both cases.
 
---
 
## 6. User Stories
 
**As a user with a flatbed scanner,**
I want to open the saneless web UI, choose a scan profile, and click Scan,
so that a single scanned page appears in paperless-ngx automatically.
 
**As a user with an ADF scanner,**
I want to load a stack of pages, trigger a scan from the web UI,
so that all pages are assembled into a single PDF and ingested as one document.
 
**As a user with a single-sided ADF scanner and a double-sided document,**
I want to scan the front faces of all pages, flip the stack when prompted, then scan the back faces,
so that saneless interleaves the two passes into a correctly ordered single PDF without me needing a duplex-capable scanner.
 
 
I want to scan both sides of every page in a single pass,
so that double-sided documents are correctly assembled without manual intervention.
 
**As a power user,**
I want to define named scan profiles (e.g. "Receipts 150dpi Grayscale", "Contracts 300dpi Color"),
so that I don't have to reconfigure settings for routine document types.
 
**As a user about to scan,**
I want to set the title, tags, and correspondent in the web UI before scanning,
so that my document lands in paperless-ngx already categorized.
 
**As any user,**
I want to see a live status indicator during scanning and ingestion,
so that I know whether my scan succeeded without checking paperless-ngx manually.
 
**As an administrator,**
I want all errors written to a persistent log file,
so that I can diagnose failures after the fact.
 
---
 
## 7. Functional Requirements
 
### 7.1 Scanner Discovery & Configuration
 
- saneless must enumerate available SANE devices at startup and on demand via `sane.get_devices()`.
- The target device can be pinned by name in config or selected interactively in the web UI.
- If saneless runs on a separate host from `saned`, the operator sets the `saned` host IP in the config file (which populates the SANE `net` backend). This is not configurable at runtime via the web UI.
- If saneless runs on the same machine as `saned`, it must also work, pointing at `localhost`.
- `saned` is always treated as an external dependency. saneless does not bundle or manage `saned`.
 
### 7.2 Scan Profiles
 
- Profiles are defined in the config file and exposed in the web UI as a dropdown.
- Each profile specifies: scanner source (`Flatbed`, `ADF`, `ADF Duplex`, `ADF Manual Duplex`), resolution (DPI), color mode (`color`, `gray`, `lineart`), and optional default paperless-ngx metadata (title template, tag IDs, correspondent ID).
- A `default` profile must be present; others are optional.
 
### 7.3 Scanning
 
- saneless must acquire scans using `python-sane`.
- For `ADF Manual Duplex` jobs, the worker must coordinate a two-pass scan: it acquires all front-facing pages (pass A), then pauses the job and signals the UI to prompt the user to flip the stack and confirm readiness. Once the user confirms, the worker acquires all back-facing pages (pass B). The pages from pass B are stored in reverse order and interleaved with pass A to produce the correct reading sequence (A1, B1, A2, B2, …). This reverse-and-interleave approach assumes the user returns the stack with page N on top, which is the natural result of flipping a face-down stack — the UI prompt must make this flip orientation explicit to avoid a silently misordered document. Pass A and pass B raw page counts — measured before empty page detection is applied — must match; if they do not, the job must fail with a clear error indicating the mismatch rather than producing a malformed document where every page is paired with the wrong back. Empty page detection is applied to both passes after the raw count check passes, so a legitimately blank reverse side is discarded without triggering a false mismatch. The worker must block between passes using a `threading.Event` that the web layer sets when the user clicks Continue — no additional infrastructure beyond the existing worker thread model is required.
- Empty page detection must be applied to ADF scans to discard blank reverse sides from non-duplex duplex jobs. Detection must use a combined mean luminance and standard deviation check: a page is considered blank only if its mean luminance is above a high threshold (very white on average) **and** its luminance standard deviation is below a low threshold (very low variance). This guards against two known failure modes of mean-only detection — translucent paper where text bleeds through from the front (high mean but elevated variance), and scanner dust or specks (high mean but non-zero variance hotspots). Both thresholds must be independently configurable. The detection algorithm must convert the image to grayscale before computing statistics — operating on a color image would require aggregating across channels and could produce misleading results, particularly for pages with faint color tints from bleed-through.
 
  Formally, a page $P$ is discarded if and only if:
 
  $$L_{mean}(P) > T_{mean} \quad \text{AND} \quad \sigma_L(P) < T_{std}$$
 
  Where $L_{mean}(P)$ is the mean luminance of the grayscale image, $\sigma_L(P)$ is its standard deviation, $T_{mean}$ is `empty_page_mean_threshold`, and $T_{std}$ is `empty_page_stddev_threshold`. The condition is a logical `AND` — a page with high mean luminance but high variance (bleed-through or dust) must not be discarded.
- After the first page is acquired, the worker must generate a low-resolution JPEG thumbnail (long edge ≤ 300px) and store it as a base64-encoded string on the job state record. This happens before remaining ADF pages are scanned, so the UI can display it immediately as a first-page preview while the rest of the document is still processing. For flatbed scans the thumbnail is generated as soon as the single page is acquired.
- All scanning work must run in a background worker thread, not in the web request handler, to prevent blocking.
- If the ADF feeder is empty, saneless must fail fast with a clear, user-facing error message ("No paper detected in feeder") rather than a generic system error. Note that many SANE backends do not report an out-of-paper condition until the first scan attempt itself raises a `sane.error` — pre-flight detection via a status query is not reliably available. The background worker must therefore explicitly catch `sane.error` on the first `multi_scan()` iteration, inspect the error string for out-of-paper indicators, and translate it into the user-friendly message. Any other `sane.error` must still be caught and reported distinctly, not swallowed or surfaced as a generic crash.
- Only one scan job may run at a time (the scanner is a shared hardware resource). Concurrent scan requests must be queued.
 
### 7.4 PDF Assembly
 
- Scanned pages (PIL Image objects) must be assembled into a single PDF using `img2pdf` (lossless encoding).
- Temporary files must be written to a configurable `tmp_dir` and cleaned up after successful ingestion or on error.
 
### 7.5 Ingestion into paperless-ngx
 
- A `GET /api/paperless/test` endpoint must be provided that performs a lightweight authenticated request against the paperless-ngx API (e.g. `GET /api/`) and returns whether the server is reachable and the token is valid. It must distinguish between three failure modes: server unreachable (network/DNS error), server reachable but token rejected (HTTP 401/403), and server reachable and token valid. This endpoint is used by the web UI Test Connection button and may also be called by the CLI and at startup to surface misconfiguration early.
- The following metadata fields must be settable per scan job: `title`, `created` (defaults to scan datetime), `correspondent` (by ID), `tags` (list of IDs, submitted as repeated form fields — not comma-separated).
- After upload, saneless must poll `GET /api/tasks/?task_id={uuid}` until the task reaches a terminal state (success or failure), and surface the outcome in the UI and log.
- A consume directory fallback must be available as a config option for environments where the REST API is unavailable or undesirable.
 
### 7.6 Web UI
 
The web UI must include the following at launch:
 
- **Health endpoint:** `GET /health` must return HTTP 200 with a minimal JSON payload (e.g. `{"status": "ok"}`) when the web layer is alive and the worker thread is running. If the worker thread has died or deadlocked, the endpoint must return HTTP 503. This endpoint requires no authentication and is intended for use by container orchestration health checks.
- **Test Connection button:** Located near the paperless-ngx URL and token configuration, this button calls `GET /api/paperless/test` and displays the result inline — a distinct message for each of the three outcomes: server unreachable, token rejected, or connection successful. This allows users to verify their configuration before committing to a scan job.
- **Profile selector:** Dropdown of configured scan profiles.
- **Metadata fields:** Title (text), Tags (multi-select from paperless-ngx tag list fetched via API), Correspondent (select from paperless-ngx correspondent list fetched via API). Tag and correspondent lists are cached in memory with a short TTL (default 60 seconds) to reduce latency without serving stale data. A refresh icon must be displayed alongside each dropdown to allow the user to manually bust the cache and re-fetch immediately — useful when a tag or correspondent has just been created in paperless-ngx. The refresh icon must trigger a per-resource cache invalidation (e.g. `POST /api/cache/invalidate?resource=tags`) rather than a full cache flush, so refreshing tags does not also evict the cached correspondent list and vice versa. The dropdown must re-populate inline without a full page reload.
- **Scan button:** Triggers a scan job; disabled while a job is in progress.
- **Live status indicator:** Shows current job state — idle / scanning / assembling / uploading / done / error — updated by polling a `/api/jobs/{id}` endpoint. For `ADF Manual Duplex` jobs an additional `awaiting_flip` state must be surfaced, displaying a clear prompt with a Continue button to trigger pass B and a Cancel button to abort the job cleanly. The prompt must include both a specific written instruction — "Keep the pages in the same order, then flip the stack over the long edge (the left or right side, not the top or bottom)" — and a simple illustrative graphic showing the correct flip axis versus the incorrect short-edge flip. This is necessary because a short-edge flip produces back pages that are upside down in the final PDF, a mistake that is not caught until after ingestion into paperless-ngx.
- **First-page thumbnail:** Once the first page has been scanned, the status area must display a thumbnail preview of it. This gives immediate visual confirmation that the scan orientation, feed, and quality are correct before the full job completes. The thumbnail is served as a base64-encoded JPEG via the job status endpoint and rendered inline — no separate image endpoint required.
- **Job history:** Table of recent scan jobs with timestamp, profile used, document title, and final status (success / error). Persisted to a local SQLite database. History is pruned by both age and count — entries older than a configurable retention period (default: 7 days) are deleted, and if the total row count still exceeds a configurable maximum (default: 500), the oldest rows beyond that limit are trimmed as well. Pruning runs at startup and periodically at runtime. The effective retention is whichever constraint is more restrictive.
 
### 7.7 CLI
 
A `saneless` CLI must provide at minimum:
 
- `saneless scan [--profile PROFILE] [--title TITLE]` — trigger a scan job
- `saneless devices` — list available SANE devices
- `saneless jobs` — list recent job history
 
### 7.8 Error Handling & Logging
 
- All errors (scan failures, API errors, assembly failures) must be displayed in the web UI for the current or most recent job.
- All errors and significant events must be written to a rotating log file at a configurable path.
- Log level must be configurable (`DEBUG`, `INFO`, `WARNING`, `ERROR`).
- Temporary files must be cleaned up on error — no orphaned scan data.
 
---
 
## 8. Non-Functional Requirements
 
- **Single concurrent scan:** Job queue depth is 1 active + N waiting. Waiting jobs must be visible in the UI.
- **Response time:** The web UI Scan button must return a job ID and begin showing status within 500ms of being clicked (the actual scan takes as long as the hardware requires).
- **Portability:** Must run on any Linux host with Python 3.14+ and `libsane` installed.
- **Container image:** An OCI image must be published alongside pip releases. The image must not require `--privileged`; USB device access is handled by the `saned` server, not the container. The `Dockerfile` must include a `HEALTHCHECK` instruction that pings `GET /health` using `curl` or `wget`, with a sensible interval (e.g. `--interval=30s --timeout=5s --retries=3`), so container runtimes and orchestrators can detect a deadlocked web layer and restart the container automatically.
- **No hardcoded credentials:** API token and all secrets must be supplied via config file or environment variable, never baked into the image.
- **Dependency footprint:** Minimize non-Python system dependencies. Only `libsane` (for `python-sane`) is a required system package.
 
---
 
## 9. Architecture
 
### 9.1 Process Model
 
saneless runs as a single process with two internal components:
 
- **Web layer** (FastAPI): Handles HTTP requests, serves the UI, enqueues scan jobs, and exposes the job status API. Always responsive — never blocks on scanner I/O.
- **Worker thread**: Owns the scanner. Dequeues jobs, runs the full pipeline (scan → assemble → ingest), and updates job state in SQLite.
 
A background thread with a `queue.Queue` is sufficient for v1. A heavier task queue (e.g. `rq` + Redis) is explicitly deferred to a future version.
 
Scanner access must be isolated behind an internal abstraction layer rather than calling `python-sane` directly throughout the codebase. In v1 this abstraction has a single implementation backed by `python-sane` / `saned`, but the interface must be designed to accommodate alternative backends — such as `sane-airscan` for driverless eSCL/WSD scanning — without requiring changes to the job pipeline, web layer, or configuration schema. Concretely: device enumeration, capability queries, and page acquisition must each be defined as interface methods that the rest of the codebase calls, not as direct library calls.
 
### 9.2 Component Diagram
 
```
Browser / CLI
     |
  [ FastAPI Web Layer ]
     |         |
     |    [ Job Queue (queue.Queue) ]
     |         |
     |    [ Worker Thread ]
     |         |-- python-sane --> saned --> Scanner
     |         |-- img2pdf
     |         |-- httpx --> paperless-ngx REST API
     |         |-- SQLite (job state)
     |
  [ SQLite (job history read) ]
```
 
### 9.3 Key Libraries
 
| Purpose | Library |
|---|---|
| SANE access | `python-sane` (python-pillow/Sane) |
| PDF assembly | `img2pdf` |
| Image handling | `Pillow` |
| HTTP client | `httpx` |
| Web framework | `FastAPI` + `uvicorn` |
| Config | `pydantic-settings` (TOML + env var support) |
| CLI | `click` |
| Job persistence | `sqlite3` (stdlib) |
 
### 9.4 Configuration
 
Configuration is managed by `pydantic-settings`, which reads from a TOML file and allows any value to be overridden by an environment variable (useful for secrets in container deployments). The TOML file structure:
 
```toml
[scanner]
# saned host — omit or set to "" if saneless runs on the same machine
host = "192.168.1.10"
# Device string from `scanimage -L` — or leave blank to use the first discovered device
device = ""
 
[paperless]
url = "http://paperless.local:8000"
# API token from My Profile > circular arrow button in paperless-ngx UI
token = ""
# Fallback: drop files here if REST API fails (leave blank to disable)
consume_dir = ""
 
[output]
tmp_dir = "/tmp/saneless"
log_file = "/var/log/saneless/saneless.log"
log_level = "INFO"
history_retention_days = 7
history_max_rows = 500
paperless_cache_ttl_seconds = 60
 
[profiles.default]
source = "ADF"
resolution = 300
mode = "color"
empty_page_mean_threshold = 0.98     # page must be at least this white on average
empty_page_stddev_threshold = 0.02   # page must have at most this much luminance variance
 
[profiles.receipts]
source = "ADF"
resolution = 150
mode = "gray"
default_tags = [3]
default_correspondent = 5
 
[profiles.contracts]
source = "Flatbed"
resolution = 300
mode = "color"
default_tags = [7, 12]
```
 
---
 
## 10. Delivery Phases
 
### Phase 1 — Core Pipeline (MVP)
- `saneless scan` CLI
- Flatbed single-page scan → PDF → paperless-ngx REST API upload
- pydantic-settings config (TOML + env vars), API token auth, task polling, log file
 
### Phase 2 — ADF & Multi-page
- ADF multi-page scan via `multi_scan()` iterator
- Empty-page detection
- PDF assembly with `img2pdf`
- Basic duplex support (via paperless-ngx consume-dir collation or native ADF Duplex mode)
 
### Phase 3 — Web UI
- FastAPI web layer + uvicorn + worker thread + `queue.Queue`
- Device picker, profile selector, metadata fields, Scan button
- Live job status polling
- Job history table (SQLite)
 
### Phase 4 — Packaging & Polish
- pip-installable package (`pyproject.toml`, published to PyPI)
- OCI container image (published to GHCR)
- `saneless devices` and `saneless jobs` CLI commands
- Consume directory fallback
- Full README, install guide, and Docker Compose example
 
---
 
---
 
## 12. Future Work
 
These items are explicitly out of scope for v1 but should be kept in mind when making architectural decisions so they are not inadvertently foreclosed.
 
| Item | Notes |
|---|---|
| Driverless scanning via `sane-airscan` | Adds support for eSCL and WSD network scanners without a dedicated `saned` server. The scanner abstraction layer introduced in v1 (§9.1) is the intended extension point. |
| Additional ingestion targets | Other document management systems, cloud storage, or local folder destinations beyond paperless-ngx. The ingestion layer should be similarly abstracted. |
| Heavier task queue | Replace `queue.Queue` + worker thread with `rq` + Redis for persistence across restarts and multi-worker scaling. |
| Scan button on hardware | Integration with `scanbd` to trigger a saneless scan job when a physical button on the scanner is pressed. |
| Mobile-friendly UI | The web UI should be built responsively from the start so this is a layout concern rather than a rewrite. |
 
| # | Question | Decision |
|---|---|---|
| 1 | Should the web UI cache paperless-ngx tag and correspondent lists? | Yes — short in-memory TTL cache (default 60s). Reduces latency without serving stale data. |
| 2 | Should job history be capped by count or age? | By age. Configurable retention period, default 7 days. Pruned at startup and periodically at runtime. |
| 3 | Should the `saned` host be configurable via the web UI? | No — config file only. Keeps the UI scope focused and treats host topology as an operator concern. |
| 4 | What is the correct behavior when the ADF feeder is empty at job start? | Fail fast with a clear error message. Do not wait for a SANE backend timeout. |
| 5 | Should the OCI image bundle `saned`? | No — `saned` is always external. The container image contains only saneless. This avoids USB passthrough complexity and keeps the image minimal. |

