# Project Research Summary

**Project:** saneless
**Domain:** SANE scanner-to-paperless-ngx bridge (self-hosted document scanning)
**Researched:** 2026-03-20
**Confidence:** HIGH

## Executive Summary

saneless is a single-purpose bridge that connects SANE network scanners to paperless-ngx via a web UI, CLI, and REST API. The closest competitor is scanservjs, which validates the core architecture pattern: a web server fronting a synchronous scanner process, with job queueing to serialize scanner access. saneless differentiates by offering native paperless-ngx integration with metadata-at-scan-time, automated manual duplex interleaving, and empty page detection -- features no existing web-based scanner tool provides together.

The recommended approach is a single-process Python application using FastAPI for the web layer and a dedicated worker thread for scanner operations. The stack is mature and well-understood: FastAPI, Pillow, img2pdf, httpx, pydantic-settings, SQLite, and python-sane. The architecture is deliberately simple -- no task queues, no ORMs, no external dependencies beyond libsane and paperless-ngx. This simplicity is a feature, not a limitation, because a scanner is inherently a single-concurrent-job device.

The primary risk is python-sane. It is a low-bus-factor C extension with known bugs including file descriptor leaks, segfaults from callback misuse, and unreliable ADF end-of-feed detection. The scanner abstraction layer is not optional -- it is the project's most critical architectural decision, isolating the rest of the codebase from python-sane's fragility. Secondary risks are SANE device-specific option naming inconsistencies and saned network firewall issues. Both are mitigated through runtime capability discovery and clear deployment documentation.

## Key Findings

### Recommended Stack

The stack is almost entirely HIGH confidence. All core libraries have recent releases, active maintenance, and confirmed Python 3.14 compatibility (except python-sane, which is unverified). The key design choice is SSE over WebSockets for real-time updates -- the UI only needs server-to-client status pushes.

**Core technologies:**
- **FastAPI + Jinja2:** Web framework with server-rendered UI and API endpoints
- **python-sane:** Only maintained Python SANE binding; C extension, low bus factor, needs careful isolation
- **Pillow:** Image manipulation for thumbnails and empty page detection
- **img2pdf:** Lossless PDF assembly (no re-encoding)
- **httpx:** Async-capable HTTP client for paperless-ngx API
- **pydantic-settings:** TOML config + env var overrides with validation
- **click:** CLI for `saneless scan`, `saneless devices`, `saneless jobs`
- **sqlite3 (stdlib):** Job history with age + count pruning
- **SSE:** Server-Sent Events for live scan status (no WebSocket dependency)

### Expected Features

**Must have (table stakes):**
- Flatbed and ADF scanning with resolution/mode selection
- PDF output via img2pdf
- Paperless-ngx REST API upload with metadata (title, tags, correspondent)
- Web UI accessible from phone browsers
- Scan profiles/presets (TOML-defined)
- Live scan status feedback
- Scanner discovery
- Error reporting in UI

**Should have (differentiators):**
- ADF manual duplex with automated two-pass interleaving and flip prompt
- Empty page detection (dual-threshold: mean luminance AND stddev)
- First-page thumbnail preview
- Paperless-ngx 3-mode connection test (unreachable/bad token/OK)
- Per-resource cache with manual refresh for tag/correspondent dropdowns
- Job history with pruning
- CLI for automation
- Health endpoint for container orchestration
- Consume directory fallback

**Defer indefinitely:**
- OCR (paperless-ngx handles this)
- Image editing (crop, rotate, deskew)
- Multi-user auth (reverse proxy handles this)
- Cloud storage / non-paperless targets
- Page reordering / drag-and-drop
- Internationalization
- Hardware button integration

### Architecture Approach

Single-process, two-thread model: FastAPI handles HTTP on the main thread, a dedicated worker thread serializes scanner jobs via `queue.Queue`. All scanner state lives in SQLite. Communication between web and worker happens only through the queue and the database -- no shared mutable state. Scanner devices are opened per-job and closed immediately after, never held between jobs.

