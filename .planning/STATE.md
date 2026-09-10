---
gsd_state_version: 1.0
milestone: v2.0
milestone_name: Prep for release
status: executing
stopped_at: Phase 22 context gathered
last_updated: "2026-09-10T23:28:54.858Z"
last_activity: 2026-09-10
progress:
  total_phases: 13
  completed_phases: 2
  total_plans: 16
  completed_plans: 15
  percent: 15
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-21)

**Core value:** A user can walk up to the web UI, click Scan, and have a correctly assembled PDF land in paperless-ngx with metadata -- without touching any other tool.
**Current focus:** Phase 22 — job-store-hardening
**Roadmap:** .planning/ROADMAP.md (13 phases, Phase 20 through Phase 32)

## Current Position

Phase: 22 (job-store-hardening) — EXECUTING
Plan: 6 of 6
Status: Ready to execute
Progress: [██████████] 100%
Last activity: 2026-09-10

## Performance Metrics

**Velocity:**

- Total plans completed: 8
- Average duration: 5min
- Total execution time: 0.27 hours

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| 01-core-pipeline | 3 | 16min | 5min |
| 20 | 5 | - | - |

**Recent Trend:**

- Last 5 plans: 01-01 (5min), 01-02 (5min), 01-03 (6min)
- Trend: stable

