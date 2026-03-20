# Requirements: saneless

**Defined:** 2026-03-20
**Core Value:** A user can walk up to the web UI, click Scan, and have a correctly assembled PDF land in paperless-ngx with metadata — without touching any other tool.

## v1 Requirements

### Scanner

- [ ] **SCAN-01**: User can discover available SANE devices at startup and on demand
- [ ] **SCAN-02**: User can pin a target device by name in config or select interactively in the web UI
- [ ] **SCAN-03**: User can perform a flatbed single-page scan
- [ ] **SCAN-04**: User can perform an ADF multi-page scan that collects all pages from the feeder
- [ ] **SCAN-05**: User can perform an ADF duplex scan using native hardware duplex
- [ ] **SCAN-06**: User can perform an ADF manual duplex scan (two-pass with flip prompt) that produces correctly interleaved pages
- [ ] **SCAN-07**: System validates raw page count match between pass A and pass B for manual duplex before interleaving
- [ ] **SCAN-08**: System detects and discards empty pages using dual-threshold algorithm (mean luminance + stddev), both thresholds configurable per profile
- [ ] **SCAN-09**: System generates a first-page thumbnail (JPEG, long edge ≤ 300px, base64) after the first page is acquired
- [ ] **SCAN-10**: System fails fast with "No paper detected in feeder" when ADF is empty, not a generic error
- [ ] **SCAN-11**: Only one scan job runs at a time; concurrent requests are queued
- [ ] **SCAN-12**: All scanning runs in a background worker thread, never blocking the web request handler

### Profiles

- [ ] **PROF-01**: User can define scan profiles in TOML config specifying source, resolution, color mode, and optional default metadata
- [ ] **PROF-02**: A `default` profile must always be present
- [ ] **PROF-03**: User can select a profile from a dropdown in the web UI

### PDF Assembly

- [ ] **PDF-01**: Scanned pages are assembled into a single PDF using img2pdf (lossless encoding)
- [ ] **PDF-02**: Temporary files are written to a configurable tmp_dir and cleaned up after success or on error

### Paperless Integration

- [ ] **PLSS-01**: System uploads PDF to paperless-ngx via REST API with metadata (title, created, correspondent ID, tag IDs)
- [ ] **PLSS-02**: System polls paperless-ngx task endpoint until terminal state and surfaces outcome in UI and log
- [ ] **PLSS-03**: System provides `GET /api/paperless/test` endpoint distinguishing three failure modes: server unreachable, token rejected, connection successful
- [ ] **PLSS-04**: User can set title, tags (multi-select from fetched list), and correspondent (select from fetched list) before scanning
- [ ] **PLSS-05**: Tag and correspondent lists are cached with TTL (default 60s) and per-resource manual refresh
- [ ] **PLSS-06**: System supports a consume directory fallback as a config option

### Web UI

- [ ] **UI-01**: Web UI accessible from any browser on the LAN with profile selector, metadata fields, and Scan button
- [ ] **UI-02**: Live status indicator shows job state (idle/scanning/assembling/uploading/done/error) via polling
- [ ] **UI-03**: For manual duplex, UI shows `awaiting_flip` state with flip prompt, illustration, Continue and Cancel buttons
- [ ] **UI-04**: First-page thumbnail displayed in status area once first page is scanned
- [ ] **UI-05**: Job history table showing timestamp, profile, title, and status, persisted to SQLite
- [ ] **UI-06**: Job history pruned by age (default 7 days) and count (default 500 rows)
- [ ] **UI-07**: Scan button disabled while a job is in progress
- [ ] **UI-08**: Refresh icon on tag/correspondent dropdowns for manual cache invalidation

### Health & Monitoring

- [ ] **HLTH-01**: `GET /health` returns 200 when web layer and worker thread are running, 503 otherwise
- [ ] **HLTH-02**: Health endpoint requires no authentication

### CLI

- [ ] **CLI-01**: `saneless scan [--profile PROFILE] [--title TITLE]` triggers a scan job
- [ ] **CLI-02**: `saneless devices` lists available SANE devices
- [ ] **CLI-03**: `saneless jobs` lists recent job history

### Configuration

- [ ] **CONF-01**: Configuration via pydantic-settings reading TOML file with env var overrides
- [ ] **CONF-02**: Scanner host, device, paperless URL/token, tmp_dir, log settings, profiles all configurable
- [ ] **CONF-03**: No hardcoded credentials; API token via config file or environment variable

### Architecture

- [ ] **ARCH-01**: Scanner access isolated behind an abstraction layer (interface methods for device enumeration, capability queries, page acquisition)
- [ ] **ARCH-02**: Background worker thread with queue.Queue for job coordination
- [ ] **ARCH-03**: SANE initialized once at startup (never re-init per scan to avoid fd leaks)

### Error Handling & Logging

