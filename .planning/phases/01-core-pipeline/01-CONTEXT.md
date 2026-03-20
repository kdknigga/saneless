# Phase 1: Core Pipeline - Context

**Gathered:** 2026-03-20
**Status:** Complete (all plans executed)
**Updated:** 2026-03-20

<domain>
## Phase Boundary

CLI-driven flatbed scan pipeline: scanner discovery, device selection, flatbed single-page scan, PDF assembly via img2pdf, and paperless-ngx REST API upload with metadata. Includes pydantic-settings configuration (TOML + env vars), scanner abstraction layer (ABC), background worker thread with queue.Queue, rotating log file, and CLI commands (`saneless scan`, `saneless devices`). ADF, duplex, web UI, and packaging are separate phases.

</domain>

<decisions>
## Implementation Decisions

### CLI output & feedback
- Minimal status lines by default: "Scanning...", "Assembling PDF...", "Uploading to paperless-ngx...", "Done: [title]"
- `-v` flag for verbose/debug output (maps to DEBUG log level)
- Errors printed to stderr with actionable messages, not raw library errors
- Exit codes: 0 success, 1 scan error, 2 config error, 3 paperless error
- `saneless devices` outputs human-readable table (device name, vendor, model, type); `--json` flag for programmatic use
- No progress bar for v1 — simple text status transitions

### Scanner abstraction depth
- Abstract base class (ABC/Protocol) with methods: `get_devices()`, `get_capabilities(device_id)`, `scan_pages(device_id, settings) -> Iterator[Image]`
- Single `SaneBackend` implementation wrapping python-sane
- Designed for testability — mock backend possible for unit tests
- Profile source values (`Flatbed`, `ADF`, `ADF Duplex`) mapped to device-reported option values at runtime
- Fail with clear error if device doesn't support requested source
- `saneless devices --capabilities` dumps raw SANE options for debugging
- `sane.init()` called once at startup, never per-scan (fd leak prevention per Pitfall #1)
- Device opened at job start, closed in context manager; `sane.cancel()` before `close()` on all paths
- No progress callbacks to python-sane (segfault prevention per Pitfall #2)

### Configuration defaults & validation
- Fail fast at startup on invalid config: clear parse errors, missing required fields listed
- Invalid profile definitions → error identifying the bad profile
- Optional fields have sensible defaults
- saned unreachable at startup → log warning but start anyway; device discovery retried on each scan attempt
- Config file location: `--config` flag for explicit path; fallback search `./saneless.toml`, `~/.config/saneless/config.toml`, `/etc/saneless/config.toml`
- Env vars override any file value (pydantic-settings standard behavior)
- SANELESS_ prefix for env vars

### Paperless-ngx upload behavior
- Retry 3 times with exponential backoff (1s, 2s, 4s) on network errors only; no retry on 4xx
- After retries exhausted, fall back to consume directory if configured
- Task polling: exponential backoff 1s→2s→4s→8s→16s→30s (capped), total timeout 5 minutes (configurable)
- Handle task-not-found on first poll with retry (race condition per Pitfall #8)
- Title required (from `--title` flag or profile default template); tags, correspondent, created date optional
- Created date defaults to scan timestamp (timezone-aware)

### Claude's Discretion
- Exact module/file organization within `src/saneless/`
- SQLite schema details for job persistence
- Exact pydantic model structure for config
- Worker thread implementation details (queue size, shutdown behavior)
- Logging format string and rotation settings
- Temp directory structure within configured tmp_dir

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Project requirements
- `docs/PRD.md` — Full product requirements document with functional specs, architecture, config schema, user stories
- `docs/PRD.md` §7.1 — Scanner discovery & configuration requirements
- `docs/PRD.md` §7.3 — Scanning requirements (empty feeder handling, worker thread model)
- `docs/PRD.md` §7.4 — PDF assembly requirements (img2pdf, temp files)
- `docs/PRD.md` §7.5 — Paperless-ngx ingestion (upload, task polling, connection test, metadata fields)
- `docs/PRD.md` §7.7 — CLI requirements
- `docs/PRD.md` §7.8 — Error handling & logging requirements
- `docs/PRD.md` §9.4 — TOML configuration schema (exact structure)

### Research findings
- `.planning/research/STACK.md` — Verified library versions and compatibility notes
- `.planning/research/PITFALLS.md` — Critical python-sane pitfalls (fd leaks, segfaults, device locking, option variance)
- `.planning/research/ARCHITECTURE.md` — Component boundaries, data flow, build order

### Code conventions
- `.planning/codebase/CONVENTIONS.md` — Naming, style, imports, error handling, docstring patterns

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `src/saneless/__init__.py`: Stub `main()` — entry point to be expanded with click CLI
- `pyproject.toml`: Package config with entry point `saneless = "saneless:main"`, ruff settings, pytest config

### Established Patterns
- Python 3.14 with strict type hints (ANN rule enforced by ruff)
- Google-style docstrings on all public symbols
- snake_case functions, PascalCase classes
- `logging.getLogger(__name__)` at module level
- Relative imports within package (`from .config import Settings`)

### Integration Points
- Entry point: `saneless:main` in pyproject.toml — click CLI group replaces stub
- Pre-commit hooks: ruff format + lint, ty + pyrefly type checking, uv sync
- Test framework: pytest in `tests/` directory

</code_context>

<specifics>
## Specific Ideas

No specific requirements — open to standard approaches. The PRD is highly detailed and prescriptive for this phase.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 01-core-pipeline*
*Context gathered: 2026-03-20*
