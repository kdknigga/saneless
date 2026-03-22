# Requirements: saneless

**Defined:** 2026-03-20
**Core Value:** A user can walk up to the web UI, click Scan, and have a correctly assembled PDF land in paperless-ngx with metadata -- without touching any other tool.

## v1 Requirements

### Scanner

- [x] **SCAN-01**: User can discover available SANE devices at startup and on demand
- [x] **SCAN-02**: User can pin a target device by name in config or select interactively in the web UI
- [x] **SCAN-03**: User can perform a flatbed single-page scan
- [x] **SCAN-04**: User can perform an ADF multi-page scan that collects all pages from the feeder
- [x] **SCAN-05**: User can perform an ADF duplex scan using native hardware duplex
- [x] **SCAN-06**: User can perform an ADF manual duplex scan (two-pass with flip prompt) that produces correctly interleaved pages
- [x] **SCAN-07**: System validates raw page count match between pass A and pass B for manual duplex before interleaving
- [x] **SCAN-08**: System detects and discards empty pages using dual-threshold algorithm (mean luminance + stddev), both thresholds configurable per profile
- [x] **SCAN-09**: System generates a first-page thumbnail (JPEG, long edge <= 300px, base64) after the first page is acquired
- [x] **SCAN-10**: System fails fast with "No paper detected in feeder" when ADF is empty, not a generic error
- [x] **SCAN-11**: Only one scan job runs at a time; concurrent requests are queued
- [x] **SCAN-12**: All scanning runs in a background worker thread, never blocking the web request handler

### Profiles

- [x] **PROF-01**: User can define scan profiles in TOML config specifying source, resolution, color mode, and optional default metadata
- [x] **PROF-02**: A `default` profile must always be present
- [x] **PROF-03**: User can select a profile from a dropdown in the web UI
- [x] **DPI-01**: Default scan resolution is 300 DPI, consolidated into a single DEFAULT_RESOLUTION constant as the authoritative source of truth

### PDF Assembly

- [x] **PDF-01**: Scanned pages are assembled into a single PDF using img2pdf (lossless encoding)
- [x] **PDF-02**: Temporary files are written to a configurable tmp_dir and cleaned up after success or on error

### Paperless Integration

- [x] **PLSS-01**: System uploads PDF to paperless-ngx via REST API with metadata (title, created, correspondent ID, tag IDs)
- [x] **PLSS-02**: System polls paperless-ngx task endpoint until terminal state and surfaces outcome in UI and log
- [x] **PLSS-03**: System provides `GET /api/paperless/test` endpoint distinguishing three failure modes: server unreachable, token rejected, connection successful
- [x] **PLSS-04**: User can set title, tags (multi-select from fetched list), and correspondent (select from fetched list) before scanning
- [x] **PLSS-05**: Tag and correspondent lists are cached with TTL (default 60s) and per-resource manual refresh
- [x] **PLSS-06**: System supports a consume directory fallback as a config option

### Web UI

- [x] **UI-01**: Web UI accessible from any browser on the LAN with profile selector, metadata fields, and Scan button
- [x] **UI-02**: Live status indicator shows job state (idle/scanning/assembling/uploading/done/error) via polling
- [x] **UI-03**: For manual duplex, UI shows `awaiting_flip` state with flip prompt, illustration, Continue and Cancel buttons
- [x] **UI-04**: First-page thumbnail displayed in status area once first page is scanned
- [x] **UI-05**: Job history table showing timestamp, profile, title, and status, persisted to SQLite
- [x] **UI-06**: Job history pruned by age (default 7 days) and count (default 500 rows)
- [x] **UI-07**: Scan button disabled while a job is in progress
- [x] **UI-08**: Refresh icon on tag/correspondent dropdowns for manual cache invalidation

### Health & Monitoring

- [x] **HLTH-01**: `GET /health` returns 200 when web layer and worker thread are running, 503 otherwise
- [x] **HLTH-02**: Health endpoint requires no authentication

### CLI

- [x] **CLI-01**: `saneless scan [--profile PROFILE] [--title TITLE]` triggers a scan job
- [x] **CLI-02**: `saneless devices` lists available SANE devices
- [x] **CLI-03**: `saneless jobs` lists recent job history

### Configuration

- [x] **CONF-01**: Configuration via pydantic-settings reading TOML file with env var overrides
- [x] **CONF-02**: Scanner host, device, paperless URL/token, tmp_dir, log settings, profiles all configurable
- [x] **CONF-03**: No hardcoded credentials; API token via config file or environment variable

### Architecture

- [x] **ARCH-01**: Scanner access isolated behind an abstraction layer (interface methods for device enumeration, capability queries, page acquisition)
- [x] **ARCH-02**: Background worker thread with queue.Queue for job coordination
- [x] **ARCH-03**: SANE initialized once at startup (never re-init per scan to avoid fd leaks)

### Error Handling & Logging

- [x] **LOG-01**: All errors and significant events written to a rotating log file at configurable path
- [x] **LOG-02**: Log level configurable (DEBUG, INFO, WARNING, ERROR)
- [x] **LOG-03**: All scan/API/assembly errors displayed in web UI for current or most recent job
- [x] **LOG-04**: Temporary files cleaned up on error -- no orphaned scan data

