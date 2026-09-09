# saneless

## What This Is

saneless is an open-source tool that bridges SANE-compatible network scanners (exposed via `saned`) and paperless-ngx. It provides a web UI and CLI for triggering scans, assembling multi-page PDFs, and ingesting documents into paperless-ngx with rich metadata — all from any device on the local network.

## Core Value

A user can walk up to the web UI, click Scan, and have a correctly assembled PDF land in paperless-ngx with metadata — without touching any other tool.

## Current Milestone: v2.0 Prep for release

**Goal:** Resolve every finding in the 2026-09-09 comprehensive code review (`.planning/reviews/2026-09-09-code-review.md`) so saneless ships as a trustworthy, appliance-grade release under its real name (`kdknigga/scanless`).

**Target features:**
- Honest outcomes: typed pipeline results, `FALLBACK` job state, scans never deleted on upload failure, unique PDF names (C-03, C-04, C-05, M-22)
- Working manual duplex: `duplex` profile field, CLI flip prompt, flip timeout, visible reverse-pass state (C-01, C-02, M-02, M-07)
- Truthful scanner layer: one source classifier, real error messages, geometry/DPI correctness, fakes that model real python-sane (C-06, M-11..M-16, M-32)
- Robust worker and web layer: locked job store, unkillable worker with 429 backpressure, sync routes, crash recovery, server-owned Scan button, single state enum (C-07, C-09, C-10, M-01, M-03, M-05)
- Strict configuration: forbid unknown keys, refuse missing `--config`, XDG/`~` expansion, validated log level, default profile always emitted, atomic merged writes, `SecretStr` (C-08, M-04, M-09, M-10, M-18..M-21, M-24)
- Exception translation at every module boundary (M-17, N-06, N-08)
- Delivery: CI on every push, fixed release workflow, `kdknigga/scanless` naming everywhere, container logging/port/mount fixes, `.dockerignore` (M-25..M-31)
- Geometry, memory, timeouts: DPI in PDFs, spool pages to disk, safe cancel, flatbed timeout (M-06, M-08, M-12, M-13)
- Hermetic, fast, meaningful test suite (M-33, M-34, N-18, N-24, N-40)
- Full minor/nit sweep of N-01..N-45
- Appliance layer: status strip + `saneless doctor`, page counts, plain-language errors, human profile labels generated at startup, one place for the token, queue visibility and owner-only flip prompt, help text and phone-friendly tag picker, trust-model and "which setup do I have" docs (U-01..U-10)

**Key context:** Phases continue from 20. Review section 10 gives an ordered remediation plan (steps 1-11) that the roadmap follows, since each step's tests protect the next. The ten CRITICAL fixes are prerequisites for the appliance layer. No git tags.

## Requirements

### Validated

- ✓ Python 3.14 project scaffolding with UV, ruff, pytest, pre-commit — existing
- ✓ pyproject.toml with entry point `saneless = "saneless:main"` — existing
- ✓ Scanner discovery and device selection via SANE — Phase 1
- ✓ Scan profiles (source, resolution, color mode, default metadata) defined in TOML config — Phase 1
- ✓ Flatbed single-page scanning — Phase 1
- ✓ PDF assembly via `img2pdf` (lossless) — Phase 1
- ✓ Paperless-ngx REST API ingestion with metadata (title, tags, correspondent, created) — Phase 1
- ✓ Paperless-ngx task polling until terminal state — Phase 1
- ✓ Paperless-ngx connection test endpoint (3 distinct failure modes) — Phase 1
- ✓ pydantic-settings configuration (TOML + env var override) — Phase 1
- ✓ Scanner abstraction layer (interface for pluggable backends) — Phase 1
- ✓ Background worker thread with `queue.Queue` (single concurrent scan) — Phase 1
- ✓ Rotating log file with configurable level and path — Phase 1
- ✓ Temporary file cleanup on success and error — Phase 1
- ✓ CLI: `saneless scan`, `saneless devices` — Phase 1
- ✓ ADF multi-page scanning with `multi_scan()` — Phase 2
- ✓ ADF duplex scanning (native hardware duplex) — Phase 2
- ✓ ADF manual duplex (two-pass with flip prompt, reverse-and-interleave) — Phase 2
- ✓ Empty page detection (mean luminance + stddev dual threshold) — Phase 2
- ✓ First-page thumbnail generation (base64 JPEG, long edge ≤ 300px) — Phase 2
- ✓ Web UI: profile selector, metadata fields, scan button, live status indicator — Phase 3
- ✓ Web UI: ADF manual duplex flip prompt with Continue/Cancel and flip illustration — Phase 3
- ✓ Web UI: first-page thumbnail preview — Phase 3
- ✓ Web UI: job history table (SQLite, pruned by age + count) — Phase 3
- ✓ Web UI: tag/correspondent dropdowns with TTL cache and per-resource refresh — Phase 3
- ✓ Health endpoint (`GET /health`, 200/503 based on worker thread state) — Phase 3
- ✓ FastAPI + Jinja2 + HTMX web layer with PicoCSS — Phase 3
- ✓ Automatic scanner profile generation from device capabilities — Phase 10
- ✓ CLI `saneless auto-profiles` command with `--force` flag — Phase 10
- ✓ Lazy auto-profile trigger on first scan (bare default detection) — Phase 10
- ✓ Comment-preserving TOML config writing via tomlkit — Phase 10
- ✓ Default scan resolution validated at 300 DPI, consolidated into single DEFAULT_RESOLUTION constant — Phase 11
- ✓ Humanized enum labels in job history table via Jinja2 filter — Phase 12
- ✓ Accessible button labels (aria-label + sr-only) on icon-only buttons — Phase 12
- ✓ External app.js replacing all inline scripts and hx-on attributes — Phase 12
- ✓ CSS spacing normalized to PicoCSS design token grid (no !important) — Phase 12
- ✓ CLI table truncation with terminal-aware column widths — Phase 12
- ✓ Containerized scanner discovery via SANE_NET_HOSTS env var injection from scanner.host config — Phase 14
- ✓ Consume directory fallback — Phase 4
- ✓ CLI: `saneless jobs` — Phase 4
- ✓ pip-installable package (pyproject.toml, PyPI) — Phase 4
- ✓ OCI container image (GHCR, HEALTHCHECK instruction) — Phase 4

