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
- [ ] **Phase 5: Web Server Launch Command** - `saneless serve` CLI command, web logging, Dockerfile CMD
- [ ] **Phase 6: Gap Closure Fixes** - Paperless test route, worker intermediate states
- [ ] **Phase 7: Tech Debt Cleanup** - python-sane packaging, Starlette deprecation, error typing, flip timing, Playwright browser tests
- [x] **Phase 13: Review Hardening** - Disk space checks, duplex data preservation, typed state events, exception sanitization, config validation, periodic pruning, empty page toggle, signal handler fix, Docker docs (completed 2026-03-22)

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
**Plans**: 5 plans

Plans:
- [ ] 01-01-PLAN.md -- Foundation: dependencies, config (pydantic-settings TOML + env), exceptions, logging
- [ ] 01-02-PLAN.md -- Core modules: scanner abstraction (ABC + SaneBackend), PDF assembly (img2pdf), paperless-ngx client
- [ ] 01-03-PLAN.md -- Integration: job model, worker thread, pipeline orchestration, Click CLI commands
- [ ] 01-04-PLAN.md -- Gap closure: XDG-compliant log path default, graceful mkdir error handling
- [ ] 01-05-PLAN.md -- Gap closure: user-friendly TOML structure errors, title alias for default_title_template

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
**Plans**: 4 plans

Plans:
- [ ] 03-00-PLAN.md -- Wave 0: xfail test stubs and httpx dev dependency for Nyquist compliance
- [ ] 03-01-PLAN.md -- Backend extensions and FastAPI web application scaffold (app factory, cache, routes, health endpoint)
- [ ] 03-02-PLAN.md -- Complete Jinja2 templates with HTMX interactions (scan form, status polling, flip prompt, job history)
- [ ] 03-03-PLAN.md -- Test suite for web endpoints, cache, and JobStore extensions

### Phase 4: Packaging and Deployment
**Goal**: Users can install saneless via pip or deploy it as an OCI container with minimal configuration
**Depends on**: Phase 3
**Requirements**: PKG-01, PKG-02, PKG-03, PLSS-06, CLI-03
**Success Criteria** (what must be TRUE):
  1. User can `pip install saneless` and run the application with no additional build steps
  2. OCI container image runs without `--privileged`, includes a working HEALTHCHECK, and is published to GHCR
  3. User can configure a consume directory fallback that deposits PDFs to a local path when paperless-ngx API is unavailable
  4. User can run `saneless jobs` to view recent job history from the command line
**Plans**: 2 plans

Plans:
- [ ] 04-01-PLAN.md -- CLI `jobs` command and consume directory fallback completion
- [ ] 04-02-PLAN.md -- PyPI metadata, Dockerfile, Docker Compose, and GitHub Actions release workflow

### Phase 5: Web Server Launch Command
**Goal**: Users can start the web server via `saneless serve` and deploy via Docker container with working healthcheck
**Depends on**: Phase 4
**Requirements**: UI-01, UI-02, UI-03, UI-04, UI-05, UI-06, UI-07, UI-08, PROF-03, PLSS-04, PLSS-05, HLTH-01, HLTH-02, LOG-01, LOG-02, LOG-03, PKG-02
**Gap Closure:** Closes GAP-01, integration gaps (cli->web, web->logging), broken flows (web UI, container deployment)
**Success Criteria** (what must be TRUE):
  1. User can run `saneless serve` and the web UI is accessible in a browser at the configured host/port
  2. Web server startup calls `configure_logging()` so logs go to the configured rotating file, not Python's default logger
  3. Docker container starts with `CMD ["serve"]`, healthcheck passes, and container stays running
**Plans**: 1 plans

Plans:
- [ ] 05-01-PLAN.md -- Add `saneless serve` CLI command with uvicorn, Dockerfile CMD, and tests

### Phase 6: Gap Closure Fixes
**Goal**: Close remaining requirement gaps -- paperless test endpoint route and worker intermediate status states
**Depends on**: Phase 5
**Requirements**: PLSS-03, UI-02
**Gap Closure:** Closes GAP-02 (PLSS-03 route), GAP-03 (UI-02 intermediate states)
**Success Criteria** (what must be TRUE):
  1. `GET /api/paperless/test` returns JSON with connection status distinguishing connected/token_rejected/unreachable
  2. Worker emits ASSEMBLING state before PDF assembly and UPLOADING state before paperless upload, visible in web UI status polling
**Plans**: 2 plans

Plans:
- [ ] 06-01-PLAN.md -- Paperless test route, worker ASSEMBLING/UPLOADING state transitions, and tests
- [ ] 06-02-PLAN.md -- Gap closure: fix test_connection to use auth-requiring endpoint (/api/tags/) instead of /api/

