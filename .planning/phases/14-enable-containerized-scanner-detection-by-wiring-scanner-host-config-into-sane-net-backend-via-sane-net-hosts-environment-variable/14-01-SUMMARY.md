---
phase: 14-enable-containerized-scanner-detection
plan: 01
subsystem: scanner
tags: [sane, sane-net, docker, environment-variables, network-scanner]

# Dependency graph
requires:
  - phase: 01-core-pipeline
    provides: SaneBackend, ScannerConfig with host field, CLI commands
provides:
  - SANE_NET_HOSTS env var injection from settings.scanner.host
  - Network scanner discovery for containerized deployments
  - Docker Compose documentation for scanner host configuration
affects: []

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Environment variable injection before library init (set os.environ before sane.init())"
    - "Config-vs-env priority: explicit env var overrides config file value"

key-files:
  created: []
  modified:
    - src/saneless/scanner/sane_backend.py
    - src/saneless/cli.py
    - tests/test_scanner.py
    - tests/test_cli.py
    - docker-compose.yml

key-decisions:
  - "SANE_NET_HOSTS set before sane.init() -- env var must exist before SANE probes backends"
  - "Externally-set SANE_NET_HOSTS takes priority over config file scanner.host value"
  - "Colon delimiter for multi-host values matches sane-net(5) specification"

patterns-established:
  - "Env-before-init: set environment variables before library initialization calls"

requirements-completed: [NET-01, NET-02, NET-03, NET-04]

# Metrics
duration: 3min
completed: 2026-03-22
---

# Phase 14 Plan 01: SANE Net Host Wiring Summary

**SaneBackend injects SANE_NET_HOSTS from scanner.host config before sane.init() for containerized network scanner discovery**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-22T19:39:42Z
- **Completed:** 2026-03-22T19:43:37Z
- **Tasks:** 2
- **Files modified:** 5

## Accomplishments
- SaneBackend accepts `host` parameter and sets SANE_NET_HOSTS env var before sane.init()
- External SANE_NET_HOSTS takes priority over config value (no override)
- All 4 CLI commands (scan, devices, serve, auto-profiles) pass settings.scanner.host
- docker-compose.yml documents SANELESS_SCANNER__HOST env var with multi-host example

## Task Commits

Each task was committed atomically:

1. **Task 1: Add host parameter to SaneBackend and set SANE_NET_HOSTS** - `f4e4557` (feat, TDD)
2. **Task 2: Wire CLI call sites and update docker-compose.yml** - `9867d34` (feat)

## Files Created/Modified
- `src/saneless/scanner/sane_backend.py` - Added host param, SANE_NET_HOSTS injection before sane.init()
- `src/saneless/cli.py` - All 4 SaneBackend() calls now pass host=settings.scanner.host
- `tests/test_scanner.py` - 4 new tests for env var set/no-override/empty/multi-host
- `tests/test_cli.py` - Updated mock scanner classes to accept host parameter
- `docker-compose.yml` - Added commented SANELESS_SCANNER__HOST example

## Decisions Made
- SANE_NET_HOSTS set before sane.init() because SANE probes backends during initialization
- Externally-set SANE_NET_HOSTS not overridden -- allows explicit env var to take priority over config
- Colon delimiter for multi-host values per sane-net(5) manpage specification

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed CLI test mock scanners missing host parameter**
- **Found during:** Task 2
- **Issue:** FailScanner, _AutoScanner, LongNameScanner, and MockSaneBackend test classes in test_cli.py lacked host= parameter, causing TypeError
- **Fix:** Added `__init__(self, host: str = "") -> None` to all mock scanner classes
- **Files modified:** tests/test_cli.py
- **Verification:** Full test suite passes (296 tests)
- **Committed in:** 9867d34 (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (1 bug)
**Impact on plan:** Necessary fix for test compatibility with new API. No scope creep.

## Issues Encountered
None

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- Phase 14 complete -- all network scanner discovery wiring in place
- Users can now set `scanner.host` in config or `SANELESS_SCANNER__HOST` env var for containerized deployments

## Self-Check: PASSED

- All key files exist
- Both task commits verified (f4e4557, 9867d34)
- host parameter present in SaneBackend.__init__
- SANE_NET_HOSTS injection code present
- sane-net(5) comment present
- All 4 CLI call sites pass host parameter

---
*Phase: 14-enable-containerized-scanner-detection*
*Completed: 2026-03-22*