- ✓ MkDocs Material documentation site with Diataxis structure (tutorials, how-to, reference, explanation) — Phase 15
- ✓ GitHub Actions docs deployment workflow — Phase 15
- ✓ Flagship tutorial "Scan Your First Document" (136 lines) — Phase 15
- ✓ 6 how-to guides (install, deploy, configure, ADF, scanner discovery, CLI scripting) — Phase 15
- ✓ 5 reference pages (CLI, config, env vars, web API, Docker) — Phase 15
- ✓ 3 explanation pages (architecture, empty-page detection, consume-dir fallback) — Phase 15
- ✓ README documentation section with docs site link — Phase 15
- ✓ Getting Started section (Quick Start, First CLI Scan, First Web UI Scan) as top-level nav — Phase 19
- ✓ auto_source_mode and paper_size woven into how-to guides — Phase 19
- ✓ Tutorial relocated from tutorials/ to getting-started/, refreshed for Python 3.14 — Phase 19

### Active

v2.0 requirements are defined in `.planning/REQUIREMENTS.md` (derived from the 2026-09-09 code review: 10 CRITICAL, 34 MAJOR, 45 MINOR/NIT, 10 USABILITY findings).

### Out of Scope

- Driverless scanning (eSCL, WSD, sane-airscan) — architecture supports future addition but not v1
- OCR — delegated to paperless-ngx
- Cloud storage / email / non-paperless-ngx destinations — v1 is paperless-ngx only
- Multi-user auth on web UI — assumes trusted LAN, auth deferred to reverse proxy
- `saned` management — external dependency, user configures separately
- `scanbd` hardware button integration — future work
- Heavier task queue (rq + Redis) — `queue.Queue` sufficient for v1

## Context

- Target audience: self-hosters running paperless-ngx on home/small-office LAN
- Users range from technical homelab operators to non-technical household members (web UI only)
- Deployment: saneless may run on same machine as `saned` or a separate host
- `saned` is always external; saneless never bundles or manages it
- Scanner is shared hardware — only one scan job at a time
- USB device access handled by `saned` server, not by saneless container
- Project scaffolding already exists: pyproject.toml, pre-commit hooks, ruff, pytest, playwright

## Constraints

- **Runtime**: Python 3.14+, `libsane-dev` (or `sane-backends-devel`) required for `python-sane` compilation
- **Scanner protocol**: `python-sane` via `saned` network backend only in v1
- **Container**: No `--privileged` required; USB passthrough handled by `saned`
- **Credentials**: API token via config file or env var, never baked into image
- **Response time**: Scan button must return job ID within 500ms
- **Concurrency**: Single active scan job; additional requests queued

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| FastAPI + uvicorn for web layer | Async-capable, modern Python web framework, good for API-first design | ✓ Good |
| `queue.Queue` + worker thread (not rq/Redis) | Sufficient for single-scanner v1; avoids infrastructure dependency | ✓ Good |
| `img2pdf` for PDF assembly | Lossless encoding, no re-compression of scanned images | ✓ Good |
| `pydantic-settings` for config | TOML + env var support, validation at startup, type safety | ✓ Good |
| Scanner abstraction layer from day one | Enables future driverless backend without pipeline changes | ✓ Good |
| SQLite for job persistence | Stdlib, no external database, sufficient for single-process model | ✓ Good |
| No web UI auth in v1 | Trusted LAN assumption; reverse proxy handles auth if needed | ✓ Good |
| Jinja2 + HTMX (no SPA) | No JS build step, server-rendered, declarative interactions | ✓ Good |
| PicoCSS classless styling | Minimal CSS, semantic HTML, auto dark mode, no build step | ✓ Good |
| Two-stage Dockerfile with uv_build | Lean OCI image — build stage creates wheel, runtime has only libsane + curl | ✓ Good |
| GitHub Actions OIDC trusted publishing | No API tokens for PyPI/GHCR — secure, no secret rotation | ✓ Good |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `/gsd-transition`):
1. Requirements invalidated? → Move to Out of Scope with reason
2. Requirements validated? → Move to Validated with phase reference
3. New requirements emerged? → Add to Active
4. Decisions to log? → Add to Key Decisions
5. "What This Is" still accurate? → Update if drifted

**After each milestone** (via `/gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-09-09 — started milestone v2.0 Prep for release, driven by the 2026-09-09 comprehensive code review*
