---
status: complete
phase: 04-packaging-and-deployment
source: 04-01-SUMMARY.md, 04-02-SUMMARY.md
started: 2026-03-21T00:00:00Z
updated: 2026-03-21T00:10:00Z
---

## Current Test

[testing complete]

## Tests

### 1. Cold Start Smoke Test
expected: Kill any running saneless process. Run `uv run saneless` from scratch. The application boots without errors. Web UI is accessible at the configured port.
result: pass

### 2. Jobs Command — Table Output
expected: Run `uv run saneless jobs`. Output shows a table with columns: Timestamp, Profile, Title, Status. If no jobs exist yet, the table is empty but headers still display (or a "no jobs" message).
result: pass

### 3. Jobs Command — JSON Output
expected: Run `uv run saneless jobs --json`. Output is a valid JSON array. Each element has timestamp, profile, title, and status fields.
result: pass

### 4. Jobs Command — Limit Flag
expected: Run `uv run saneless jobs --limit 1`. Output shows at most 1 job entry (or none if no jobs exist). No errors from the flag.
result: pass

### 5. Consume Directory Fallback
expected: Configure a consume directory path (e.g., `/tmp/saneless-consume`). With paperless-ngx unreachable, scan a document. The PDF is deposited into the consume directory. Directory is auto-created if it didn't exist. A warning log is emitted.
result: pass

### 6. PyPI Metadata
expected: Run `uv run python -c "import importlib.metadata; m = importlib.metadata.metadata('saneless'); print(m['Summary'], m['License'])"`. Shows a description and license. `pip install saneless` would find a well-formed package (classifiers, URLs present in pyproject.toml).
result: pass

### 7. Dockerfile Builds
expected: Run `docker build -t saneless:test .`. Build completes without errors. The image uses a two-stage build (builder + runtime). Run `docker run --rm saneless:test saneless --help` and see CLI help output.
result: pass

### 8. Docker HEALTHCHECK
expected: Inspect the Dockerfile. It contains a HEALTHCHECK instruction that curls the /health endpoint on port 8080. Run the container and verify `docker inspect` shows health status transitioning to healthy (or at least the healthcheck command is configured).
result: pass

### 9. Docker Compose Configuration
expected: Review `docker-compose.yml`. It defines the saneless service with config volume mount, environment variable overrides, and a named data volume. `docker compose config` validates without errors.
result: pass

### 10. GitHub Actions Release Workflow
expected: Review `.github/workflows/release.yml`. It triggers on `v*` tag push. It has jobs for: test gate, PyPI publishing (OIDC trusted publishing), and GHCR container publishing. The workflow structure is valid YAML.
result: pass

## Summary

total: 10
passed: 10
issues: 0
pending: 0
skipped: 0

## Gaps

[none yet]
