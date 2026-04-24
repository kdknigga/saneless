# Phase 4: Packaging and Deployment - Context

**Gathered:** 2026-03-20
**Status:** Ready for planning

<domain>
## Phase Boundary

pip-installable Python package published to PyPI, OCI container image published to GHCR with HEALTHCHECK, consume directory fallback completion, and `saneless jobs` CLI command. CI/CD workflows for automated publishing. No new scanning features, no UI changes, no auth.

</domain>

<decisions>
## Implementation Decisions

### Dockerfile design
- Two-stage build: build stage installs uv and builds wheel, runtime stage copies wheel and installs with pip
- Runtime base image: `python:3.14-slim` — compatible with C extensions (python-sane), smaller than full image, more reliable than alpine for libsane
- System packages in runtime: `libsane` only (minimal footprint per PRD §8)
- HEALTHCHECK instruction: `curl`-based, `--interval=30s --timeout=5s --retries=3` pinging `GET /health` (per PRD §8)
- No `--privileged` required — USB device access handled by external `saned` server (PRD §8, PKG-03)
- No hardcoded credentials — API token via config file or environment variable
- Expose port 8080 (matches `OutputConfig.web_port` default of 8080)
- Config injected via volume mount (`/etc/saneless/config.toml`) or environment variables (SANELESS_ prefix)
- Consume directory as optional volume mount

### CI/CD & publishing
- GitHub Actions workflow triggered on tag push (`v*`)
- PyPI publishing via trusted publisher (GitHub Actions OIDC) — no API tokens needed
- GHCR image tagged with version + `latest`
- Docker Compose example included in repo for quick deployment reference
- CI runs linting (ruff) and tests (pytest) before publishing; type checking (ty, pyrefly) omitted from CI because Python 3.14 may not be available on ubuntu-latest — pre-commit hooks enforce type checking locally before tagging

### CLI `jobs` command
- Human-readable table by default: timestamp, profile, title, status columns — matches `devices` command pattern
- `--json` flag for programmatic output (JSON array)
- `--limit N` flag to control row count, default 20 most recent
- Uses existing `JobStore.list_recent()` — no new persistence needed
- Exit code 0 always (informational command)

### Consume directory completion
- Core fallback logic already implemented in `PaperlessClient.upload()` and `run_pipeline()`
- Remaining work: startup validation (warn if configured dir doesn't exist), create on first use
- Integration testing to verify fallback triggers correctly when paperless-ngx is unreachable
- Document consume directory config in README

### Claude's Discretion
- Exact Dockerfile layer ordering and caching optimization
- GitHub Actions workflow YAML structure
- Docker Compose example details (network mode, restart policy, volume paths)
- Package metadata (classifiers, long description, project URLs)
- Whether to include `curl` or `wget` in container for HEALTHCHECK (curl preferred for consistency)
- README structure and install guide content

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Product requirements
- `docs/PRD.md` — Full product requirements document
- `docs/PRD.md` §8 — Non-functional requirements: container image constraints, no --privileged, HEALTHCHECK spec, no hardcoded credentials, dependency footprint
- `docs/PRD.md` §7.5 — Paperless-ngx ingestion: consume directory fallback requirement
- `docs/PRD.md` §7.7 — CLI requirements: `saneless jobs` command
- `docs/PRD.md` §10 — Phase 4 scope: pip package, OCI image, docker-compose example, consume directory fallback

### Prior phase context
- `.planning/phases/01-core-pipeline/01-CONTEXT.md` — CLI output patterns, config defaults, paperless upload behavior (retry + consume dir fallback)
- `.planning/phases/03-web-ui/03-CONTEXT.md` — Health endpoint design (used by HEALTHCHECK)

### Existing implementation
- `pyproject.toml` — Build system (uv_build), entry point, dependencies
- `src/saneless/paperless.py` — PaperlessClient with consume_dir fallback already implemented
- `src/saneless/pipeline.py` — Pipeline handles "fallback" task UUID from consume dir
- `src/saneless/config.py` — PaperlessConfig.consume_dir field exists
- `src/saneless/cli.py` — Click CLI group pattern for `jobs` command
- `src/saneless/job.py` — JobStore.list_recent() for job history data

### Requirements
- `.planning/REQUIREMENTS.md` — PKG-01, PKG-02, PKG-03, PLSS-06, CLI-03

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `src/saneless/cli.py`: Click CLI group with `scan` and `devices` commands — add `jobs` following same pattern
- `src/saneless/job.py`: `JobStore.list_recent()` returns job history — direct reuse for CLI `jobs` command
- `src/saneless/paperless.py`: `PaperlessClient` already accepts `consume_dir` and falls back on upload failure
- `src/saneless/pipeline.py`: Pipeline already handles `"fallback"` return from consume dir upload
- `src/saneless/config.py`: `PaperlessConfig.consume_dir` field already exists
- `src/saneless/web/routes.py`: Health endpoint at `GET /health` — HEALTHCHECK target

### Established Patterns
- Click CLI: `@cli.command()` with `@click.pass_context`, `--json` flag for programmatic output
- Table output: manual formatting with f-strings and column widths (see `devices` command)
- Config: pydantic-settings with TOML + env var override (SANELESS_ prefix)
- Build system: `uv_build` backend in pyproject.toml
- Entry point: `saneless = "saneless:main"` in `[project.scripts]`

### Integration Points
- `JobStore` requires SQLite database path — CLI `jobs` needs access to same DB as web layer
- `consume_dir` validation at startup — add to config validation in `load_settings()`
- GitHub Actions needs access to PyPI (OIDC trusted publisher) and GHCR (GITHUB_TOKEN)

</code_context>

<specifics>
## Specific Ideas

- PRD §8 specifies exact HEALTHCHECK parameters: `--interval=30s --timeout=5s --retries=3`
- PRD explicitly states OCI image must NOT bundle `saned` — saneless only
- Docker Compose example mentioned in PRD §10 Phase 4 scope
- `saneless jobs` follows established CLI pattern from `saneless devices` (table + `--json`)

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 04-packaging-and-deployment*
*Context gathered: 2026-03-20*