**Major components:**
1. **FastAPI Web Layer** -- API endpoints, static UI, SSE streaming, paperless metadata cache
2. **Worker Thread** -- Dequeues jobs, drives scanner via abstraction layer, runs page pipeline, uploads to paperless-ngx
3. **Scanner Abstraction (ABC)** -- Isolates python-sane behind a clean interface; validates options at runtime
4. **Page Pipeline** -- Empty detection, thumbnail generation, img2pdf assembly
5. **Paperless Client** -- Upload, task polling with backoff, connection test, metadata fetching with TTL cache
6. **Config (pydantic-settings)** -- TOML + env vars, scan profiles, validation at startup
7. **SQLite Job Store** -- Job state machine (queued/scanning/awaiting_flip/assembling/uploading/done/error)

### Critical Pitfalls

1. **python-sane fd leak on repeated init/exit** -- Call `sane.init()` exactly once at startup, never call `sane.exit()` except at shutdown. Monitor fd count in health endpoint.
2. **python-sane segfaults from callback misuse** -- Avoid progress callbacks entirely in v1. If needed, wrap in defensive adapter.
3. **ADF multi_scan() unreliable end-of-feed** -- Per-page timeout, validate each image immediately, always call `sane.cancel()` after iteration.
4. **Scanner device locking / exclusive access** -- Context managers for all device access, explicit `sane.cancel()` before `close()` on every path including errors.
5. **Scanner options are device-specific** -- Never hardcode option names. Enumerate at runtime, validate against reported constraints, map through abstraction layer.

## Implications for Roadmap

Based on research, suggested phase structure:

### Phase 1: Core Pipeline (Config + Scanner + PDF + Paperless Upload)

**Rationale:** Dependencies flow bottom-up. Config is needed by everything. Scanner abstraction is the riskiest component and must be validated first. End-to-end flatbed scan to paperless-ngx upload proves the entire pipeline works.
**Delivers:** Working CLI-driven single-page scan that produces a PDF and uploads it to paperless-ngx with metadata. Config system. Scanner abstraction layer. Paperless client with connection test and task polling.
**Features:** Scanner discovery, flatbed scan, PDF output, paperless-ngx upload with title, scan profiles (basic), error handling, temp file cleanup, CLI (`saneless scan`, `saneless devices`).
**Avoids:** fd leak (init-once pattern), segfaults (no progress callbacks), device locking (context managers), option name issues (abstraction layer validates at runtime).

### Phase 2: ADF and Multi-Page

**Rationale:** ADF scanning depends on Phase 1's scanner abstraction and PDF pipeline. Manual duplex is the highest-complexity feature and the primary differentiator. Empty page detection must happen here because it integrates into the page pipeline before PDF assembly.
**Delivers:** Multi-page ADF scanning, native hardware duplex, manual two-pass duplex with interleaving, empty page detection.
**Features:** ADF multi-page scan, empty page detection (dual threshold), native duplex, manual duplex with flip prompt, per-profile threshold config.
**Avoids:** ADF end-of-feed hang (per-page timeouts), interleave errors (count validation, blank detection AFTER interleave), img2pdf EXIF issues (strip EXIF, write to disk first).

### Phase 3: Web UI

**Rationale:** The web layer is a thin shell over components built in Phases 1-2. Building it last means the underlying API is stable and well-tested before adding UI concerns. The worker thread + queue architecture replaces CLI-driven execution.
**Delivers:** Full web interface with profile selector, metadata fields, scan button, live status via SSE, thumbnail preview, job history, tag/correspondent dropdowns, connection test page, health endpoint.
**Features:** Web UI, live status, first-page thumbnail, metadata dropdowns with cache/refresh, job history table, paperless-ngx connection test, health endpoint, manual duplex flip prompt with illustration.
**Avoids:** Blocking event loop (worker thread isolates scanning), shared mutable state (queue + SQLite only).

### Phase 4: Packaging and Deployment

