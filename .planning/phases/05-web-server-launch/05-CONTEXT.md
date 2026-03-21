# Phase 5: Web Server Launch Command - Context

**Gathered:** 2026-03-20
**Status:** Ready for planning

<domain>
## Phase Boundary

Add `saneless serve` CLI command that starts the FastAPI web server with proper logging, and add `CMD ["serve"]` to the Dockerfile so the container launches the web UI by default. No new web features, no UI changes, no new API endpoints. This is pure integration: wiring the existing web app factory to a CLI entrypoint and Docker entrypoint.

</domain>

<decisions>
## Implementation Decisions

### Serve command design
- New Click command `saneless serve` added to `cli.py` following existing command pattern
- Flags: `--host` (default from config, fallback `0.0.0.0`) and `--port` (default from config, fallback `8080`)
- No `--reload` flag — production-only command, dev reload handled by running uvicorn directly
- No `--workers` flag — single worker, single scanner model (queue.Queue is not multi-process safe)
- Prints listening address to stdout on startup (e.g., "Serving on http://0.0.0.0:8080")
- Uses `uvicorn.run()` programmatically to start the server

### Logging integration
- `serve` command calls `configure_logging()` before `uvicorn.run()`, same pattern as `scan`/`devices`/`jobs` commands
- Uvicorn's own loggers configured to use the same rotating file handler as the application
- `--verbose` flag (inherited from CLI group) enables uvicorn debug logging and stderr output
- Uvicorn access log format matches application log format for consistency in the rotating log file

### Docker CMD
- Add `CMD ["serve"]` to Dockerfile — combined with existing `ENTRYPOINT ["saneless"]` produces `saneless serve`
- Container binds `0.0.0.0:8080` by default (matches existing EXPOSE and HEALTHCHECK)
- Existing HEALTHCHECK instruction unchanged — already correct (`curl -f http://localhost:8080/health`)
- Update docker-compose.yml if present to reflect the CMD addition

### Claude's Discretion
- Exact uvicorn log handler wiring approach (log_config dict vs programmatic handler attachment)
- Whether to suppress uvicorn's default banner in favor of saneless's own startup message
- Uvicorn server options (timeout-keep-alive, limit-concurrency) if any are needed
- Test approach for the serve command (unit test with mocked uvicorn.run vs integration)

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Product requirements
- `docs/PRD.md` — Full product requirements document
- `docs/PRD.md` §8 — Non-functional requirements: container constraints, HEALTHCHECK spec
- `docs/PRD.md` §9.1 — Process model: FastAPI + uvicorn architecture

### Prior phase context
- `.planning/phases/03-web-ui/03-CONTEXT.md` — Web UI architecture decisions (FastAPI, Jinja2+HTMX, PicoCSS, health endpoint)
- `.planning/phases/04-packaging-and-deployment/04-CONTEXT.md` — Dockerfile design, port 8080, HEALTHCHECK, ENTRYPOINT

### Existing implementation
- `src/saneless/cli.py` — Click CLI group, existing commands pattern, `configure_logging()` call
- `src/saneless/web/app.py` — `create_app()` factory that builds the full FastAPI application
- `src/saneless/logging_config.py` — `configure_logging()` with rotating file handler
- `src/saneless/config.py` — `OutputConfig.web_port` default, settings model
- `Dockerfile` — Two-stage build, ENTRYPOINT/HEALTHCHECK already present, needs CMD

### Requirements
- `.planning/REQUIREMENTS.md` — UI-01 through UI-08, HLTH-01, HLTH-02, LOG-01, LOG-02, LOG-03, PKG-02

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `src/saneless/web/app.py`: `create_app(settings, scanner)` — complete FastAPI app factory, just needs to be called from CLI
- `src/saneless/cli.py`: Click CLI group with `--config`, `--verbose` flags — `serve` follows same pattern
- `src/saneless/logging_config.py`: `configure_logging()` — already called by all other CLI commands
- `src/saneless/scanner/sane_backend.py`: `SaneBackend` — scanner instance needed by `create_app()`
- `src/saneless/config.py`: `OutputConfig.web_port` (default 8080) — available for `--port` default

### Established Patterns
- CLI commands: `@cli.command()` with `@click.pass_context`, settings from `ctx.obj["settings"]`
- Logging: `configure_logging()` called in CLI group callback before any command runs
- App factory: `create_app(settings, scanner)` returns fully configured FastAPI app
- Config: pydantic-settings with TOML + env var override

### Integration Points
- `serve` command creates `SaneBackend()` and calls `create_app(settings, scanner)` to get the app
- `uvicorn.run(app, host=host, port=port)` starts the server
- Dockerfile `CMD ["serve"]` + existing `ENTRYPOINT ["saneless"]` = `saneless serve`
- HEALTHCHECK already targets `localhost:8080/health` — no changes needed

</code_context>

<specifics>
## Specific Ideas

- The serve command is the missing link: `create_app()` exists but nothing calls it outside of tests
- Dockerfile currently has ENTRYPOINT but no CMD — container can't start without manual args
- Logging must be initialized before uvicorn to ensure application logs go to rotating file from the start
- Uvicorn's default logging should be redirected to avoid dual-logging (uvicorn default + rotating file)

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 05-web-server-launch*
*Context gathered: 2026-03-20*