### Packaging

- [x] **PKG-01**: pip-installable Python package (pyproject.toml, published to PyPI)
- [x] **PKG-02**: OCI container image published to GHCR with HEALTHCHECK instruction
- [x] **PKG-03**: Container does not require --privileged; USB handled by saned server

### Review Hardening

- [x] **RH-01**: System checks available disk space before multi-page scans and fails fast if insufficient for estimated page count at current DPI
- [x] **RH-02**: Manual duplex page count mismatch saves front pages as partial PDF to consume directory instead of discarding all scanned data
- [x] **RH-03**: Worker state transitions use typed enum events instead of string matching against log messages
- [x] **RH-04**: Paperless test endpoint sanitizes exception messages in 502 responses to prevent leaking tokens or internal IPs
- [x] **RH-05**: Config validation checks tmp_dir and consume_dir are writable at startup, failing fast on permission errors
- [x] **RH-06**: Job history pruning runs periodically during runtime, not only at startup
- [x] **RH-07**: ProfileConfig has enable_empty_page_detection boolean toggle to bypass blank detection without threshold manipulation
- [x] **RH-08**: Uvicorn test server disables signal handlers when run in non-main thread
- [x] **RH-09**: Docker Compose config documents requirement for host config.toml to exist before first run

### Network Scanner Discovery

- [ ] **NET-01**: SaneBackend sets SANE_NET_HOSTS environment variable when scanner.host config is non-empty, before sane.init()
- [ ] **NET-02**: SaneBackend does not override an externally-set SANE_NET_HOSTS (e.g. from Docker -e or /etc/sane.d/net.conf)
- [ ] **NET-03**: All CLI commands (scan, devices, serve, auto-profiles) pass settings.scanner.host to SaneBackend constructor
- [ ] **NET-04**: Docker Compose example documents SANELESS_SCANNER__HOST env var for network scanner discovery

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
| SCAN-01 | Phase 1 | Complete |
| SCAN-02 | Phase 1 | Complete |
| SCAN-03 | Phase 1 | Complete |
| PROF-01 | Phase 1 | Complete |
| PROF-02 | Phase 1 | Complete |
| PDF-01 | Phase 1 | Complete |
| PDF-02 | Phase 1 | Complete |
| PLSS-01 | Phase 1 | Complete |
| PLSS-02 | Phase 1 | Complete |
| PLSS-03 | Phase 6 | Complete |
| CONF-01 | Phase 1 | Complete |
| CONF-02 | Phase 1 | Complete |
| CONF-03 | Phase 1 | Complete |
| ARCH-01 | Phase 1 | Complete |
| ARCH-02 | Phase 1 | Complete |
| ARCH-03 | Phase 1 | Complete |
| LOG-01 | Phase 1 | Complete |
| LOG-02 | Phase 1 | Complete |
| LOG-04 | Phase 1 | Complete |
| CLI-01 | Phase 1 | Complete |
| CLI-02 | Phase 1 | Complete |
| SCAN-04 | Phase 2 | Complete |
| SCAN-05 | Phase 2 | Complete |
| SCAN-06 | Phase 2 | Complete |
| SCAN-07 | Phase 2 | Complete |
| SCAN-08 | Phase 2 | Complete |
| SCAN-09 | Phase 2 | Complete |
| SCAN-10 | Phase 2 | Complete |
| SCAN-11 | Phase 2 | Complete |
| SCAN-12 | Phase 2 | Complete |
| UI-01 | Phase 3 | Complete |
| UI-02 | Phase 6 | Complete |
| UI-03 | Phase 3 | Complete |
| UI-04 | Phase 3 | Complete |
| UI-05 | Phase 3 | Complete |
| UI-06 | Phase 3 | Complete |
| UI-07 | Phase 3 | Complete |
| UI-08 | Phase 3 | Complete |
| PROF-03 | Phase 3 | Complete |
| PLSS-04 | Phase 3 | Complete |
| PLSS-05 | Phase 3 | Complete |
| HLTH-01 | Phase 3 | Complete |
| HLTH-02 | Phase 3 | Complete |
| LOG-03 | Phase 3 | Complete |
| PKG-01 | Phase 4 | Complete |
| PKG-02 | Phase 4 | Complete |
| PKG-03 | Phase 4 | Complete |
| PLSS-06 | Phase 4 | Complete |
| CLI-03 | Phase 4 | Complete |
| DPI-01 | Phase 11 | Complete |
| NET-01 | Phase 14 | Pending |
| NET-02 | Phase 14 | Pending |
| NET-03 | Phase 14 | Pending |
| NET-04 | Phase 14 | Pending |

**Coverage:**
- v1 requirements: 54 total
- Mapped to phases: 54
- Unmapped: 0
- Satisfied: 50
- Pending: 4 (NET-01, NET-02, NET-03, NET-04)

---
*Requirements defined: 2026-03-20*
*Last updated: 2026-03-22 after Phase 14 planning*
