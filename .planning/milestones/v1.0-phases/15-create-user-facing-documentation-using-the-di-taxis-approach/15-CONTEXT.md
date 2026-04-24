# Phase 15: User-Facing Documentation (Diátaxis) - Context

**Gathered:** 2026-03-22
**Status:** Ready for planning

<domain>
## Phase Boundary

Create user-facing documentation using the Diátaxis framework (tutorials, how-to guides, reference, explanation). Documentation lives in-repo under `docs/` and is published via MkDocs + GitHub Pages. No code changes — documentation only. README remains the landing page with links into the docs site.

</domain>

<decisions>
## Implementation Decisions

### Documentation framework and hosting
- **D-01:** MkDocs with Material for MkDocs theme — standard for Python projects, Markdown-native, zero-friction for contributors
- **D-02:** Published to GitHub Pages via `gh-pages` branch, built by GitHub Actions on push to main
- **D-03:** Source lives in `docs/` directory with `mkdocs.yml` at repo root
- **D-04:** MkDocs is a dev dependency (`uv add --dev mkdocs-material`)

### Diátaxis quadrant coverage
- **D-05:** All four Diátaxis quadrants included:
  - **Tutorials** (learning-oriented): "Scan your first document" end-to-end walkthrough
  - **How-to guides** (task-oriented): Specific tasks like "Set up manual duplex scanning", "Deploy with Docker Compose", "Configure auto-profiles"
  - **Reference** (information-oriented): CLI commands, configuration options, environment variables, API endpoints
  - **Explanation** (understanding-oriented): Architecture overview, how the scan pipeline works, why certain design choices were made

### Content scope — what to document
- **D-06:** Tutorial: One tutorial — "Scan your first document" covering install → config → first scan → verify in paperless-ngx
- **D-07:** How-to guides (6 guides):
  1. Install on bare metal (pip + libsane-dev)
  2. Deploy with Docker Compose (alongside paperless-ngx)
  3. Configure scan profiles (source, resolution, color mode, metadata defaults)
  4. Set up ADF duplex scanning (simplex, hardware duplex, manual duplex)
  5. Set up scanner host discovery for containers (SANE_NET_HOSTS wiring)
  6. Use the CLI for scripting/automation (scan, devices, jobs, auto-profiles with --json)
- **D-08:** Reference:
  1. CLI command reference (all 5 commands with flags, exit codes, output formats)
  2. Configuration reference (full TOML schema with all fields, defaults, types)
  3. Environment variables reference (SANELESS_ prefix mapping)
  4. Web API reference (health, scan, flip, paperless test, status endpoints)
  5. Docker reference (image, ports, volumes, env vars, healthcheck)
- **D-09:** Explanation:
  1. Architecture overview (scanner → pipeline → PDF → paperless, worker thread model)
  2. How empty page detection works (luminance + stddev thresholds)
  3. How consume directory fallback works (when paperless API is unavailable)

### Audience handling
- **D-10:** Primary audience is technical self-hosters (homelab). Write for someone who knows Docker and Linux but hasn't used SANE before
- **D-11:** Tutorial is written for the least technical user — step-by-step with copy-paste commands
- **D-12:** Reference docs are terse and complete — no hand-holding, just facts

### Documentation style
- **D-13:** No screenshots of the web UI (they go stale fast). Use text descriptions and config examples instead
- **D-14:** All code examples must be copy-pasteable (full commands, not fragments)
- **D-15:** Config examples use realistic values (192.168.x.x IPs, plausible paperless URLs)
- **D-16:** Each page has a clear "what you'll need" or "prerequisites" section where applicable

### README relationship
- **D-17:** README stays as-is (concise quick-start) with an added "Documentation" section linking to the full docs site
- **D-18:** README does NOT duplicate content from docs — it's the elevator pitch + quick start only

### Claude's Discretion
- Navigation structure and sidebar ordering
- MkDocs plugins beyond material theme
- Exact wording and content of each page
- Whether to add admonitions (tips, warnings) and where
- Footer, logo, color scheme choices for docs site

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Product context
- `docs/PRD.md` — Full product requirements, deployment topology, non-functional requirements
- `.planning/PROJECT.md` — Core value statement, validated requirements list, key decisions table

### Existing documentation
- `README.md` — Current quick-start documentation (to be linked from, not duplicated)
- `docker-compose.yml` — Docker Compose example with inline comments (source of truth for container deployment docs)

### Configuration surface
- `src/saneless/config.py` — All pydantic-settings config models (TOML fields, defaults, env var mapping)
- `src/saneless/cli.py` — All CLI commands, flags, and help text

### Feature implementations (for accuracy)
- `src/saneless/scanner/base.py` — Scanner abstraction interface
- `src/saneless/pipeline.py` — Scan pipeline stages and events
- `src/saneless/pages.py` — Empty page detection algorithm
- `src/saneless/paperless.py` — Paperless-ngx client with consume dir fallback
- `src/saneless/auto_profiles.py` — Auto-profile generation logic
- `src/saneless/web/routes.py` — Web API endpoints

### Prior phase context
- `.planning/phases/14-enable-containerized-scanner-detection-by-wiring-scanner-host-config-into-sane-net-backend-via-sane-net-hosts-environment-variable/14-CONTEXT.md` — Scanner host wiring decisions
- `.planning/phases/04-packaging-and-deployment/04-CONTEXT.md` — Docker, PyPI, CLI jobs decisions

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `README.md`: Current quick-start content — reuse as basis for tutorial and install guide
- `docker-compose.yml`: Complete Docker Compose example with comments — reference for container deployment guide
- `docs/PRD.md`: Problem statement and goals — reuse for explanation section's "why saneless exists"
- CLI `--help` output: Authoritative flag/option documentation — extract for CLI reference

### Established Patterns
- Config uses pydantic-settings: TOML file + `SANELESS_` env var prefix with `__` nesting — document this mapping
- 5 CLI commands: `scan`, `devices`, `jobs`, `serve`, `auto-profiles` — all support `--json` where applicable
- Web API follows REST conventions with `/api/` prefix and `/health` at root

### Integration Points
- `mkdocs.yml` at repo root — new file
- `docs/` directory — currently only has `PRD.md`, new docs go alongside or in subdirectories
- GitHub Actions — new workflow for docs publishing (or add to existing CI)
- `pyproject.toml` — add mkdocs-material as dev dependency

</code_context>

<specifics>
## Specific Ideas

- Diátaxis is the explicit framework requested — four quadrants must be clearly separated in navigation
- "Scan your first document" tutorial is the flagship entry point for new users
- Docker Compose guide should show saneless alongside paperless-ngx (the common deployment)
- CLI reference should include exit codes and `--json` output shapes for automation users

</specifics>

<deferred>
## Deferred Ideas

- API documentation auto-generation (OpenAPI/Swagger) — could be a future enhancement
- Man pages for CLI commands — out of scope for web-based docs phase
- Translated documentation — future if community demands it
- Video tutorials — not in scope for this text-based documentation phase

</deferred>

---

*Phase: 15-create-user-facing-documentation-using-the-di-taxis-approach*
*Context gathered: 2026-03-22*