### Phase 7: Tech Debt Cleanup
**Goal**: Address accumulated tech debt from v1.0 milestone audit — packaging gaps, deprecation warnings, error handling clarity, flip flow timing, and browser rendering verification
**Depends on**: Phase 6
**Requirements**: PKG-01, UI-01, UI-02, UI-03, ARCH-02
**Tech Debt Closure:** Closes 5 of 6 tech debt items from v1.0 audit (1 deferred: physical scanner verification)
**Success Criteria** (what must be TRUE):
  1. `python-sane` is a mandatory dependency in `pyproject.toml` and Dockerfile installs it correctly
  2. Zero Starlette `TemplateResponse` deprecation warnings across all 13 call sites
  3. `JobState` distinguishes error categories (feeder, config, scanner, upload) rather than relying on error message text
  4. POST to `/api/flip/continue` returns response only after worker has transitioned out of `AWAITING_FLIP` state
  5. Playwright tests verify PicoCSS/HTMX rendering, live status polling, and flip prompt UI in a real browser
**Plans**: 2 plans

Plans:
- [ ] 07-01-PLAN.md -- Backend hardening: python-sane mandatory dep, ErrorCategory enum, typed exception handling, flip timing synchronization
- [ ] 07-02-PLAN.md -- Playwright browser tests for PicoCSS rendering, HTMX polling, flip prompt UI, scan form

## Progress

**Execution Order:**
Phases execute in numeric order: 1 -> 2 -> 3 -> 4

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Core Pipeline | 0/4 | Planning complete | - |
| 2. ADF and Multi-Page | 0/3 | Planning complete | - |
| 3. Web UI | 1/4 | In Progress|  |
| 4. Packaging and Deployment | 0/2 | Planning complete | - |
| 5. Web Server Launch Command | 0/1 | Planning complete | - |
| 6. Gap Closure Fixes | 0/1 | Planning complete | - |
| 7. Tech Debt Cleanup | 0/2 | Planning complete | - |

### Phase 8: Audit Lint and Type Checker Ignores

**Goal:** Zero unjustified suppressions in production code -- fix every fixable # noqa and # type: ignore, document remaining justified ones, tighten per-file-ignores
**Requirements**: AUDIT-01, AUDIT-02, AUDIT-03, AUDIT-04
**Depends on:** Phase 7
**Plans:** 1/1 plans complete

Plans:
- [ ] 08-01-PLAN.md -- Fix httpx type: ignore, document justified suppressions, tighten per-file-ignores

### Phase 9: Enable Pytest Strict Mode and Test Quality Parity

**Goal:** Full test quality parity with production code -- type annotations, docstrings, and lint compliance on all test files, with pytest strict mode enforcing warnings-as-errors and strict markers
**Requirements**: TQUAL-01, TQUAL-02, TQUAL-03, TQUAL-04, TQUAL-05, TQUAL-06, TQUAL-07
**Depends on:** Phase 8
**Plans:** 2/3 plans executed

Plans:
- [ ] 09-01-PLAN.md -- Fix type checker errors in test files, replace mock patterns with concrete stubs, remove ty/pyrefly exclusions
- [ ] 09-02-PLAN.md -- Add type annotations, docstrings, move lazy imports to top-level, remove ANN/PLC0415 per-file-ignores
- [ ] 09-03-PLAN.md -- Fix ResourceWarning from unclosed SQLite connections, enable pytest strict configuration

### Phase 10: Automatic scanner profile creation

**Goal:** Users get scan profiles auto-generated from scanner capabilities on first use, and can explicitly regenerate via `saneless auto-profiles` CLI command
**Requirements**: AP-01, AP-02, AP-03, AP-04, AP-05, AP-06, AP-07, AP-08, AP-09
**Depends on:** Phase 9
**Plans:** 2/2 plans complete

Plans:
- [ ] 10-01-PLAN.md -- Core auto_profiles module: pure generation functions, TOML persistence, tomlkit dep, ProfileConfig auto_generated field (TDD)
- [ ] 10-02-PLAN.md -- CLI auto-profiles command and worker lazy trigger integration

### Phase 11: Review and adjust default DPI setting

**Goal:** Validate 300 DPI as the optimal default for document scanning (Tesseract OCR minimum recommendation) and consolidate the duplicated DPI value into a single DEFAULT_RESOLUTION constant
**Requirements**: DPI-01
**Depends on:** Phase 10
**Plans:** 1/1 plans complete

Plans:
- [ ] 11-01-PLAN.md -- Consolidate DEFAULT_RESOLUTION constant in config.py, update auto_profiles.py and tests

### Phase 12: UI polish: humanize enum labels, add accessible button labels, fix CLI table truncation, replace inline HTMX scripts, normalize spacing to design token grid

**Goal:** Cosmetic and accessibility polish across web UI and CLI -- humanize raw enum labels, add ARIA attributes to icon buttons, extract inline scripts to external JS, normalize CSS spacing to PicoCSS design token grid, and fix CLI table truncation with terminal-aware column widths
**Requirements**: P12-01, P12-02, P12-03, P12-04, P12-05
**Depends on:** Phase 11
**Plans:** 2/2 plans complete