- [ ] **LOG-01**: All errors and significant events written to a rotating log file at configurable path
- [ ] **LOG-02**: Log level configurable (DEBUG, INFO, WARNING, ERROR)
- [ ] **LOG-03**: All scan/API/assembly errors displayed in web UI for current or most recent job
- [ ] **LOG-04**: Temporary files cleaned up on error — no orphaned scan data

### Packaging

- [ ] **PKG-01**: pip-installable Python package (pyproject.toml, published to PyPI)
- [ ] **PKG-02**: OCI container image published to GHCR with HEALTHCHECK instruction
- [ ] **PKG-03**: Container does not require --privileged; USB handled by saned server

## v2 Requirements

### Notifications

- **NOTF-01**: System sends desktop/push notification on scan completion or error

### Advanced Scanning

- **ADVS-01**: Support for driverless scanning via sane-airscan (eSCL/WSD)
- **ADVS-02**: scanbd hardware button integration to trigger scans

### Targets

- **TARG-01**: Additional ingestion targets beyond paperless-ngx (cloud storage, email, local folder)

### UI Enhancements

- **UIX-01**: Mobile-optimized responsive layout
- **UIX-02**: Multi-language support / internationalization

## Out of Scope

| Feature | Reason |
|---------|--------|
| Built-in OCR | Delegated to paperless-ngx; duplicating adds heavy dependency for worse results |
| Image editing (crop, rotate, deskew) | saneless is a bridge, not a photo editor; re-scan if wrong |
| Multi-user authentication | Trusted LAN assumption; reverse proxy handles auth if needed |
| Cloud storage / email targets | v1 is paperless-ngx only; architecture supports future backends |
| saned management / bundling | External dependency; user configures separately |
| Film/slide scanning | Different use case entirely |
| Custom pipeline system | Fixed pipeline is predictable and testable |
| Page reordering / drag-and-drop | Manual duplex handles ordering algorithmically |
| Real-time scan preview (live line-by-line) | Impractical over network SANE; first-page thumbnail sufficient |
| Multiple simultaneous scanners | Single scanner at a time; device selection in config or UI |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| SCAN-01 | Phase 1 | Pending |
| SCAN-02 | Phase 1 | Pending |
| SCAN-03 | Phase 1 | Pending |
| PROF-01 | Phase 1 | Pending |
| PROF-02 | Phase 1 | Pending |
| PDF-01 | Phase 1 | Pending |
| PDF-02 | Phase 1 | Pending |
| PLSS-01 | Phase 1 | Pending |
| PLSS-02 | Phase 1 | Pending |
| PLSS-03 | Phase 1 | Pending |
| CONF-01 | Phase 1 | Pending |
| CONF-02 | Phase 1 | Pending |
| CONF-03 | Phase 1 | Pending |
| ARCH-01 | Phase 1 | Pending |
| ARCH-02 | Phase 1 | Pending |
| ARCH-03 | Phase 1 | Pending |
| LOG-01 | Phase 1 | Pending |
| LOG-02 | Phase 1 | Pending |
| LOG-04 | Phase 1 | Pending |
| CLI-01 | Phase 1 | Pending |
| CLI-02 | Phase 1 | Pending |
| SCAN-04 | Phase 2 | Pending |
| SCAN-05 | Phase 2 | Pending |
| SCAN-06 | Phase 2 | Pending |
| SCAN-07 | Phase 2 | Pending |
| SCAN-08 | Phase 2 | Pending |
| SCAN-09 | Phase 2 | Pending |
| SCAN-10 | Phase 2 | Pending |
| SCAN-11 | Phase 2 | Pending |
| SCAN-12 | Phase 2 | Pending |
| UI-01 | Phase 3 | Pending |
| UI-02 | Phase 3 | Pending |
| UI-03 | Phase 3 | Pending |
| UI-04 | Phase 3 | Pending |
| UI-05 | Phase 3 | Pending |
| UI-06 | Phase 3 | Pending |
| UI-07 | Phase 3 | Pending |
| UI-08 | Phase 3 | Pending |
| PROF-03 | Phase 3 | Pending |
| PLSS-04 | Phase 3 | Pending |
| PLSS-05 | Phase 3 | Pending |
| HLTH-01 | Phase 3 | Pending |
| HLTH-02 | Phase 3 | Pending |
| LOG-03 | Phase 3 | Pending |
| PKG-01 | Phase 4 | Pending |
| PKG-02 | Phase 4 | Pending |
| PKG-03 | Phase 4 | Pending |
| PLSS-06 | Phase 4 | Pending |
| CLI-03 | Phase 4 | Pending |

**Coverage:**
- v1 requirements: 47 total
- Mapped to phases: 47
- Unmapped: 0 ✓

---
*Requirements defined: 2026-03-20*
*Last updated: 2026-03-20 after initial definition*