*Updated after each plan completion*
| Phase 01 P02 | 5min | 2 tasks | 9 files |
| Phase 01 P03 | 6min | 2 tasks | 9 files |
| Phase 02 P01 | 5min | 2 tasks | 9 files |
| Phase 02 P02 | 8min | 1 tasks | 2 files |
| Phase 02 P03 | 7min | 2 tasks | 6 files |
| Phase 03 P00 | 2min | 1 tasks | 5 files |
| Phase 03-web-ui P01 | 8min | 2 tasks | 17 files |
| Phase 03-web-ui P02 | 2min | 2 tasks | 8 files |
| Phase 03-web-ui P03 | 5min | 2 tasks | 3 files |
| Phase 04 P02 | 2min | 2 tasks | 4 files |
| Phase 04-packaging-and-deployment P01 | 3min | 2 tasks | 4 files |
| Phase 05 P01 | 3min | 2 tasks | 4 files |
| Phase 06 P01 | 2min | 1 tasks | 4 files |
| Phase 07-tech-debt-cleanup P01 | 7min | 2 tasks | 7 files |
| Phase 07-tech-debt-cleanup P02 | 4min | 1 tasks | 2 files |
| Phase 01 P04 | 2min | 1 tasks | 3 files |
| Phase 01 P05 | 3min | 2 tasks | 2 files |
| Phase 06 P02 | 1min | 1 tasks | 2 files |
| Phase 08 P01 | 3min | 2 tasks | 6 files |
| Phase 09 P01 | 8min | 2 tasks | 7 files |
| Phase 09 P02 | 30min | 1 tasks | 15 files |
| Phase 09 P03 | 7min | 2 tasks | 3 files |
| Phase 10 P01 | 4min | 1 tasks | 5 files |
| Phase 10 P02 | 3min | 2 tasks | 4 files |
| Phase 11 P01 | 2min | 2 tasks | 4 files |
| Phase 12 P02 | 2min | 2 tasks | 2 files |
| Phase 12 P01 | 3min | 3 tasks | 9 files |
| Phase 13 P01 | 4min | 2 tasks | 10 files |
| Phase 13 P02 | 4min | 2 tasks | 4 files |
| Phase 13 P03 | 5min | 1 tasks | 2 files |
| Phase 14 P01 | 3min | 2 tasks | 5 files |
| Phase 15 P01 | 3min | 2 tasks | 20 files |
| Phase 15 P03 | 2min | 2 tasks | 5 files |
| Phase 15 P02 | 3min | 2 tasks | 6 files |
| Phase 15 P04 | 2min | 2 tasks | 4 files |
| Phase 16 P01 | 2min | 2 tasks | 6 files |
| Phase 16 P02 | 2min | 2 tasks | 3 files |
| Phase 17 P01 | 2min | 2 tasks | 3 files |
| Phase 18 P01 | 10min | 2 tasks | 7 files |
| Phase 18 P02 | 1min | 1 tasks | 2 files |
| Phase 19 P01 | 2min | 2 tasks | 3 files |
| Phase 19 P02 | 1min | 3 tasks | 5 files |
| Phase 20 P01 | 6min | 3 tasks | 5 files |
| Phase 20 P02 | 10min | 3 tasks | 0 files |
| Phase 20 P03 | 11min | 3 tasks | 0 files |
| Phase 20 P04 | 9min | 3 tasks | 0 files |
| Phase 20 P05 | 7min | 3 tasks | 4 files |

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- [Roadmap]: 4-phase bottom-up structure -- config/scanner first, UI last, packaging at end
- [Roadmap]: python-sane risk addressed in Phase 1 via scanner abstraction layer
- [01-01]: Used settings_customise_sources hook for runtime TOML file path
- [01-01]: Deferred python-sane (requires libsane-dev system headers)
- [Phase 01]: Lazy import for python-sane: deferred C extension loading via _ensure_sane()
- [Phase 01]: httpx multipart upload: combined form fields + file in single files param
- [01-03]: SQLite check_same_thread=False for cross-thread worker access
- [01-03]: Lazy cli import in __init__.py to avoid loading all deps on package import
- [01-03]: Suppress discovery message in --json mode for clean JSON output
- [Phase 02-01]: Used Resampling.LANCZOS instead of Image.LANCZOS for ty/pyrefly type checker compatibility
- [Phase 02]: Used separate except clauses for Python 3.12 AST compatibility in pre-commit hooks
- [Phase 02]: Added _as_image() cast helper to satisfy ty type checker with ThreadPoolExecutor generic results
- [Phase 02]: Extracted _scan_manual_duplex and _scan_simplex helpers to keep run_pipeline under ruff complexity limits
- [Phase 03]: httpx already a production dep; added to dev group for TestClient availability in test env
- [Phase 03-01]: response_model=None on /health for union return type
- [Phase 03-01]: Module-level Form default singleton to avoid B008 mutable default
- [Phase 03-01]: S104 per-file ignore for config.py bind-all-interfaces (LAN app)
- [Phase 03-web-ui]: hx-on::before-request for instant scan button disable before server response
- [Phase 03-web-ui]: Hidden div hx-trigger=load for auto-refreshing history on DONE/ERROR
- [Phase 03-web-ui]: SVG currentColor for dark/light PicoCSS theme compatibility
- [Phase 03-03]: StubScanner concrete class instead of MagicMock to avoid ABC issues with lifespan
- [Phase 03-03]: Direct job_store manipulation for state-dependent tests instead of form submission
- [Phase 04]: Port 8080 in Dockerfile matches OutputConfig.web_port default
- [Phase 04]: PyPI trusted publishing via OIDC -- no API tokens needed
- [Phase 04-01]: DB path for jobs command matches web layer: tmp_dir/saneless.db
- [Phase 05]: log_config=None so uvicorn loggers propagate to root handler configured by CLI group
- [Phase 05]: Click option defaults are None, resolved at runtime from settings (not decoration time)
- [Phase 06]: 502 status code for unexpected paperless test failures (upstream service error)
- [Phase 07-tech-debt-cleanup]: isinstance chain in _categorize_error() instead of separate except blocks for PLR0915 compliance
- [Phase 07-tech-debt-cleanup]: Set transition event on DONE state for mock pipeline compatibility
- [Phase 07-tech-debt-cleanup]: SQLite ALTER TABLE migration with OperationalError suppression for error_category column
- [Phase 07]: Concrete _BrowserTestScanner stub for lifespan-safe browser testing
- [01-04]: Catch OSError alone (PermissionError subclass) for log dir fallback
- [01-04]: XDG state dir (~/.local/state/saneless) for log file default
- [Phase 01]: Field(alias='title') with populate_by_name=True for TOML and code access
- [Phase 01]: Intercept ValidationError extra_forbidden to provide ConfigError with section hints
- [Phase 06]: Use /api/tags/?page_size=1 instead of /api/ for connection testing -- DRF browsable API root returns 200 regardless of auth
- [Phase 06]: Gap closure complete -- PLSS-03 and UI-02 requirements fully satisfied
- [Phase 08]: httpx._types.FileTypes via TYPE_CHECKING guard for multipart upload typing
- [Phase 08]: PLC0415 retained for tests (77 lazy imports); TCH/T201/PLR* removed (0 violations each)
- [Phase 09]: ARG rule ignored for tests via per-file-ignores (ABC stubs have legitimately unused params)
- [Phase 09]: _FakeSaneDevice with pluggable callables and real methods for SaneDevice protocol compliance
- [Phase 09]: Type-narrowing helpers (_get, _app) centralize None checks in test code
- [Phase 09]: MockSaneDev._snap_impl delegate: type-safe MagicMock replacement for snap method
- [Phase 09]: object.__setattr__ for type-unsafe test mock reassignment patterns
- [Phase 09]: TYPE_CHECKING blocks with from __future__ import annotations in all test files
- [Phase 09]: isinstance type narrowing for eagerly-created JobStore cleanup in serve tests
- [Phase 10]: Top-level imports for ProfileConfig/Settings; TYPE_CHECKING for DeviceCapabilities
- [Phase 10]: cast() for tomlkit Container to satisfy ty type checker on 'in' operator
- [Phase 10]: ADF Back excluded from simplex matching via early 'back' check before ADF pattern
- [Phase 10]: CLI auto-profiles uses ctx.parent.params for --config propagation
- [Phase 10]: Worker _auto_generated flag set True before attempt to prevent retrigger on failure
- [Phase 11]: 300 DPI confirmed as correct default per Tesseract OCR minimum recommendation
- [Phase 11]: Single DEFAULT_RESOLUTION constant eliminates duplication between config.py and auto_profiles.py
- [Phase 12]: Dynamic column widths: name_w = max(20, cols - 45) for devices, title_w = max(15, cols - 50) for jobs
- [Phase 12]: humanize_state as Jinja2 filter registered on template env rather than template-level macro
- [Phase 12]: External app.js with IIFE pattern for HTMX event delegation instead of inline scripts
- [Phase 13]: Standalone validate_settings_dirs() instead of Pydantic model_validator -- ConfigError required per D-13
- [Phase 13]: RH-08 uvicorn signal handler fix not needed -- current uvicorn already skips signals in non-main threads
- [Phase 13]: Combined Task 1+2 commit due to tight callback type coupling between pipeline and worker
- [Phase 13]: PipelineEvent StrEnum with is-identity dispatch replacing string matching in worker
- [Phase 13]: _scan_manual_duplex returns (fronts, backs) tuple on mismatch for graceful degradation instead of ScanError
- [Phase 14]: SANE_NET_HOSTS set before sane.init() -- env var must exist before SANE probes backends
- [Phase 14]: Externally-set SANE_NET_HOSTS takes priority over config file scanner.host value
- [Phase 15]: Material theme with indigo palette and dark/light toggle for docs site
- [Phase 15]: Grouped env vars by section for scanability; documented profile env var limitation
- [Phase 15]: Used tabbed content for pipx vs pip install alternatives in bare metal guide
- [Phase 15]: Cross-linked explanation pages to related how-to guides for actionable follow-up
- [Phase 16]: Auto source override placed after _is_adf_source() call, preserving D-05
- [Phase 16]: Literal type on ProfileConfig for validation; plain str on ScanSettings dataclass
- [Phase 16]: Auto source check before flatbed in source_to_slug to prevent substring match
- [Phase 16]: Only write auto_source_mode to TOML when non-default (keep config clean)
- [Phase 16]: Auto-only scanner defaults to adf mode for multi-page capability
- [Phase 17]: Removed FileTypes import entirely; tags as list in data dict for httpx repeated field encoding
- [Phase 18]: Inline Literal in ProfileConfig instead of PaperSize import to satisfy TC001 without noqa
- [Phase 18]: Geometry attributes added to SaneDevice Protocol for type-safe access
- [Phase 18]: Extracted _set_geometry/_maybe_crop helpers for PLR0912/PLR0915 compliance
- [Phase 18]: paper_size placed after auto_source_mode in profiles table for logical grouping
- [Phase 19]: Docker tab first in Quick Start per D-03; pipx over pip for bare metal; no screenshots per D-08
- [Phase 19]: Getting Started placed first after Home in nav per D-01; old Tutorials removed per D-04
- [Phase 20-01]: pytest-timeout dependency + timeout ini keys must land in one commit -- --strict-config makes an unknown ini key a hard error on every pytest run
- [Phase 20-01]: timeout_method = signal, not thread -- thread calls os._exit(1) and kills teardown plus the rest of the suite
- [Phase 20-01]: CI actions pinned to full 40-hex SHAs with version comments, paired with dependabot.yml in the same commit so pins stay maintained
- [Phase 20-01]: libsane-dev apt step before uv sync --locked in every CI job -- python-sane is sdist-only and compiles against sane/sane.h
- [Phase 20-01]: CONTRIBUTING.md follows README.md GitHub markdown dialect, not the mkdocs-material dialect used under docs/
- [Phase 20-02]: git-filter-repo must run as --refs master..autodev --partial -- whole-history filtering drops the SSH gpgsig on root commit a87b3dd, changing its SHA to 18d7911 and destroying the PR merge base
- [Phase 20-02]: Filtered PR branch is autodev-filtered -- 112 commits, zero .planning/ paths in every commit, based on the original signed master root a87b3dd
- [Phase 20-02]: kdknigga/saneless is PRIVATE with zero refs on the remote; defaultBranchRef is empty, so Plan 04's ruleset must target refs/heads/master explicitly
- [Phase 20-02]: bypass_actors: [] remains UNANSWERED -- Plan 04 must treat the ruleset bypass list as an open user input, not a settled decision
- [Phase 20-02]: Verification greps must use 'command grep' -- the shell's grep is a ugrep wrapper whose -qv exit status is wrong and it misclassified 7 commits in the history audit
- [Phase 20-03]: Plan 04's required_status_checks contexts are exactly `lint` and `test`, read back from GET /commits/{sha}/check-runs, with GitHub Actions integration_id 15368
- [Phase 20-03]: Do NOT require copilot-pull-request-reviewer in the master ruleset -- it posts under the same app id 15368 but comes from an account-level Copilot workflow outside this repo, so requiring it would deadlock master if the setting is ever disabled
- [Phase 20-03]: The first push of master made it GitHub's default branch automatically; no gh repo edit --default-branch was needed

