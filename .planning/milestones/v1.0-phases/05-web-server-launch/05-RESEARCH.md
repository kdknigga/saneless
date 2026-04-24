# Phase 5: Web Server Launch Command - Research

**Researched:** 2026-03-20
**Domain:** CLI-to-web integration, uvicorn programmatic API, Docker CMD
**Confidence:** HIGH

## Summary

Phase 5 is a pure integration phase -- no new features, no new libraries. The existing `create_app()` factory, Click CLI group, `configure_logging()`, and Dockerfile all exist. The work is wiring them together: a new `serve` Click command that calls `uvicorn.run()` with the app from `create_app()`, and adding `CMD ["serve"]` to the Dockerfile.

The primary technical challenge is uvicorn log configuration. Uvicorn ships its own `dictConfig`-based logging setup that creates separate handlers for `uvicorn`, `uvicorn.error`, and `uvicorn.access` loggers. This conflicts with the application's `configure_logging()` which sets up a single rotating file handler on the root logger. The solution is passing `log_config=None` to `uvicorn.run()`, which disables uvicorn's custom logging setup entirely and lets its loggers propagate to the root logger.

**Primary recommendation:** Pass `log_config=None` and `access_log=True` to `uvicorn.run()` so all uvicorn output flows through the already-configured rotating file handler. Set `log_level` to match the application's configured level.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- New Click command `saneless serve` added to `cli.py` following existing command pattern
- Flags: `--host` (default from config, fallback `0.0.0.0`) and `--port` (default from config, fallback `8080`)
- No `--reload` flag -- production-only command
- No `--workers` flag -- single worker, queue.Queue not multi-process safe
- Prints listening address to stdout on startup
- Uses `uvicorn.run()` programmatically to start the server
- `serve` command calls `configure_logging()` before `uvicorn.run()` (same pattern as scan/devices/jobs)
- Uvicorn's own loggers configured to use same rotating file handler
- `--verbose` flag (inherited from CLI group) enables uvicorn debug logging and stderr output
- Uvicorn access log format matches application log format
- Add `CMD ["serve"]` to Dockerfile -- combined with ENTRYPOINT produces `saneless serve`
- Container binds `0.0.0.0:8080` by default
- Existing HEALTHCHECK instruction unchanged

### Claude's Discretion
- Exact uvicorn log handler wiring approach (log_config dict vs programmatic handler attachment)
- Whether to suppress uvicorn's default banner in favor of saneless's own startup message
- Uvicorn server options (timeout-keep-alive, limit-concurrency) if any are needed
- Test approach for the serve command (unit test with mocked uvicorn.run vs integration)

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| UI-01 | Web UI accessible from any browser on the LAN | `serve` command with `--host 0.0.0.0` makes web UI reachable on LAN |
| UI-02 | Live status indicator shows job state via polling | Already implemented in web layer; serve command just starts the server |
| UI-03 | Manual duplex awaiting_flip state with flip prompt | Already implemented; serve command enables access |
| UI-04 | First-page thumbnail displayed in status area | Already implemented; serve command enables access |
| UI-05 | Job history table persisted to SQLite | Already implemented; serve command enables access |
| UI-06 | Job history pruned by age and count | Already implemented in app lifespan; serve command triggers it |
| UI-07 | Scan button disabled while job in progress | Already implemented; serve command enables access |
| UI-08 | Refresh icon on tag/correspondent dropdowns | Already implemented; serve command enables access |
| PROF-03 | User can select profile from dropdown in web UI | Already implemented; serve command enables access |
| PLSS-04 | User can set title, tags, correspondent before scanning | Already implemented; serve command enables access |
| PLSS-05 | Tag and correspondent lists cached with TTL and manual refresh | Already implemented; serve command enables access |
| HLTH-01 | GET /health returns 200 when web layer and worker running, 503 otherwise | Already implemented at `/health` endpoint; Docker HEALTHCHECK targets it |
| HLTH-02 | Health endpoint requires no authentication | Already implemented; no auth layer exists |
| LOG-01 | All errors and significant events written to rotating log file | `configure_logging()` called before `uvicorn.run()`; uvicorn logs flow through root logger |
| LOG-02 | Log level configurable | Settings `output.log_level` controls root logger level; passed to uvicorn via `log_level` param |
| LOG-03 | All scan/API/assembly errors displayed in web UI | Already implemented; serve command enables access |
| PKG-02 | OCI container image with HEALTHCHECK | Dockerfile gets `CMD ["serve"]` to complete the container startup chain |
</phase_requirements>

