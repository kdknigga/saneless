# Phase 4: Packaging and Deployment - Research

**Researched:** 2026-03-20
**Domain:** Python packaging (PyPI), OCI containers (Docker/GHCR), CI/CD (GitHub Actions)
**Confidence:** HIGH

## Summary

Phase 4 takes an already-functional Python application and wraps it for distribution via two channels: a pip-installable package on PyPI and an OCI container image on GHCR. The codebase already uses `uv_build` as its build backend and has a working `[project.scripts]` entry point, so the packaging foundation exists. The remaining work is: (1) polish `pyproject.toml` metadata for PyPI, (2) write a two-stage Dockerfile with HEALTHCHECK, (3) create a GitHub Actions release workflow that builds and publishes both artifacts on tag push, (4) implement the `saneless jobs` CLI command following the established Click pattern, and (5) complete consume directory fallback with startup validation.

The ecosystem is mature and well-documented. `uv build` + `uv publish` with trusted publishing is the modern standard, and Docker multi-stage builds with `python:3.14-slim` are straightforward. No novel technical challenges exist in this phase.

**Primary recommendation:** Use `uv build` + `uv publish` for PyPI (trusted publishing via OIDC), `docker/build-push-action` for GHCR, and a single GitHub Actions workflow file with separate jobs triggered on `v*` tag push.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- Two-stage Dockerfile: build stage installs uv and builds wheel, runtime stage copies wheel and installs with pip
- Runtime base image: `python:3.14-slim`
- System packages in runtime: `libsane` only
- HEALTHCHECK: `curl`-based, `--interval=30s --timeout=5s --retries=3` pinging `GET /health`
- No `--privileged` required -- USB device access handled by external `saned` server
- No hardcoded credentials -- API token via config file or environment variable
- Expose port 8000 (uvicorn default)
- Config injected via volume mount (`/etc/saneless/config.toml`) or environment variables (SANELESS_ prefix)
- Consume directory as optional volume mount
- GitHub Actions workflow triggered on tag push (`v*`)
- PyPI publishing via trusted publisher (GitHub Actions OIDC) -- no API tokens needed
- GHCR image tagged with version + `latest`
- Docker Compose example included in repo
- CI runs linting, type checking, and tests before publishing
- `saneless jobs`: human-readable table by default with timestamp, profile, title, status columns
- `--json` flag for programmatic output (JSON array)
- `--limit N` flag to control row count, default 20 most recent
- Uses existing `JobStore.list_recent()` -- no new persistence needed
- Exit code 0 always (informational command)
- Consume directory: startup validation (warn if configured dir doesn't exist), create on first use
- Integration testing to verify fallback triggers correctly

### Claude's Discretion
- Exact Dockerfile layer ordering and caching optimization
- GitHub Actions workflow YAML structure
- Docker Compose example details (network mode, restart policy, volume paths)
- Package metadata (classifiers, long description, project URLs)
- Whether to include `curl` or `wget` in container for HEALTHCHECK (curl preferred)
- README structure and install guide content

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| PKG-01 | pip-installable Python package (pyproject.toml, published to PyPI) | `uv_build` backend already configured; needs metadata polish + `uv publish` workflow |
| PKG-02 | OCI container image published to GHCR with HEALTHCHECK instruction | Two-stage Dockerfile with `python:3.14-slim`, `docker/build-push-action` in GHA |
| PKG-03 | Container does not require --privileged; USB handled by saned server | Architecture already supports this -- saned is external, no USB passthrough needed |
| PLSS-06 | System supports a consume directory fallback as a config option | Core logic exists in `PaperlessClient.upload_document()`; needs startup validation + create-on-first-use |
| CLI-03 | `saneless jobs` lists recent job history | New Click command using `JobStore.list_recent()`, follows `devices` command pattern |
</phase_requirements>

## Standard Stack

### Core (already in project)
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| uv_build | >=0.10.3,<0.11.0 | Build backend | Already configured; produces standard wheels |
| click | >=8.3.1 | CLI framework | Already used for `scan` and `devices` commands |
| pydantic-settings | >=2.13.1 | Config with env vars | Already used; SANELESS_ prefix + TOML loading |

### CI/CD Actions
| Action | Version | Purpose | When to Use |
|--------|---------|---------|-------------|
| astral-sh/setup-uv | v7 | Install uv in GHA | Every CI job |
| pypa/gh-action-pypi-publish | v1.12 | Publish to PyPI via OIDC | Release workflow PyPI job |
| docker/login-action | v3 | Login to GHCR | Release workflow Docker job |
| docker/metadata-action | v5 | Generate Docker tags/labels | Release workflow Docker job |
| docker/build-push-action | v6 | Build and push Docker image | Release workflow Docker job |
| actions/checkout | v5 | Checkout code | Every workflow |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| pypa/gh-action-pypi-publish | uv publish directly | `uv publish` works but pypa action handles attestations automatically; either works with trusted publishing |
| curl in HEALTHCHECK | wget or python script | curl is more common, but adds ~5MB to image; python healthcheck avoids extra package but is heavier to invoke |

**Note on HEALTHCHECK curl vs wget:** The `python:3.14-slim` image does not include curl. Install `curl` in the Dockerfile runtime stage (`apt-get install -y --no-install-recommends curl`). This adds minimal size (~5MB) and is the standard approach for container health checks.

## Architecture Patterns

### Recommended Project Structure (new files)
```
.
├── Dockerfile                  # Two-stage build
├── docker-compose.yml          # Quick deployment example
├── .github/
│   └── workflows/
│       └── release.yml         # Tag-triggered publish workflow
├── src/saneless/
│   └── cli.py                  # Add `jobs` command here
└── tests/
    └── test_cli.py             # Add `jobs` command tests here
```

### Pattern 1: Two-Stage Dockerfile
**What:** Build stage creates wheel with uv, runtime stage installs from wheel with pip
**When to use:** Always for this project -- separates build tools from runtime
**Example:**
```dockerfile
# Stage 1: Build
FROM python:3.14-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY . .
RUN uv build --wheel --out-dir /dist

# Stage 2: Runtime
FROM python:3.14-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    libsane curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1
ENTRYPOINT ["saneless"]
```

### Pattern 2: GitHub Actions Release Workflow
**What:** Single workflow file with three sequential jobs: test, publish-pypi, publish-docker
**When to use:** On tag push matching `v*`
**Example structure:**
```yaml
name: Release
on:
  push:
    tags: ["v*"]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      - uses: astral-sh/setup-uv@v7
      - run: uv sync
      - run: uv run ruff check .
      - run: uv run ruff format --check .
      - run: uv run pytest

  publish-pypi:
    needs: test
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write
    steps:
      - uses: actions/checkout@v5
      - uses: astral-sh/setup-uv@v7
      - run: uv build
      - uses: pypa/gh-action-pypi-publish@v1.12

  publish-docker:
    needs: test
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v5
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/metadata-action@v5
        id: meta
        with:
          images: ghcr.io/${{ github.repository }}
          tags: |
            type=semver,pattern={{version}}
            type=raw,value=latest
      - uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
```

### Pattern 3: CLI `jobs` Command (follows `devices` pattern)
**What:** Click command with `--json` and `--limit` flags, table output by default
**When to use:** Implementing CLI-03
**Example:**
```python
@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--limit", default=20, help="Maximum jobs to show.")
@click.pass_context
def jobs(ctx: click.Context, *, as_json: bool, limit: int) -> None:
    """List recent scan job history."""
    settings = ctx.obj["settings"]
    store = JobStore(db_path=str(Path(settings.output.tmp_dir) / "jobs.db"))
    try:
        recent = store.list_recent(limit=limit)
        if as_json:
            data = [{"id": j.id, "profile": j.profile, "title": j.title,
                      "state": j.state.value, "created_at": j.created_at.isoformat()} for j in recent]
            click.echo(json.dumps(data, indent=2))
        else:
            # Table output
            ...
    finally:
        store.close()
```

### Anti-Patterns to Avoid
- **Running uv in the runtime container:** uv is a build tool only; the runtime stage should use pip to install the wheel
- **Installing dev dependencies in Docker:** Only production deps should be in the image
- **Using `COPY . .` after dependency install:** This busts the Docker layer cache on every code change; however, for this project the wheel is self-contained so layer ordering is less critical
- **Hardcoding version in Dockerfile tags:** Use `docker/metadata-action` to derive tags from git tags

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| PyPI publishing auth | Manual token management | Trusted publisher OIDC | Zero secrets to manage, more secure |
| Docker tag management | Manual tag strings | docker/metadata-action | Handles semver extraction, `latest` tag, OCI labels |
| Build artifact creation | Custom build scripts | `uv build` | Standard PEP 517 wheel building |
| CI test matrix | Ad-hoc shell scripts | GitHub Actions jobs with `needs:` | Built-in dependency, parallelism, failure isolation |

**Key insight:** The entire release pipeline uses standard, well-maintained actions. No custom scripting needed beyond the Dockerfile itself.

## Common Pitfalls

### Pitfall 1: Missing `id-token: write` Permission
**What goes wrong:** PyPI trusted publishing silently fails with unhelpful error
**Why it happens:** The permission must be set at job level, not workflow level, for security
**How to avoid:** Always set `permissions: id-token: write` on the publish-pypi job
**Warning signs:** "Token exchange failed" errors in the publish step

### Pitfall 2: curl Not in python:3.14-slim
**What goes wrong:** HEALTHCHECK fails because curl is not installed
**Why it happens:** Slim images strip non-essential packages
**How to avoid:** Explicitly `apt-get install curl` in the runtime stage
**Warning signs:** Container starts but Docker reports unhealthy

### Pitfall 3: PyPI Project Name Collision
**What goes wrong:** `uv publish` fails because "saneless" is already taken on PyPI
**Why it happens:** PyPI names are globally unique
**How to avoid:** Check `pip index versions saneless` or visit pypi.org/project/saneless before first publish
**Warning signs:** 403 or "project name already exists" error from PyPI

### Pitfall 4: Uvicorn Port Mismatch
**What goes wrong:** HEALTHCHECK pings port 8000 but uvicorn binds to port 8080
**Why it happens:** Config defaults `web_port = 8080` but CONTEXT.md says expose 8000
**How to avoid:** Check `OutputConfig.web_port` -- it defaults to 8080. Either change the default or use 8080 in the Dockerfile EXPOSE and HEALTHCHECK
**Warning signs:** Container starts, HEALTHCHECK fails, health endpoint works on different port

### Pitfall 5: JobStore DB Path Mismatch Between CLI and Web
**What goes wrong:** `saneless jobs` shows empty results even though web UI shows history
**Why it happens:** CLI creates a new JobStore with a different db_path than the web layer
**How to avoid:** Both must resolve to the same SQLite file path; use config-driven db_path
**Warning signs:** Empty job list in CLI while web shows jobs

### Pitfall 6: pyproject.toml Missing Metadata for PyPI
**What goes wrong:** PyPI rejects upload or shows incomplete project page
**Why it happens:** Current pyproject.toml has placeholder description, no classifiers, no license
**How to avoid:** Fill in description, license, classifiers, project-urls, keywords before first publish
**Warning signs:** Warnings from `uv build` or twine check

## Code Examples

### Existing CLI Pattern for Reference
```python
# Source: src/saneless/cli.py (devices command)
@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def devices(ctx: click.Context, *, as_json: bool) -> None:
    """List available scanning devices."""
    # ... table or JSON output based on flag
```

### Consume Directory Startup Validation
```python
# Add to config validation or app startup
from pathlib import Path
import logging
import warnings

def validate_consume_dir(consume_dir: str) -> None:
    """Warn if consume directory is configured but doesn't exist; create on first use."""
    if not consume_dir:
        return
    path = Path(consume_dir)
    if not path.exists():
        logging.getLogger(__name__).warning(
            "Consume directory %s does not exist; will create on first use", path
        )
```

### Docker Compose Example
```yaml
# docker-compose.yml
services:
  saneless:
    image: ghcr.io/owner/saneless:latest
    ports:
      - "8000:8080"
    volumes:
      - ./config.toml:/etc/saneless/config.toml:ro
      - ./consume:/consume
    environment:
      - SANELESS_PAPERLESS__URL=http://paperless:8000
      - SANELESS_PAPERLESS__TOKEN=your-token-here
    restart: unless-stopped
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| setuptools + twine | uv_build + uv publish | 2024-2025 | Faster builds, no twine dependency |
| PyPI API tokens in secrets | Trusted publisher OIDC | 2023 | No secrets management needed |
| Manual Docker tag strings | docker/metadata-action | 2023 | Automatic semver + latest tagging |
| requirements.txt in Docker | Wheel install in multi-stage | 2024 | Reproducible, faster, no source in image |

**Deprecated/outdated:**
- `setup.py` / `setup.cfg`: replaced by `pyproject.toml` (this project already uses pyproject.toml)
- PyPI username/password auth: removed entirely from PyPI
- `docker build` without BuildKit: BuildKit is default since Docker 23.0

## Open Questions

1. **PyPI project name availability**
   - What we know: The package is named "saneless" in pyproject.toml
   - What's unclear: Whether "saneless" is available on PyPI
   - Recommendation: Check before first publish; if taken, consider "saneless-scanner" or similar

2. **Uvicorn port: 8000 vs 8080**
   - What we know: CONTEXT.md says "Expose port 8000", but `OutputConfig.web_port` defaults to 8080
   - What's unclear: Whether the user intends to change the default or adjust Docker config
   - Recommendation: Use the existing default (8080) in the Dockerfile EXPOSE and HEALTHCHECK for consistency; document that the port is configurable

3. **JobStore database path for CLI**
   - What we know: Web layer creates JobStore during app lifespan; CLI needs access to same DB
   - What's unclear: Where the DB file is stored -- it may be in tmp_dir or a dedicated path
   - Recommendation: Check the web app lifespan to determine the DB path, ensure CLI uses the same resolution logic

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest >=9.0.2 |
| Config file | pyproject.toml `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/test_cli.py -x` |
| Full suite command | `uv run pytest` |

### Phase Requirements to Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| PKG-01 | Package builds and installs | smoke | `uv build && uv run --with dist/*.whl --no-project -- python -c "import saneless"` | No -- Wave 0 |
| PKG-02 | Dockerfile builds successfully | smoke | `docker build -t saneless-test .` | No -- Wave 0 |
| PKG-03 | Container runs without --privileged | smoke | `docker run --rm saneless-test --help` | No -- Wave 0 |
| PLSS-06 | Consume dir fallback works | unit | `uv run pytest tests/test_paperless.py -x -k consume` | Partial (upload fallback tested) |
| CLI-03 | `saneless jobs` shows history | unit | `uv run pytest tests/test_cli.py -x -k jobs` | No -- Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_cli.py tests/test_paperless.py -x`
- **Per wave merge:** `uv run pytest && uv run ruff check . && uv run ruff format --check .`
- **Phase gate:** Full suite green + `uv build` succeeds + `docker build` succeeds

### Wave 0 Gaps
- [ ] `tests/test_cli.py::TestJobsCommand` -- covers CLI-03 (jobs table, --json, --limit)
- [ ] Consume dir startup validation test -- covers PLSS-06 completion
- [ ] Dockerfile build smoke test (manual/CI only, not pytest)
- [ ] `uv build` smoke test (manual/CI only, not pytest)

## Sources

### Primary (HIGH confidence)
- [PyPI Trusted Publishers docs](https://docs.pypi.org/trusted-publishers/using-a-publisher/) - OIDC workflow requirements
- [uv build backend docs](https://docs.astral.sh/uv/concepts/build-backend/) - Build configuration
- [uv GitHub integration guide](https://docs.astral.sh/uv/guides/integration/github/) - setup-uv action, publish workflow
- [uv package guide](https://docs.astral.sh/uv/guides/package/) - Build and publish commands
- [GitHub Docs: Publishing Docker images](https://docs.github.com/en/actions/publishing-packages/publishing-docker-images) - GHCR workflow pattern
- [Docker Hub python:3.14-slim](https://hub.docker.com/layers/library/python/3.14-slim/) - Base image availability confirmed

### Secondary (MEDIUM confidence)
- [pypa/gh-action-pypi-publish releases](https://github.com/pypa/gh-action-pypi-publish/releases) - v1.12.4 latest as of Jan 2026
- [astral-sh/setup-uv](https://github.com/astral-sh/setup-uv) - v7.6.0 latest (verified via GitHub API)
- [astral-sh/trusted-publishing-examples](https://github.com/astral-sh/trusted-publishing-examples) - Official example repo

### Tertiary (LOW confidence)
- None

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - all tools verified via official docs, versions confirmed
- Architecture: HIGH - patterns are well-established, codebase patterns already exist to follow
- Pitfalls: HIGH - based on direct code inspection (port mismatch, missing curl, DB path)

**Research date:** 2026-03-20
**Valid until:** 2026-04-20 (stable ecosystem, 30-day validity)