Plans:
- [ ] 12-01-PLAN.md -- Web UI polish: humanize_state Jinja2 filter, accessible button labels, extract inline scripts to app.js, normalize CSS spacing, copywriting fixes
- [ ] 12-02-PLAN.md -- CLI table truncation: _truncate helper with terminal-aware column widths for devices and jobs commands

### Phase 13: Review hardening -- cross-AI review findings

**Goal:** Address 9 hardening items identified by cross-AI plan review (Gemini CLI) -- disk space pre-flight checks, manual duplex data preservation, typed state machine events, exception sanitization, config writability validation, periodic job pruning, empty page detection toggle, threaded Uvicorn signal fix, and Docker Compose documentation
**Requirements**: RH-01, RH-02, RH-03, RH-04, RH-05, RH-06, RH-07, RH-08, RH-09
**Depends on:** Phase 12
**Plans:** 3/3 plans complete

Plans:
- [x] 13-01-PLAN.md -- Simple independent fixes: exception sanitization, config writability validation, empty page toggle, Uvicorn signal fix, Docker Compose docs
- [x] 13-02-PLAN.md -- Core pipeline hardening: PipelineEvent typed enum, disk space pre-flight check, post-job pruning
- [x] 13-03-PLAN.md -- Manual duplex mismatch recovery: save partial PDFs and upload both to paperless-ngx

**Success Criteria** (what must be TRUE):
  1. Multi-page scan pipeline checks disk space before starting and raises a clear error if estimated space exceeds available
  2. Manual duplex page count mismatch saves front pages as partial PDF to consume directory, not silently discarded
  3. Worker state transitions use a typed enum callback, not string comparison against log messages
  4. `GET /api/paperless/test` 502 response contains sanitized error detail, never raw exception strings with tokens or IPs
  5. Config validation at startup fails fast if tmp_dir or consume_dir paths are not writable
  6. Job history is pruned periodically during runtime (not only at application startup)
  7. `ProfileConfig.enable_empty_page_detection` boolean toggle exists and defaults to True
  8. Browser test Uvicorn server sets `install_signal_handlers=False` to prevent ValueError in non-main thread
  9. docker-compose.yml includes a comment warning that config.toml must exist on host before first run

### Phase 14: Enable containerized scanner detection by wiring scanner.host config into SANE net backend via SANE_NET_HOSTS environment variable

**Goal:** Containerized saneless discovers network scanners when user sets `scanner.host` in config -- the application wires this into SANE's net backend via the `SANE_NET_HOSTS` environment variable before `sane.init()`
**Requirements**: NET-01, NET-02, NET-03, NET-04
**Depends on:** Phase 13
**Plans:** 1/1 plans complete

Plans:
- [x] 14-01-PLAN.md -- Wire scanner.host into SANE_NET_HOSTS env var, update CLI call sites, add docker-compose example

**Success Criteria** (what must be TRUE):
  1. `SaneBackend(host="192.168.1.50")` sets `SANE_NET_HOSTS=192.168.1.50` before `sane.init()`
  2. If `SANE_NET_HOSTS` is already set externally, the application does not override it
  3. All 4 CLI commands pass `settings.scanner.host` to the `SaneBackend` constructor
  4. `docker-compose.yml` documents `SANELESS_SCANNER__HOST` as a commented-out example

### Phase 15: Create user-facing documentation using the Diataxis approach

**Goal:** Complete user-facing documentation site with all four Diataxis quadrants (tutorials, how-to guides, reference, explanation) published via MkDocs Material to GitHub Pages
**Requirements**: D-01, D-02, D-03, D-04, D-05, D-06, D-07, D-08, D-09, D-10, D-11, D-12, D-13, D-14, D-15, D-16, D-17, D-18
**Depends on:** Phase 14
**Plans:** 4/4 plans complete

Plans:
- [x] 15-01-PLAN.md -- MkDocs scaffolding (config, theme, GitHub Actions, landing page) and "Scan Your First Document" tutorial
- [x] 15-02-PLAN.md -- How-to guides: install, Docker Compose, scan profiles, ADF duplex, scanner host discovery, CLI scripting
- [x] 15-03-PLAN.md -- Reference pages: CLI commands, configuration, environment variables, web API, Docker
- [x] 15-04-PLAN.md -- Explanation pages: architecture, empty page detection, consume directory fallback; README docs link

**Success Criteria** (what must be TRUE):
  1. `uv run mkdocs build --strict` exits 0 with all 16 pages (index + 15 content pages)
  2. Documentation site has four clearly separated Diataxis quadrants in navigation
  3. Tutorial walks new user from install to verified scan in paperless-ngx
  4. All how-to guides have prerequisites sections and copy-paste commands
  5. Reference pages are terse and complete with all CLI flags, config fields, env vars, and API endpoints
  6. No screenshots in any documentation page
  7. README has a Documentation section linking to the docs site without duplicating content
  8. GitHub Actions workflow deploys docs to GitHub Pages on push to main