### Roadmap Evolution

**Milestone v2.0 (Phases 20-32)** — derived from the reconciled 13-phase spine in `.planning/research/SUMMARY.md`. Load-bearing orderings: CI first (20); job-store lock + migration ladder (22) before typed results (23); scanner truthfulness (24) before manual duplex (25); `output.data_dir` with `failed/` preservation (23); config-directory mount with atomic config write (27); htmx vendoring with the Scan-button fix (26); appliance layer (30) after honest outcomes, scanner truth, and exception translation; delivery/identity (31) then suite hygiene (32) last.

- Phase 8 added: Audit lint and type checker ignores and noqas and fix them
- Phase 9 added: Enable pytest strict mode and ensure all tests pass the same linting and type checking quality checks as normal code
- Phase 10 added: Automatic scanner profile creation
- Phase 11 added: Review and adjust default DPI setting
- Phase 12 added: UI polish: humanize enum labels, add accessible button labels, fix CLI table truncation, replace inline HTMX scripts, normalize spacing to design token grid
- Phase 14 added: Enable containerized scanner detection by wiring scanner.host config into SANE net backend via SANE_NET_HOSTS environment variable
- Phase 15 added: Create user-facing documentation using the Diátaxis approach
- Phase 16 added: When a scanner advertised auto mode, it should be configurable by the user if that means flatbed mode or ADF mode
- Phase 17 added: Fix Paperless upload error: datetime format and title type mismatch in API payload
- Phase 18 added: Automatic scanned page size detection or user-specified paper size to avoid capturing the full scanner bed
- Phase 19 added: Write user-facing docs including a full getting started section that walks a new user through setup and first scan using the Diátaxis model

