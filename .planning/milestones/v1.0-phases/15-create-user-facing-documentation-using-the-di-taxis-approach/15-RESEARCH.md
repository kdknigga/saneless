# Phase 15: User-Facing Documentation (Diataxis) - Research

**Researched:** 2026-03-22
**Domain:** Documentation tooling (MkDocs + Material), Diataxis framework, GitHub Pages deployment
**Confidence:** HIGH

## Summary

This phase creates user-facing documentation for saneless using the Diataxis framework (tutorials, how-to guides, reference, explanation) published via MkDocs with Material for MkDocs theme to GitHub Pages. The project currently has only a concise README and a `docs/PRD.md` (internal). All user decisions are locked -- MkDocs Material, GitHub Pages via GitHub Actions, `docs/` directory, dev dependency. The content scope is well-defined: 1 tutorial, 6 how-to guides, 5 reference pages, 3 explanation pages.

This is a documentation-only phase. No application code changes. The deliverables are: `mkdocs.yml` configuration, `docs/` directory with all pages, a GitHub Actions workflow for deployment, the `mkdocs-material` dev dependency, and a "Documentation" link section added to README.

**Primary recommendation:** Structure as three plans: (1) MkDocs scaffolding + GitHub Actions + tutorial, (2) how-to guides + reference pages, (3) explanation pages + README link + final review.

<user_constraints>

## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** MkDocs with Material for MkDocs theme -- standard for Python projects, Markdown-native, zero-friction for contributors
- **D-02:** Published to GitHub Pages via `gh-pages` branch, built by GitHub Actions on push to main
- **D-03:** Source lives in `docs/` directory with `mkdocs.yml` at repo root
- **D-04:** MkDocs is a dev dependency (`uv add --dev mkdocs-material`)
- **D-05:** All four Diataxis quadrants included (tutorials, how-to guides, reference, explanation)
- **D-06:** Tutorial: One tutorial -- "Scan your first document" covering install -> config -> first scan -> verify in paperless-ngx
- **D-07:** How-to guides (6 guides): bare metal install, Docker Compose deploy, scan profiles, ADF duplex, scanner host discovery, CLI scripting
- **D-08:** Reference (5 pages): CLI commands, config TOML schema, environment variables, web API, Docker
- **D-09:** Explanation (3 pages): architecture overview, empty page detection, consume directory fallback
- **D-10:** Primary audience is technical self-hosters (homelab). Write for Docker/Linux users who haven't used SANE before
- **D-11:** Tutorial written for least technical user -- step-by-step with copy-paste commands
- **D-12:** Reference docs terse and complete -- no hand-holding, just facts
- **D-13:** No screenshots of web UI (go stale fast). Use text descriptions and config examples
- **D-14:** All code examples must be copy-pasteable (full commands, not fragments)
- **D-15:** Config examples use realistic values (192.168.x.x IPs, plausible paperless URLs)
- **D-16:** Each page has a clear "what you'll need" or "prerequisites" section where applicable
- **D-17:** README stays as-is (concise quick-start) with added "Documentation" section linking to full docs site
- **D-18:** README does NOT duplicate content from docs -- elevator pitch + quick start only

### Claude's Discretion
- Navigation structure and sidebar ordering
- MkDocs plugins beyond material theme
- Exact wording and content of each page
- Whether to add admonitions (tips, warnings) and where
- Footer, logo, color scheme choices for docs site

### Deferred Ideas (OUT OF SCOPE)
- API documentation auto-generation (OpenAPI/Swagger)
- Man pages for CLI commands
- Translated documentation
- Video tutorials

</user_constraints>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| mkdocs-material | 9.7.6 | MkDocs theme with search, navigation, admonitions | De facto standard for Python project docs. Includes search, dark mode, responsive layout |
| mkdocs | 1.6.1 | Static site generator from Markdown | Pulled in as dependency of mkdocs-material |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| (none needed) | -- | -- | mkdocs-material bundles all needed plugins (search, navigation, content tabs) |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| mkdocs-material | Sphinx + furo | Heavier, RST-native, overkill for Markdown-only docs |
| GitHub Pages | Read the Docs | Adds external service dependency; GitHub Pages is zero-config with Actions |