## Standard Stack

### Core (already installed)
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| uvicorn | 0.42.0 | ASGI server | Already a dependency; `uvicorn.run()` is the programmatic API |
| click | 8.3+ | CLI framework | Already used for all CLI commands |
| fastapi | 0.135+ | Web framework | Already used; `create_app()` factory exists |

### Supporting (already installed)
No new dependencies needed. Everything required is already in `pyproject.toml`.

### Alternatives Considered
None -- all decisions are locked. This phase adds zero new dependencies.

## Architecture Patterns

### Serve Command Structure
The `serve` command follows the exact same pattern as `scan`, `devices`, and `jobs`:

```python
@cli.command()
@click.option("--host", default=None, help="Bind host.")
@click.option("--port", default=None, type=int, help="Bind port.")
@click.pass_context
def serve(ctx: click.Context, host: str | None, port: int | None) -> None:
    """Start the web server."""
    settings = ctx.obj["settings"]
    verbose = ctx.obj["verbose"]

    actual_host = host or settings.output.web_host
    actual_port = port or settings.output.web_port

    scanner = SaneBackend()
    app = create_app(settings, scanner)

    click.echo(f"Serving on http://{actual_host}:{actual_port}")

    uvicorn.run(
        app,
        host=actual_host,
        port=actual_port,
        log_config=None,
        log_level=settings.output.log_level.lower(),
        access_log=True,
    )
```

**Key observations from existing code:**
1. `configure_logging()` is called in the `cli` group callback (line 54 of cli.py), which runs BEFORE any subcommand. So `serve` does NOT need to call it -- it is already called.
2. Settings and verbose flag are available via `ctx.obj`.
3. `create_app(settings, scanner)` handles all app wiring including lifespan (worker start/stop, job pruning).

### Uvicorn Log Configuration (Claude's Discretion -- RECOMMENDATION)

**Recommendation: Use `log_config=None`**

Uvicorn's default `log_config` is a `dictConfig` dict that:
- Creates `uvicorn`, `uvicorn.error`, and `uvicorn.access` loggers
- Attaches StreamHandlers to stderr/stdout
- Sets `propagate=False` on `uvicorn` and `uvicorn.access`