### Pending Todos

None yet.

### Blockers/Concerns

- python-sane Python 3.14 compatibility unverified -- test early in Phase 1
- paperless-ngx API docs inaccessible (403) -- validate endpoints against GitHub source in Phase 1

### Quick Tasks Completed

| # | Description | Date | Commit | Directory |
|---|-------------|------|--------|-----------|
| 260320-j26 | Fix all linter and type checker failures in pre-commit | 2026-03-20 | bb774c7 | [260320-j26-fix-linter-and-type-checker-failures-in-](./quick/260320-j26-fix-linter-and-type-checker-failures-in-/) |
| 260420-bkr | Commit outstanding changes (gitignore, debug notes, phase scaffolds 16-19) | 2026-04-20 | 226426c | [260420-bkr-commit-outstanding-changes](./quick/260420-bkr-commit-outstanding-changes/) |
| 260424-wtc | Remove orphaned agent worktrees | 2026-04-24 | 8686793 | [260424-wtc-remove-orphaned-agent-worktrees](./quick/260424-wtc-remove-orphaned-agent-worktrees/) |

## Session Continuity

Last activity: 2026-09-10 - Completed 20-01-PLAN.md (CI workflow, Dependabot, CONTRIBUTING.md, pytest-timeout)
Last session: 2026-09-10T21:11:10.374Z
Stopped at: Phase 22 context gathered
Resume file: .planning/phases/22-job-store-hardening/22-CONTEXT.md
