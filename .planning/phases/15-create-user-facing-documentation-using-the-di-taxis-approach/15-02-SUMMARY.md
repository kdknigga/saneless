---
phase: 15-create-user-facing-documentation-using-the-di-taxis-approach
plan: 02
subsystem: docs
tags: [mkdocs, diataxis, how-to, sane, docker, cli]

requires:
  - phase: 15-create-user-facing-documentation-using-the-di-taxis-approach
    provides: MkDocs Material site scaffold with stub pages (Plan 01)
provides:
  - Six complete how-to guides covering install, Docker, profiles, ADF duplex, scanner discovery, and CLI scripting
affects: [15-create-user-facing-documentation-using-the-di-taxis-approach]

tech-stack:
  added: []
  patterns: [diataxis how-to format with prerequisites and copy-paste commands]

key-files:
  created:
    - docs/how-to/install-bare-metal.md
    - docs/how-to/deploy-docker-compose.md
    - docs/how-to/configure-scan-profiles.md
    - docs/how-to/set-up-adf-duplex.md
    - docs/how-to/scanner-host-discovery.md
    - docs/how-to/cli-scripting.md
  modified: []

key-decisions:
  - "Used tabbed content (=== syntax) for pipx vs pip install options"
  - "Showed combined saneless + paperless-ngx Docker Compose for realistic homelab deployment"
  - "Documented all three duplex modes with per-mode profile config and CLI examples"

patterns-established:
  - "How-to guide structure: title, prerequisites, numbered steps, troubleshooting"

requirements-completed: [D-07, D-10, D-13, D-14, D-15, D-16]

duration: 3min
completed: 2026-03-22
---

# Phase 15 Plan 02: How-To Guides Summary

**Six Diataxis how-to guides with step-by-step install, Docker Compose deployment, scan profiles, ADF duplex modes, container scanner discovery, and CLI scripting automation**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-22T20:30:28Z
- **Completed:** 2026-03-22T20:33:59Z
- **Tasks:** 2
- **Files modified:** 6

## Accomplishments

- Bare metal install guide with distro-specific SANE headers table and pipx/pip options
- Docker Compose deployment guide showing combined saneless + paperless-ngx services
- Scan profile configuration guide with complete field reference table and auto-profiles
- ADF duplex guide covering simplex, hardware duplex, and manual duplex two-pass flow
- Scanner host discovery guide explaining SANE_NET_HOSTS env var injection for containers
- CLI scripting guide with JSON output examples, exit code table, and cron scheduling

## Task Commits

Each task was committed atomically:

1. **Task 1: Write install, Docker Compose, and scan profiles how-to guides** - `4f25906` (feat)
2. **Task 2: Write ADF duplex, scanner host discovery, and CLI scripting how-to guides** - `3752ff8` (feat)

## Files Created/Modified

- `docs/how-to/install-bare-metal.md` - Bare metal installation with SANE headers, pipx, troubleshooting
- `docs/how-to/deploy-docker-compose.md` - Docker Compose with combined saneless + paperless-ngx services
- `docs/how-to/configure-scan-profiles.md` - Profile field reference, auto-profiles, empty page tuning
- `docs/how-to/set-up-adf-duplex.md` - Three ADF modes with profile config and manual duplex flow
- `docs/how-to/scanner-host-discovery.md` - SANE_NET_HOSTS wiring, multiple hosts, priority rules
- `docs/how-to/cli-scripting.md` - JSON output shapes, exit codes, scripting examples, cron

## Decisions Made

- Used Material for MkDocs tabbed content blocks for pipx vs pip install alternatives
- Combined saneless + paperless-ngx Docker Compose example for realistic homelab deployment (per D-10)
- Documented page count mismatch recovery in manual duplex (graceful degradation from pipeline.py)
- Showed both config.toml and env var methods for scanner host discovery

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- All six how-to guide stubs replaced with substantive content
- Cross-links between guides in place (install -> profiles, Docker -> scanner discovery, etc.)
- Ready for Plan 03 (reference pages) and Plan 04 (explanation pages)

---
*Phase: 15-create-user-facing-documentation-using-the-di-taxis-approach*
*Completed: 2026-03-22*