**Installation:**
```bash
uv add --dev mkdocs-material
```

This pulls in `mkdocs` and all Material dependencies automatically.

**Version verification:** `mkdocs-material` 9.7.6 confirmed via PyPI on 2026-03-22. `mkdocs` 1.6.1 confirmed via PyPI on 2026-03-22.

## Architecture Patterns

### Recommended Project Structure
```
saneless/
  mkdocs.yml                    # Site config at repo root (D-03)
  docs/
    index.md                    # Landing page (links into quadrants)
    PRD.md                      # Existing file (excluded from nav)
    tutorials/
      scan-your-first-document.md
    how-to/
      install-bare-metal.md
      deploy-docker-compose.md
      configure-scan-profiles.md
      set-up-adf-duplex.md
      scanner-host-discovery.md
      cli-scripting.md
    reference/
      cli-commands.md
      configuration.md
      environment-variables.md
      web-api.md
      docker.md
    explanation/
      architecture.md
      empty-page-detection.md
      consume-directory-fallback.md
  .github/workflows/
    docs.yml                    # New workflow for docs deployment
    release.yml                 # Existing (unchanged)
```

### Pattern 1: Diataxis Navigation Structure
**What:** Four top-level nav sections mapping directly to Diataxis quadrants
**When to use:** Always -- this is the locked framework (D-05)
**Example:**
```yaml
# mkdocs.yml
nav:
  - Home: index.md
  - Tutorials:
    - Scan Your First Document: tutorials/scan-your-first-document.md
  - How-To Guides:
    - Install on Bare Metal: how-to/install-bare-metal.md
    - Deploy with Docker Compose: how-to/deploy-docker-compose.md
    - Configure Scan Profiles: how-to/configure-scan-profiles.md
    - Set Up ADF Duplex Scanning: how-to/set-up-adf-duplex.md
    - Scanner Host Discovery (Containers): how-to/scanner-host-discovery.md
    - Use the CLI for Scripting: how-to/cli-scripting.md
  - Reference:
    - CLI Commands: reference/cli-commands.md
    - Configuration (TOML): reference/configuration.md
    - Environment Variables: reference/environment-variables.md
    - Web API: reference/web-api.md
    - Docker: reference/docker.md
  - Explanation:
    - Architecture Overview: explanation/architecture.md
    - Empty Page Detection: explanation/empty-page-detection.md
    - Consume Directory Fallback: explanation/consume-directory-fallback.md
```

### Pattern 2: MkDocs Material Configuration
**What:** Minimal `mkdocs.yml` with Material theme defaults
**When to use:** Project setup
**Example:**
```yaml
site_name: saneless
site_description: SANE scanner to paperless-ngx bridge
site_url: https://kris-knigga.github.io/saneless/
repo_url: https://github.com/kris-knigga/saneless
repo_name: kris-knigga/saneless

theme:
  name: material
  palette:
    - scheme: default
      primary: indigo
      accent: indigo
      toggle:
        icon: material/brightness-7
        name: Switch to dark mode
    - scheme: slate
      primary: indigo
      accent: indigo
      toggle:
        icon: material/brightness-4
        name: Switch to light mode
  features:
    - navigation.sections
    - navigation.expand
    - search.highlight
    - content.code.copy

markdown_extensions:
  - admonition
  - pymdownx.details
  - pymdownx.superfences
  - pymdownx.tabbed:
      alternate_style: true
  - attr_list
  - def_list
  - tables
```

### Pattern 3: GitHub Actions Docs Workflow
**What:** Separate workflow that deploys docs on push to main
**When to use:** Automated docs publishing (D-02)
**Example:**
```yaml
# .github/workflows/docs.yml
name: Docs

on:
  push:
    branches: [main]

permissions:
  contents: write

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: actions/setup-python@v5
        with:
          python-version: "3.x"
      - run: pip install mkdocs-material
      - run: mkdocs gh-deploy --force
```