**Rationale:** Containerization and packaging come last because they wrap the complete application. The consume directory fallback is a deployment-time concern.
**Delivers:** OCI container image with HEALTHCHECK, pip-installable package, `saneless jobs` CLI, consume directory fallback, deployment documentation.
**Features:** Container image, consume directory fallback, `saneless jobs` command, deployment guide.
**Avoids:** Missing libsane in container (build-time import test), net backend not enabled (verify dll.conf).

### Phase Ordering Rationale

- **Bottom-up dependency chain:** Config feeds everything, scanner abstraction feeds all scan types, PDF pipeline feeds upload, upload feeds the entire purpose of the tool.
- **Risk-first:** python-sane integration is the highest-risk component. Phase 1 surfaces all SANE issues before building on top.
- **Differentiator timing:** Manual duplex (Phase 2) is the hardest feature and the strongest differentiator. Building it early validates the architecture and locks in the competitive advantage.
- **UI last:** The web layer is a presentation concern. Building it over a stable, tested API avoids rework.

### Research Flags

Phases likely needing deeper research during planning:
- **Phase 2 (ADF/Duplex):** Manual duplex interleaving logic is error-prone and under-documented in the wild. The interaction between blank page detection and page count validation needs careful design. Test with multiple scanner models if possible.
- **Phase 1 (Scanner Abstraction):** python-sane's option discovery API and device-specific behavior may require experimentation. Consider building a SANE option dump tool first.

Phases with standard patterns (skip research-phase):
- **Phase 3 (Web UI):** FastAPI + Jinja2 + SSE is well-documented. The patterns here are standard web development.
- **Phase 4 (Packaging):** OCI container builds and pip packaging are fully documented. The only nuance is libsane installation.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | All packages verified on PyPI with recent releases. Only python-sane (MEDIUM) due to low maintenance and unverified Python 3.14 compat. |
| Features | MEDIUM | Competitor analysis based on project pages and training data. Could not access paperless-ngx API docs directly (403). Feature priorities are sound but competitive landscape may shift. |
| Architecture | HIGH | Pattern validated by scanservjs. Single-process worker thread model is well-proven for single-device scanner tools. |
| Pitfalls | HIGH | Most pitfalls confirmed via open GitHub issues with reproduction cases. python-sane bugs are documented and specific. |

**Overall confidence:** HIGH

### Gaps to Address

- **python-sane Python 3.14 compatibility:** Unverified. Test compilation from sdist early in Phase 1. Have a fallback plan (pin Python 3.13 in container, or fork python-sane).
- **paperless-ngx API docs inaccessible (403):** Could not verify API endpoints directly. Rely on GitHub source and community documentation. Validate upload endpoint, task polling, and metadata endpoints in Phase 1.
- **Device-specific SANE behavior:** Option names and ADF behavior vary by scanner model. Phase 1 should include testing against at least one real scanner via saned. Consider adding a mock scanner backend for testing.
- **Pillow MAX_IMAGE_PIXELS for high-DPI scans:** Need to determine correct limit. 1200 DPI A4 is ~139M pixels, exceeding the default 89.5M limit. Must be set before any image processing.
- **Manual duplex UX:** The flip prompt with illustration is critical for usability but the exact UX has no precedent in web scanner tools. May need user testing.

## Sources

### Primary (HIGH confidence)
- python-sane GitHub: issues #70, #73, #81, #93, #103
- img2pdf PyPI: https://pypi.org/project/img2pdf/
- FastAPI, Pillow, httpx, pydantic-settings, click: all verified via PyPI (March 2026)
- saned(8) manpage, Arch Wiki SANE
- scanservjs GitHub: https://github.com/sbs20/scanservjs

### Secondary (MEDIUM confidence)
- paperless-ngx API: https://github.com/paperless-ngx/paperless-ngx/blob/main/docs/api.md (verified March 2026 via GitHub)
- Competitor feature analysis: NAPS2, VueScan, HP Smart, Brother iPrint&Scan (from official sites)

### Tertiary (LOW confidence)
- python-sane Python 3.14 compatibility: untested, no upstream CI for 3.14
- paperless-ngx task polling behavior: community discussions, not verified against source

---
*Research completed: 2026-03-20*
*Ready for roadmap: yes*
