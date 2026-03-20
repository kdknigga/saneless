# Roadmap: saneless

## Overview

saneless delivers a scan-to-paperless-ngx bridge in four phases, structured bottom-up by dependency chain. Phase 1 proves the entire pipeline end-to-end (config, scanner abstraction, PDF assembly, paperless upload) via CLI. Phase 2 adds ADF multi-page and duplex scanning -- the hardest and most differentiating features. Phase 3 wraps the stable backend in a web UI with live status, metadata controls, and job history. Phase 4 packages everything for deployment as a pip package and OCI container.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [ ] **Phase 1: Core Pipeline** - Scanner abstraction, flatbed scan, PDF assembly, paperless-ngx upload, config, CLI
- [ ] **Phase 2: ADF and Multi-Page** - ADF scanning, hardware duplex, manual duplex, empty page detection, thumbnails
- [ ] **Phase 3: Web UI** - Browser interface with profiles, metadata, live status, job history, health endpoint
- [ ] **Phase 4: Packaging and Deployment** - pip package, OCI container, consume directory fallback

## Phase Details

### Phase 1: Core Pipeline
**Goal**: A user can run a CLI command that discovers a scanner, performs a flatbed scan, assembles a PDF, and uploads it to paperless-ngx with metadata
**Depends on**: Nothing (first phase)
**Requirements**: SCAN-01, SCAN-02, SCAN-03, PROF-01, PROF-02, PDF-01, PDF-02, PLSS-01, PLSS-02, PLSS-03, CONF-01, CONF-02, CONF-03, ARCH-01, ARCH-02, ARCH-03, LOG-01, LOG-02, LOG-04, CLI-01, CLI-02
**Success Criteria** (what must be TRUE):
  1. User can run `saneless devices` and see a list of available SANE scanners on the network
  2. User can run `saneless scan` and a flatbed scan produces a PDF that appears in paperless-ngx with the specified title
  3. User can define scan profiles in a TOML config file and select one via `--profile` flag
  4. Configuration loads from TOML file with environment variable overrides, and invalid config fails at startup with a clear error
  5. Scanner operations go through an abstraction layer that isolates python-sane behind clean interface methods
**Plans**: 3 plans

Plans:
- [ ] 01-01-PLAN.md -- Foundation: dependencies, config (pydantic-settings TOML + env), exceptions, logging
- [ ] 01-02-PLAN.md -- Core modules: scanner abstraction (ABC + SaneBackend), PDF assembly (img2pdf), paperless-ngx client
- [ ] 01-03-PLAN.md -- Integration: job model, worker thread, pipeline orchestration, Click CLI commands

### Phase 2: ADF and Multi-Page
**Goal**: Users can scan multi-page documents from the ADF in all modes (simplex, hardware duplex, manual duplex) with automatic empty page removal
**Depends on**: Phase 1
**Requirements**: SCAN-04, SCAN-05, SCAN-06, SCAN-07, SCAN-08, SCAN-09, SCAN-10, SCAN-11, SCAN-12
**Success Criteria** (what must be TRUE):
  1. User can load a stack of pages into the ADF and get a single multi-page PDF with all pages in order
  2. User can perform a manual duplex scan (two passes) and the system correctly interleaves front and back pages, rejecting mismatched page counts
  3. Empty pages are automatically detected and discarded before PDF assembly, with configurable thresholds per profile
  4. A thumbnail of the first scanned page is generated and available for downstream display
  5. Attempting to ADF-scan with an empty feeder produces a specific "No paper detected in feeder" error, not a generic failure
**Plans**: 3 plans

Plans:
- [ ] 02-01-PLAN.md -- Foundation types + page processing: config thresholds, AWAITING_FLIP state, FeederEmptyError, empty page detection, thumbnail generation
- [ ] 02-02-PLAN.md -- ADF scanner extension: multi_scan() for ADF/duplex sources, page validation, empty feeder detection, EXIF stripping
- [ ] 02-03-PLAN.md -- Pipeline + worker integration: manual duplex interleaving, empty page filtering, thumbnail callbacks, AWAITING_FLIP event coordination

### Phase 3: Web UI
**Goal**: Users can perform all scanning operations from a browser on any device on the LAN, with live feedback, metadata entry, and job history
**Depends on**: Phase 2
**Requirements**: UI-01, UI-02, UI-03, UI-04, UI-05, UI-06, UI-07, UI-08, PROF-03, PLSS-04, PLSS-05, HLTH-01, HLTH-02, LOG-03
**Success Criteria** (what must be TRUE):
  1. User can open the web UI in a browser, select a profile, enter metadata (title, tags, correspondent), and click Scan to start a job
  2. UI shows live status updates (idle/scanning/assembling/uploading/done/error) and displays the first-page thumbnail once available
  3. For manual duplex scans, UI shows a flip prompt with Continue and Cancel buttons and the scan button is disabled while a job is in progress
  4. User can browse job history showing timestamp, profile, title, and outcome -- with old entries automatically pruned
  5. `GET /health` returns 200 when the system is healthy and 503 when the worker thread is down, requiring no authentication
**Plans**: TBD

Plans:
- [ ] 03-01: TBD
- [ ] 03-02: TBD
- [ ] 03-03: TBD

### Phase 4: Packaging and Deployment
**Goal**: Users can install saneless via pip or deploy it as an OCI container with minimal configuration
**Depends on**: Phase 3
**Requirements**: PKG-01, PKG-02, PKG-03, PLSS-06, CLI-03
**Success Criteria** (what must be TRUE):
  1. User can `pip install saneless` and run the application with no additional build steps
  2. OCI container image runs without `--privileged`, includes a working HEALTHCHECK, and is published to GHCR
  3. User can configure a consume directory fallback that deposits PDFs to a local path when paperless-ngx API is unavailable
  4. User can run `saneless jobs` to view recent job history from the command line
**Plans**: TBD

Plans:
- [ ] 04-01: TBD

## Progress

**Execution Order:**
Phases execute in numeric order: 1 -> 2 -> 3 -> 4

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Core Pipeline | 0/3 | Planning complete | - |
| 2. ADF and Multi-Page | 0/3 | Planning complete | - |
| 3. Web UI | 0/3 | Not started | - |
| 4. Packaging and Deployment | 0/1 | Not started | - |