### Anti-Patterns to Avoid
- **Mixing Diataxis quadrants on a single page:** Each page belongs to exactly one quadrant. Don't put tutorial steps inside a reference page or explain "why" in a how-to.
- **Duplicating README content:** README links to docs; docs link back to README. No content overlap (D-17, D-18).
- **Using docs/ for internal planning docs:** `docs/PRD.md` exists but should be excluded from the nav. Either move it or add `exclude` config.
- **Screenshots of the web UI:** Explicitly forbidden (D-13). Use text descriptions, config TOML, and CLI output examples instead.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Documentation site | Custom HTML/CSS | MkDocs + Material | Search, dark mode, mobile, versioning all included |
| Search | Custom JS search | Material built-in search | lunr.js-based, works offline, zero config |
| Code block copy buttons | Custom clipboard JS | Material `content.code.copy` feature | Single line in config |
| Dark/light mode toggle | Custom CSS variables | Material palette toggle | Built into theme |
| GitHub Pages deploy | Manual gh-pages push | GitHub Actions workflow | One-time setup, fully automated |

**Key insight:** MkDocs Material bundles everything needed for a professional documentation site. The only custom work is writing Markdown content.

## Common Pitfalls

### Pitfall 1: docs/PRD.md Appearing in Navigation
**What goes wrong:** The existing `docs/PRD.md` shows up in the rendered docs site as a page.
**Why it happens:** MkDocs auto-discovers all `.md` files in `docs/` when no explicit `nav` is set.
**How to avoid:** Always use an explicit `nav:` section in `mkdocs.yml`. The PRD.md file will be ignored by MkDocs if not listed in nav (it still exists in repo but won't appear on the site).
**Warning signs:** A "PRD" link appearing in the sidebar.

### Pitfall 2: GitHub Pages Not Configured for gh-pages Branch
**What goes wrong:** Docs deploy succeeds in Actions but site returns 404.
**Why it happens:** GitHub repository Settings > Pages needs the source branch set to `gh-pages`.
**How to avoid:** After first deploy, verify Settings > Pages > Source is set to `gh-pages` branch. The first `mkdocs gh-deploy --force` creates the branch automatically.
**Warning signs:** Green checkmark on Actions but 404 on the site URL.

### Pitfall 3: Config Examples with Placeholder Values
**What goes wrong:** Users copy config examples with `your-token-here` and wonder why things don't work.
**Why it happens:** Lazy placeholders instead of realistic examples.
**How to avoid:** Use realistic but obviously-not-real values per D-15: `192.168.1.50` for IPs, `http://paperless.local:8000` for URLs, `abc123def456` for tokens with a note to replace.
**Warning signs:** GitHub issues from users who copy-pasted without reading.

### Pitfall 4: Forgetting Exit Codes in CLI Reference
**What goes wrong:** Automation users can't reliably check scan outcomes in scripts.
**Why it happens:** Exit codes are documented in code but not in user docs.
**How to avoid:** Document exit codes for each CLI command. From the source: `sys.exit(1)` for scan errors, `sys.exit(2)` for config errors, `sys.exit(3)` for paperless errors.
**Warning signs:** Users asking "how do I check if the scan succeeded in my script?"

### Pitfall 5: Stale Documentation After Feature Changes
**What goes wrong:** Docs describe old behavior after code changes in future phases.
**Why it happens:** Docs live in separate files from code, easy to forget.
**How to avoid:** This is a known tradeoff. Keep reference docs close to source-of-truth (config.py, cli.py). Add a note at the top of reference pages with the version they describe.
**Warning signs:** Feature names or options that don't match `--help` output.

## Code Examples

### Source Material for CLI Reference (from cli.py)

Five commands with their options:

| Command | Options | Exit Codes |
|---------|---------|------------|
| `saneless` (group) | `--config PATH`, `-v/--verbose` | 2 (config error) |
| `saneless scan` | `--profile NAME`, `--title TEXT` (required) | 1 (scan error), 2 (config/profile error), 3 (paperless error) |
| `saneless devices` | `--json`, `--capabilities` | 0 (success) |
| `saneless jobs` | `--json`, `--limit N` | 0 (success) |
| `saneless serve` | `--host ADDR`, `--port N` | 1 (bind error) |
| `saneless auto-profiles` | `--force` | 1 (no scanners) |

### Source Material for Configuration Reference (from config.py)

```toml
# Full configuration schema with defaults
[scanner]
host = ""           # SANE net host IP/hostname (empty = local USB)
device = ""         # Pin specific device name (empty = auto-detect)

[paperless]
url = ""            # Paperless-ngx base URL
token = ""          # API authentication token
consume_dir = ""    # Fallback directory when API unavailable

[output]
tmp_dir = "/tmp/saneless"       # Temporary scan files
log_file = "~/.local/state/saneless/saneless.log"
log_level = "INFO"              # DEBUG, INFO, WARNING, ERROR
log_max_bytes = 10485760        # 10 MB
log_backup_count = 5
history_retention_days = 7
history_max_rows = 500
paperless_task_timeout = 300    # seconds
paperless_cache_ttl_seconds = 60
min_free_space_mb = 500
web_host = "0.0.0.0"
web_port = 8080

[profiles.default]
source = "Flatbed"
resolution = 300
mode = "color"
default_tags = []
default_correspondent = null    # Optional int
title = ""                      # Default title template
empty_page_mean_threshold = 250.0
empty_page_stddev_threshold = 5.0
enable_empty_page_detection = true
auto_generated = false
```

### Source Material for Environment Variables Reference

Pattern: `SANELESS_` prefix + `__` nested delimiter.

| Environment Variable | Config Path | Example |
|---------------------|-------------|---------|
| `SANELESS_SCANNER__HOST` | `scanner.host` | `192.168.1.50` |
| `SANELESS_SCANNER__DEVICE` | `scanner.device` | `net:192.168.1.50:pixma:MF740C` |
| `SANELESS_PAPERLESS__URL` | `paperless.url` | `http://paperless:8000` |
| `SANELESS_PAPERLESS__TOKEN` | `paperless.token` | `abc123def456` |
| `SANELESS_PAPERLESS__CONSUME_DIR` | `paperless.consume_dir` | `/consume` |
| `SANELESS_OUTPUT__TMP_DIR` | `output.tmp_dir` | `/tmp/saneless` |
| `SANELESS_OUTPUT__LOG_LEVEL` | `output.log_level` | `DEBUG` |
| `SANELESS_OUTPUT__WEB_PORT` | `output.web_port` | `8080` |

### Source Material for Web API Reference (from routes.py)

| Method | Endpoint | Purpose | Response |
|--------|----------|---------|----------|
| GET | `/` | Web UI main page | HTML |
| GET | `/health` | Health check | `{"status": "ok"}` or 503 |
| GET | `/api/paperless/test` | Test paperless connection | `{"status": "connected\|token_rejected\|unreachable"}` |
| POST | `/api/scan` | Start scan job | HTML partial (HTMX) |
| GET | `/api/jobs/current/status` | Poll current job | HTML partial (HTMX) |
| GET | `/api/tags` | Fetch tag options | HTML partial |
| GET | `/api/correspondents` | Fetch correspondent options | HTML partial |
| POST | `/api/cache/invalidate?resource=tags` | Refresh cache | HTML partial |
| GET | `/api/jobs/history` | Job history table | HTML partial |
| POST | `/api/flip/continue` | Continue manual duplex | HTML partial |
| POST | `/api/flip/abort` | Abort manual duplex | HTML partial |

### Source Material for Docker Reference (from Dockerfile + docker-compose.yml)

- Image: `ghcr.io/kris-knigga/saneless:latest`
- Base: `python:3.14-slim`
- Port: `8080`
- Healthcheck: `curl -f http://localhost:8080/health` (30s interval, 5s timeout, 3 retries)
- Entrypoint: `saneless serve`
- Config mount: `./config.toml:/etc/saneless/config.toml:ro`
- Data volume: `/tmp/saneless` for scan temp files and SQLite DB

### Config Search Paths (from config.py)

1. `--config PATH` (explicit)
2. `./saneless.toml` (current directory)
3. `~/.config/saneless/config.toml` (XDG config)
4. `/etc/saneless/config.toml` (system-wide, used in Docker)

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Sphinx + RST | MkDocs + Material | ~2020 onwards | Python ecosystem shifted to Markdown-native docs |
| Manual gh-pages push | GitHub Actions `mkdocs gh-deploy` | ~2022 | Automated, no local deploy needed |
| Single flat docs page | Diataxis four-quadrant structure | ~2021 (Procida framework) | Industry standard for separating learning/doing/looking-up/understanding |

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | Playwright (browser) + manual `mkdocs build` |
| Config file | `mkdocs.yml` (new) |
| Quick run command | `uv run mkdocs build --strict` |
| Full suite command | `uv run mkdocs build --strict` (catches broken links, missing files) |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| DOC-01 | mkdocs.yml valid and site builds | build | `uv run mkdocs build --strict` | N/A (Wave 0) |
| DOC-02 | All 15 doc pages render | build | `uv run mkdocs build --strict` (fails on broken nav refs) | N/A (Wave 0) |
| DOC-03 | GitHub Actions workflow syntactically valid | lint | `yamllint .github/workflows/docs.yml` or manual review | N/A (Wave 0) |
| DOC-04 | No broken internal links | build | `uv run mkdocs build --strict` (strict mode catches warnings as errors) | N/A (Wave 0) |
| DOC-05 | README has Documentation section | grep | `grep -q "Documentation" README.md` | Exists (modify) |

### Sampling Rate
- **Per task commit:** `uv run mkdocs build --strict`
- **Per wave merge:** `uv run mkdocs build --strict` + local `uv run mkdocs serve` preview
- **Phase gate:** Full build green + README link present

### Wave 0 Gaps
- [ ] `mkdocs.yml` -- must be created as part of first plan
- [ ] `docs/index.md` -- landing page
- [ ] `mkdocs-material` dev dependency -- `uv add --dev mkdocs-material`

## Open Questions

1. **docs/PRD.md coexistence**
   - What we know: `docs/PRD.md` exists and is an internal planning document
   - What's unclear: Whether it should stay in `docs/` or move elsewhere
   - Recommendation: Leave it in place. With explicit `nav:` in mkdocs.yml, it won't appear in the published site. No file moves needed.

2. **GitHub Pages domain**
   - What we know: Site will be at `https://kris-knigga.github.io/saneless/`
   - What's unclear: Whether a custom domain is desired
   - Recommendation: Use default GitHub Pages URL. Custom domain can be added later.

3. **Multiple scanner host syntax**
   - What we know: `SANELESS_SCANNER__HOST` supports colon-separated hosts (from docker-compose.yml comments)
   - What's unclear: How the colon separator interacts with IPv6 addresses
   - Recommendation: Document colon-separated format with a note that this follows SANE_NET_HOSTS conventions. IPv6 is a SANE upstream concern.

## Sources

### Primary (HIGH confidence)
- Project source code: `src/saneless/cli.py`, `src/saneless/config.py`, `src/saneless/web/routes.py`, `src/saneless/pages.py` -- authoritative for all reference content
- `pyproject.toml` -- dev dependency configuration
- `Dockerfile` + `docker-compose.yml` -- Docker reference source of truth
- `README.md` -- current documentation baseline

### Secondary (MEDIUM confidence)
- [Material for MkDocs - Publishing your site](https://squidfunk.github.io/mkdocs-material/publishing-your-site/) -- GitHub Actions workflow pattern
- [MkDocs Deploying Your Docs](https://www.mkdocs.org/user-guide/deploying-your-docs/) -- gh-pages deployment
- [Diataxis Framework](https://diataxis.fr/) -- Quadrant definitions and best practices
- [Diataxis - Start Here](https://diataxis.fr/start-here/) -- Quick overview of the four types

### Tertiary (LOW confidence)
- None -- all findings verified against primary sources or official documentation

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- mkdocs-material 9.7.6 verified on PyPI, well-known Python docs stack
- Architecture: HIGH -- Diataxis quadrants clearly defined in CONTEXT.md, MkDocs patterns well-established
- Pitfalls: HIGH -- derived from direct source code review and MkDocs documentation
- Content accuracy: HIGH -- all reference material extracted directly from project source code

**Research date:** 2026-03-22
**Valid until:** 2026-04-22 (stable -- documentation tooling changes slowly)