By passing `log_config=None`, uvicorn skips its `dictConfig` call entirely. The uvicorn loggers then propagate to the root logger, which `configure_logging()` has already equipped with a RotatingFileHandler. This is the cleanest approach because:
- No dual-logging (uvicorn's handlers + our handlers)
- Consistent format: all log lines use the same formatter
- No complex dict construction or manual handler attachment
- The `log_level` parameter still works independently of `log_config`

**Confidence: HIGH** -- verified by reading the default `log_config` dict from `uvicorn.run` signature (v0.42.0). When `log_config=None`, uvicorn calls `logging.config.dictConfig` with nothing, leaving loggers in their default state (propagate to root).

### Uvicorn Banner Suppression (Claude's Discretion -- RECOMMENDATION)

**Recommendation: Let uvicorn print its own "Started server process" and "Uvicorn running on" messages, but also print the saneless-specific message before `uvicorn.run()`.**

Uvicorn logs "Started server process [PID]" and "Uvicorn running on http://host:port" at INFO level to the `uvicorn.error` logger. With `log_config=None`, these go to the root logger (rotating file + stderr if verbose). The saneless `click.echo()` message prints to stdout regardless, giving the user immediate feedback. No suppression needed -- the uvicorn messages are useful in the log file.

### Uvicorn Server Options (Claude's Discretion -- RECOMMENDATION)

**Recommendation: No extra options needed.** The defaults are fine for this use case:
- `timeout_keep_alive=5` -- adequate for LAN use
- `limit_concurrency=None` -- no limit, appropriate for single-user LAN app
- `workers=None` -- single worker (locked decision)

### Dockerfile CMD Addition

```dockerfile
ENTRYPOINT ["saneless"]
CMD ["serve"]
```

This produces `saneless serve` as the default command. Users can override: `docker run saneless devices`.

The existing docker-compose.yml does not specify a `command:`, so it will use the image's CMD automatically. No changes needed to docker-compose.yml.

### Anti-Patterns to Avoid
- **Do not call `configure_logging()` inside the serve command** -- it is already called in the CLI group callback. Calling it twice would add duplicate handlers.
- **Do not pass a custom `log_config` dict to uvicorn** -- this fights with the root logger setup. Use `None` instead.
- **Do not use `uvicorn.Config` + `uvicorn.Server` classes directly** -- `uvicorn.run()` is simpler and sufficient for single-worker synchronous startup.
- **Do not add `--reload` or `--workers`** -- locked decision. Dev reload is done via `uvicorn src.saneless.web.app:create_app --factory --reload` directly.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| ASGI server | Custom server loop | `uvicorn.run()` | Handles signal handling, graceful shutdown, keep-alive |
| Log unification | Manual handler surgery on uvicorn loggers | `log_config=None` | One line vs fragile handler manipulation |
| CLI option defaults from config | Custom default resolution | Click option with `default=None` + fallback in command body | Click doesn't support dynamic defaults from context |

## Common Pitfalls

### Pitfall 1: Duplicate Log Handlers
**What goes wrong:** Calling `configure_logging()` both in CLI group and serve command adds two RotatingFileHandlers to root logger, doubling every log line.
**Why it happens:** Developer doesn't realize CLI group callback already calls `configure_logging()`.
**How to avoid:** The serve command body must NOT call `configure_logging()`. It is handled by the `@cli.command()` group callback.
**Warning signs:** Log file shows duplicate lines for every event.

### Pitfall 2: Uvicorn Overrides Application Logging
**What goes wrong:** Uvicorn's default `log_config` calls `dictConfig` which can reconfigure existing loggers, removing the rotating file handler.
**Why it happens:** `dictConfig` with `disable_existing_loggers: False` still creates new handlers that compete.
**How to avoid:** Pass `log_config=None` to `uvicorn.run()`.
**Warning signs:** Logs appear on stderr but not in the rotating file.

### Pitfall 3: Click Default vs Config Default
**What goes wrong:** Setting `default=settings.output.web_port` in the Click decorator fails because settings aren't available at decoration time.
**Why it happens:** Click decorators execute at import time, before the CLI group callback runs.
**How to avoid:** Use `default=None` in the Click option, then resolve to config value in the command body.
**Warning signs:** `AttributeError` or `NameError` at import time.

### Pitfall 4: Container Binds to 127.0.0.1
**What goes wrong:** Server not reachable from outside the container.
**Why it happens:** Uvicorn defaults to `127.0.0.1` if host not specified. Container networking requires `0.0.0.0`.
**How to avoid:** Config default is already `0.0.0.0` in `OutputConfig.web_host`. Ensure the serve command uses it.
**Warning signs:** `curl localhost:8080` from host returns "Connection refused".

### Pitfall 5: SaneBackend Initialization in Serve Command
**What goes wrong:** Scanner initialization fails or behaves differently from CLI scan command.
**Why it happens:** `SaneBackend()` must be created before `create_app()` because the app factory wires it into the worker.
**How to avoid:** Follow the same pattern as the `scan` command: create `SaneBackend()` first, then pass to `create_app()`.
**Warning signs:** Scanner not found errors, or `create_app` fails with missing scanner argument.

## Code Examples

### Complete Serve Command (verified pattern from existing cli.py)
```python
import uvicorn

from .web.app import create_app

@cli.command()
@click.option("--host", default=None, help="Bind address.")
@click.option("--port", default=None, type=int, help="Bind port.")
@click.pass_context
def serve(ctx: click.Context, host: str | None, port: int | None) -> None:
    """Start the web server."""
    settings = ctx.obj["settings"]
    actual_host = host or settings.output.web_host
    actual_port = port or settings.output.web_port

    scanner = SaneBackend()
    app = create_app(settings, scanner)

    click.echo(f"Serving on http://{actual_host}:{actual_port}")

    uvicorn.run(
        app,
        host=actual_host,
        port=actual_port,
        log_config=None,
        log_level=settings.output.log_level.lower(),
        access_log=True,
    )
```

### Dockerfile CMD (one-line addition)
```dockerfile
ENTRYPOINT ["saneless"]
CMD ["serve"]
```

### Test Pattern (mocked uvicorn.run)
```python
def test_serve_calls_uvicorn(self, monkeypatch):
    """Serve command calls uvicorn.run with correct host/port."""
    captured = {}

    def mock_uvicorn_run(app, **kwargs):
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr("saneless.cli.uvicorn.run", mock_uvicorn_run)
    runner, settings = _patch_cli(monkeypatch)
    from saneless.cli import cli

    result = runner.invoke(cli, ["serve"])
    assert result.exit_code == 0
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 8080
    assert captured["log_config"] is None
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `uvicorn.Config` + `Server.run()` | `uvicorn.run()` | uvicorn 0.15+ | Simpler programmatic API |
| `log_config=LOGGING_CONFIG` custom dict | `log_config=None` + root logger | Always available | Avoids fighting with dictConfig |

**No deprecated features in play.** Uvicorn 0.42.0 is current and `uvicorn.run()` is the stable programmatic API.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.0.2 |
| Config file | `pyproject.toml` [tool.pytest.ini_options] |
| Quick run command | `uv run pytest tests/test_cli.py -x` |
| Full suite command | `uv run pytest` |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| UI-01 | serve command starts web server on configured host/port | unit | `uv run pytest tests/test_cli.py::TestServeCommand -x` | Wave 0 |
| LOG-01 | uvicorn uses rotating file handler (log_config=None) | unit | `uv run pytest tests/test_cli.py::TestServeCommand::test_serve_log_config_none -x` | Wave 0 |
| LOG-02 | log level passed to uvicorn | unit | `uv run pytest tests/test_cli.py::TestServeCommand::test_serve_log_level -x` | Wave 0 |
| PKG-02 | Dockerfile has CMD ["serve"] | unit | `uv run pytest tests/test_cli.py::TestServeCommand::test_serve_default_args -x` | Wave 0 (Dockerfile verified manually or via grep) |
| HLTH-01 | Health endpoint returns 200 | integration | `uv run pytest tests/test_web.py -x` | Exists |
| HLTH-02 | Health endpoint no auth | integration | `uv run pytest tests/test_web.py -x` | Exists |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_cli.py -x`
- **Per wave merge:** `uv run pytest`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_cli.py::TestServeCommand` -- new test class for serve command (mock uvicorn.run, verify args)
- No new test files or framework config needed -- existing infrastructure covers all needs

## Open Questions

1. **SaneBackend initialization in container without scanner**
   - What we know: `SaneBackend()` calls `sane.init()` which requires libsane. Container has libsane installed. But if no scanner is configured/reachable at startup, does `SaneBackend()` throw?
   - What's unclear: Whether `SaneBackend()` constructor fails or just `get_devices()` returns empty
   - Recommendation: Check `SaneBackend.__init__` -- if it calls `sane.init()` eagerly, the serve command should handle the case where no scanner is available at startup (the web UI handles this gracefully already). This is likely fine since the existing scan command creates SaneBackend the same way.

## Sources

### Primary (HIGH confidence)
- `uvicorn.run()` signature -- inspected from installed uvicorn 0.42.0 via `inspect.signature()`
- Existing source code: `cli.py`, `web/app.py`, `logging_config.py`, `config.py`, `Dockerfile`
- Existing test patterns: `tests/test_cli.py`

### Secondary (MEDIUM confidence)
- Uvicorn `log_config=None` behavior -- consistent with uvicorn documentation pattern; verified by reading default dict from signature

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- zero new dependencies, all verified installed
- Architecture: HIGH -- follows existing CLI command pattern exactly, uvicorn.run() API verified
- Pitfalls: HIGH -- identified from direct code reading of cli.py and uvicorn signature
- Logging: HIGH -- uvicorn log_config=None verified from actual installed version

**Research date:** 2026-03-20
**Valid until:** 2026-04-20 (stable -- no fast-moving components)
