# saneless

## What This Is

saneless is an open-source tool that bridges SANE-compatible network scanners (exposed via `saned`) and paperless-ngx. It provides a web UI and CLI for triggering scans, assembling multi-page PDFs, and ingesting documents into paperless-ngx with rich metadata — all from any device on the local network.

## Core Value

A user can walk up to the web UI, click Scan, and have a correctly assembled PDF land in paperless-ngx with metadata — without touching any other tool.

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

### Active

None — all v1 requirements validated.

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

---
*Last updated: 2026-03-22 after Phase 14 completion — containerized scanner detection via SANE_NET_HOSTS env var wiring from scanner.host config*
