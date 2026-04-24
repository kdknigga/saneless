---
phase: 04-packaging-and-deployment
plan: 02
subsystem: infra
tags: [docker, pypi, github-actions, ci-cd, oidc, ghcr]

# Dependency graph
requires:
  - phase: 03-web-ui
    provides: /health endpoint for HEALTHCHECK
provides:
  - Complete PyPI metadata for pip install
  - Two-stage Dockerfile with HEALTHCHECK
  - Docker Compose deployment example
  - GitHub Actions release workflow (test + PyPI + GHCR)
affects: []

# Tech tracking
tech-stack:
  added: [docker, github-actions, pypa/gh-action-pypi-publish, docker/build-push-action]
  patterns: [two-stage-dockerfile, trusted-publishing-oidc, tag-triggered-release]

key-files:
  created:
    - Dockerfile
    - docker-compose.yml
    - .github/workflows/release.yml
  modified:
    - pyproject.toml

key-decisions:
  - "Port 8080 in Dockerfile matches OutputConfig.web_port default (not 8000 from CONTEXT.md)"
  - "PyPI trusted publishing via OIDC -- no API tokens needed"
  - "curl installed in runtime stage for HEALTHCHECK (not wget or python script)"

patterns-established:
  - "Two-stage Dockerfile: uv build in builder, pip install wheel in runtime"
  - "Tag-triggered release: v* tag push runs test then publishes to PyPI and GHCR in parallel"

requirements-completed: [PKG-01, PKG-02, PKG-03]

# Metrics
duration: 2min
completed: 2026-03-20
---

# Phase 04 Plan 02: Packaging and Deployment Infrastructure Summary

**PyPI metadata, two-stage Dockerfile with HEALTHCHECK, Docker Compose example, and tag-triggered GitHub Actions release workflow publishing to PyPI (OIDC) and GHCR**

## Performance

- **Duration:** 2 min
- **Started:** 2026-03-20T21:46:15Z
- **Completed:** 2026-03-20T21:48:08Z
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments
- Polished pyproject.toml with description, license, classifiers, keywords, and project URLs
- Two-stage Dockerfile: uv builds wheel in builder stage, runtime stage installs with pip + libsane + curl for HEALTHCHECK
- Docker Compose example with config volume mount, env var overrides, and named data volume
- GitHub Actions release workflow with test gate, PyPI OIDC publishing, and GHCR container publishing

## Task Commits

Each task was committed atomically:

1. **Task 1: Polish pyproject.toml metadata and create Dockerfile + docker-compose.yml** - `9d31c87` (feat)
2. **Task 2: Create GitHub Actions release workflow** - `4bd8bf8` (feat)

## Files Created/Modified
- `pyproject.toml` - Added description, license, classifiers, keywords, project URLs
- `Dockerfile` - Two-stage build with HEALTHCHECK on port 8080
- `docker-compose.yml` - Deployment example with config and data volumes
- `.github/workflows/release.yml` - Tag-triggered release with test, publish-pypi, publish-docker jobs

## Decisions Made
- Used port 8080 (matching OutputConfig.web_port default) instead of 8000 from CONTEXT.md, per RESEARCH.md Pitfall 4
- Installed curl in runtime image for HEALTHCHECK (standard approach, ~5MB addition)
- Omitted ty and pyrefly from CI test job (require Python 3.14 which may not be available on ubuntu-latest runners)

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- All packaging infrastructure in place
- First publish requires: PyPI trusted publisher configuration on pypi.org, GitHub environment named "pypi"
- Container publishing works automatically via GITHUB_TOKEN

## Self-Check: PASSED

- All 4 created/modified files verified present on disk
- Both task commits verified in git log (9d31c87, 4bd8bf8)

---
*Phase: 04-packaging-and-deployment*
*Completed: 2026-03-20*
