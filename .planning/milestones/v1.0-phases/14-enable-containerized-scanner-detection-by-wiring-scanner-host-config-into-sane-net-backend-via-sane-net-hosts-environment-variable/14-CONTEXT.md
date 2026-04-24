# Phase 14: Enable Containerized Scanner Detection - Context

**Gathered:** 2026-03-22
**Status:** Ready for planning

<domain>
## Phase Boundary

Wire the existing `settings.scanner.host` config value into the SANE net backend so containerized saneless can discover network scanners. The `ScannerConfig.host` field already exists but is unused — this phase makes it functional by setting the `SANE_NET_HOSTS` environment variable before `sane.init()`.

</domain>

<decisions>
## Implementation Decisions

### Environment variable injection point
- **D-01:** Set `SANE_NET_HOSTS` in `SaneBackend.__init__()` before `sane.init()`, not at CLI entry point or in Dockerfile
- **D-02:** Only set the env var when `scanner.host` is non-empty — don't override if user has configured it externally (e.g. via Docker env var or /etc/sane.d/net.conf)
- **D-03:** `SaneBackend.__init__()` gains an optional `host: str = ""` parameter; callers in cli.py pass `settings.scanner.host`

### Multiple hosts support
- **D-04:** `scanner.host` accepts a single hostname/IP or a semicolon-separated list (SANE_NET_HOSTS uses semicolons as delimiter)
- **D-05:** No validation of host format — SANE itself reports connection failures with clear messages

### Docker configuration
- **D-06:** Add `SANELESS_SCANNER__HOST` example (commented out) to docker-compose.yml showing how to point at a scanner host
- **D-07:** No changes to Dockerfile — the env var is set at runtime by the application, not baked into the image

### Backward compatibility
- **D-08:** When `scanner.host` is empty (default), behavior is unchanged — SANE uses its own discovery mechanisms (USB, mDNS, net.conf)

### Claude's Discretion
- Logging verbosity when SANE_NET_HOSTS is set
- Test approach for env var injection (mock vs. monkeypatch)

</decisions>

<specifics>
## Specific Ideas

- The primary use case is Docker containers that can't access USB scanners and need to reach a `saned` daemon or SANE-over-network scanner on the LAN
- Should "just work" — user adds `scanner.host = "192.168.1.50"` to config.toml and the container discovers the scanner

</specifics>

<canonical_refs>
## Canonical References

No external specs — requirements are fully captured in decisions above.

### Existing code
- `src/saneless/config.py` — `ScannerConfig` class with unused `host` field (line 47-51)
- `src/saneless/scanner/sane_backend.py` — `SaneBackend.__init__()` where `sane.init()` is called (line 175-179)
- `src/saneless/cli.py` — 4 `SaneBackend()` instantiation sites (lines 102, 166, 277, 312)
- `docker-compose.yml` — deployment example that needs scanner host env var documentation

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `ScannerConfig.host` field already exists with correct default (`""`)
- `Settings` model already loads scanner config from TOML and env vars (`SANELESS_SCANNER__HOST`)
- All 4 CLI commands have `settings` available at the point where `SaneBackend()` is constructed

### Established Patterns
- Env var override pattern: `SANELESS_` prefix with `__` nested delimiter (pydantic-settings)
- Lazy import pattern for sane module via `_ensure_sane()` in sane_backend.py
- Docker env vars documented in docker-compose.yml comments

### Integration Points
- `SaneBackend.__init__()` — add host parameter, set os.environ before sane.init()
- `cli.py` scan/devices/serve/auto_profiles commands — pass settings.scanner.host to SaneBackend
- `docker-compose.yml` — add commented example for SANELESS_SCANNER__HOST

</code_context>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope

</deferred>

---

*Phase: 14-enable-containerized-scanner-detection-by-wiring-scanner-host-config-into-sane-net-backend-via-sane-net-hosts-environment-variable*
*Context gathered: 2026-03-22*
