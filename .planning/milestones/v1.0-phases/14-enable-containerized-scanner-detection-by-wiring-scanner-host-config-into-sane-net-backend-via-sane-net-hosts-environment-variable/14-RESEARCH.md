# Phase 14: Enable Containerized Scanner Detection - Research

**Researched:** 2026-03-22
**Domain:** SANE network backend, environment variable injection, Python os.environ
**Confidence:** HIGH

## Summary

This phase wires the existing `settings.scanner.host` config value into SANE's net backend by setting the `SANE_NET_HOSTS` environment variable before `sane.init()`. The scope is narrow: modify `SaneBackend.__init__()` to accept a `host` parameter, set `os.environ["SANE_NET_HOSTS"]` when non-empty, update the 4 CLI call sites to pass `settings.scanner.host`, and add a commented example to `docker-compose.yml`.

A critical correction from research: **SANE_NET_HOSTS uses colons as delimiters, not semicolons** as stated in D-04. The official SANE man page (sane-net(5)) specifies "a colon-separated list of hostnames or IP addresses." IPv6 addresses must be enclosed in square brackets (e.g., `[::1]`) to distinguish from the colon delimiters. The implementation should use colons.

**Primary recommendation:** Set `os.environ["SANE_NET_HOSTS"]` in `SaneBackend.__init__()` before `sane.init()` when the host parameter is non-empty, using colons as the delimiter for multiple hosts. Log the configured hosts at INFO level.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Set `SANE_NET_HOSTS` in `SaneBackend.__init__()` before `sane.init()`, not at CLI entry point or in Dockerfile
- **D-02:** Only set the env var when `scanner.host` is non-empty -- don't override if user has configured it externally (e.g. via Docker env var or /etc/sane.d/net.conf)
- **D-03:** `SaneBackend.__init__()` gains an optional `host: str = ""` parameter; callers in cli.py pass `settings.scanner.host`
- **D-04:** `scanner.host` accepts a single hostname/IP or a delimiter-separated list (**CORRECTION: SANE uses colons, not semicolons -- see Open Questions**)
- **D-05:** No validation of host format -- SANE itself reports connection failures with clear messages
- **D-06:** Add `SANELESS_SCANNER__HOST` example (commented out) to docker-compose.yml showing how to point at a scanner host
- **D-07:** No changes to Dockerfile -- the env var is set at runtime by the application, not baked into the image
- **D-08:** When `scanner.host` is empty (default), behavior is unchanged -- SANE uses its own discovery mechanisms (USB, mDNS, net.conf)

### Claude's Discretion
- Logging verbosity when SANE_NET_HOSTS is set
- Test approach for env var injection (mock vs. monkeypatch)

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope
</user_constraints>

## Standard Stack

### Core
No new dependencies. This phase uses only Python stdlib (`os`, `logging`) and modifies existing code.

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| os (stdlib) | N/A | Set `SANE_NET_HOSTS` env var | Only way to pass env vars to C-level sane library |

### Alternatives Considered
None -- `os.environ` is the only mechanism to pass environment variables to the SANE C library before `sane.init()`.

## Architecture Patterns

### Pattern 1: Environment Variable Injection Before Library Init
**What:** Set `os.environ["SANE_NET_HOSTS"]` in `SaneBackend.__init__()` before calling `sane.init()`.
**When to use:** When a C library reads environment variables at initialization time.
**Example:**
```python
# Source: SANE man page sane-net(5)
import os

class SaneBackend(ScannerBackend):
    def __init__(self, host: str = "") -> None:
        _ensure_sane()
        if host and "SANE_NET_HOSTS" not in os.environ:
            os.environ["SANE_NET_HOSTS"] = host
            logger.info("SANE_NET_HOSTS set to %s", host)
        self._sane_version = sane.init()
        logger.info("SANE initialized, version %s", self._sane_version)
```

### Pattern 2: CLI Call Site Updates
**What:** Pass `settings.scanner.host` to `SaneBackend()` at all 4 construction sites in cli.py.
**When to use:** Each CLI command that creates a SaneBackend.
**Example:**
```python
scanner = SaneBackend(host=settings.scanner.host)
```

### Anti-Patterns to Avoid
- **Setting env var at CLI entry point:** Would affect all subprocesses and is too early (before settings are loaded). D-01 locks this to `SaneBackend.__init__()`.
- **Overriding existing SANE_NET_HOSTS:** Per D-02, if the env var is already set (e.g., from Docker `-e`), don't clobber it.
- **Validating host format:** Per D-05, SANE provides clear error messages for unreachable hosts. Don't add regex validation.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Host format validation | Custom IP/hostname regex | Let SANE report errors | SANE's error messages are clear; validation adds complexity without value |

## Common Pitfalls

### Pitfall 1: Wrong Delimiter for SANE_NET_HOSTS
**What goes wrong:** Using semicolons instead of colons to separate multiple hosts. SANE silently fails to parse additional hosts.
**Why it happens:** CONTEXT.md D-04 states semicolons, but the SANE specification uses colons.
**How to avoid:** Use colons as delimiter. Document this in code comments referencing sane-net(5).
**Warning signs:** Scanner discovery works for single host but not multiple hosts.

### Pitfall 2: Setting SANE_NET_HOSTS After sane.init()
**What goes wrong:** SANE reads the environment variable at init time. Setting it afterward has no effect.
**Why it happens:** Natural tendency to configure after construction.
**How to avoid:** The `os.environ` assignment MUST come before `sane.init()` in `__init__()`.
**Warning signs:** Host is configured but scanner is not discovered.

### Pitfall 3: Overriding User's External SANE_NET_HOSTS
**What goes wrong:** User sets `SANE_NET_HOSTS` via Docker `-e` or shell, but the application clobbers it with the config value.
**Why it happens:** Unconditional `os.environ["SANE_NET_HOSTS"] = host`.
**How to avoid:** Per D-02, check `"SANE_NET_HOSTS" not in os.environ` before setting.
**Warning signs:** User's Docker environment variable is ignored.

### Pitfall 4: IPv6 Bracket Requirements
**What goes wrong:** IPv6 addresses without brackets are ambiguous with the colon delimiter.
**Why it happens:** IPv6 addresses contain colons, same as the delimiter.
**How to avoid:** Document that IPv6 addresses must be enclosed in brackets (e.g., `[::1]`). Per D-05, don't validate -- let SANE handle errors.
**Warning signs:** IPv6 host not reachable when part of a multi-host list.

## Code Examples

### SaneBackend.__init__() Modification
```python
# Source: sane-net(5) man page + project CONTEXT.md decisions
import os

class SaneBackend(ScannerBackend):
    def __init__(self, host: str = "") -> None:
        """Initialize SANE, optionally configuring network host discovery."""
        _ensure_sane()
        if host and "SANE_NET_HOSTS" not in os.environ:
            os.environ["SANE_NET_HOSTS"] = host
            logger.info("SANE net host discovery configured: %s", host)
        elif "SANE_NET_HOSTS" in os.environ:
            logger.info(
                "SANE_NET_HOSTS already set externally: %s",
                os.environ["SANE_NET_HOSTS"],
            )
        self._sane_version = sane.init()
        logger.info("SANE initialized, version %s", self._sane_version)
```

### CLI Call Site (all 4 identical pattern)
```python
scanner = SaneBackend(host=settings.scanner.host)
```

### docker-compose.yml Addition
```yaml
environment:
  - SANELESS_PAPERLESS__URL=http://paperless:8000
  - SANELESS_PAPERLESS__TOKEN=changeme
  # Uncomment to discover scanners on a remote host via SANE net backend:
  # - SANELESS_SCANNER__HOST=192.168.1.50
  # Multiple hosts use colons: 192.168.1.50:192.168.1.51
```

### Test Pattern (monkeypatch)
```python
def test_sane_backend_sets_sane_net_hosts(
    monkeypatch: pytest.MonkeyPatch,
    mock_sane_module: MockSaneModule,
) -> None:
    """SaneBackend sets SANE_NET_HOSTS when host is provided."""
    monkeypatch.delenv("SANE_NET_HOSTS", raising=False)
    SaneBackend(host="192.168.1.50")
    assert os.environ["SANE_NET_HOSTS"] == "192.168.1.50"

def test_sane_backend_does_not_override_existing_env(
    monkeypatch: pytest.MonkeyPatch,
    mock_sane_module: MockSaneModule,
) -> None:
    """SaneBackend preserves externally set SANE_NET_HOSTS."""
    monkeypatch.setenv("SANE_NET_HOSTS", "external-host")
    SaneBackend(host="config-host")
    assert os.environ["SANE_NET_HOSTS"] == "external-host"

def test_sane_backend_no_host_no_env_change(
    monkeypatch: pytest.MonkeyPatch,
    mock_sane_module: MockSaneModule,
) -> None:
    """SaneBackend with empty host does not set SANE_NET_HOSTS."""
    monkeypatch.delenv("SANE_NET_HOSTS", raising=False)
    SaneBackend()
    assert "SANE_NET_HOSTS" not in os.environ
```

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest (with strict markers and config) |
| Config file | `pyproject.toml` [tool.pytest.ini_options] |
| Quick run command | `uv run pytest tests/test_scanner.py -x -q` |
| Full suite command | `uv run pytest -x` |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| P14-01 | SANE_NET_HOSTS set when host non-empty | unit | `uv run pytest tests/test_scanner.py::TestSaneBackendInit -x` | Needs new tests |
| P14-02 | Env var not overridden when already set | unit | `uv run pytest tests/test_scanner.py::TestSaneBackendInit -x` | Needs new tests |
| P14-03 | Empty host leaves env unchanged | unit | `uv run pytest tests/test_scanner.py::TestSaneBackendInit -x` | Needs new tests |
| P14-04 | CLI commands pass settings.scanner.host | unit | `uv run pytest tests/test_cli.py -x` | Existing tests may need update |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_scanner.py tests/test_cli.py -x -q`
- **Per wave merge:** `uv run pytest -x`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
None -- existing test infrastructure covers all needs. New tests go into existing `TestSaneBackendInit` class in `tests/test_scanner.py`.

## Open Questions

1. **Colon vs. Semicolon Delimiter**
   - What we know: The SANE man page sane-net(5) clearly specifies colons. CONTEXT.md D-04 says semicolons.
   - What's unclear: Whether the user specifically intended semicolons for a reason, or made an error.
   - Recommendation: Use **colons** per the SANE specification. The user's config field `scanner.host` should accept colon-separated lists. Document this correction prominently so the planner flags it. If the user's TOML value contains a single host (the primary use case), the delimiter question is moot.

2. **Logging Level for SANE_NET_HOSTS**
   - What we know: Claude's discretion per CONTEXT.md.
   - Recommendation: Log at INFO level when SANE_NET_HOSTS is set or already exists externally. This is a significant configuration event that operators want to see in normal logs, not just in debug mode.

3. **Test Approach**
   - What we know: Claude's discretion. Existing tests use `monkeypatch.setattr` for the sane module and `monkeypatch.setenv`/`monkeypatch.delenv` for env vars.
   - Recommendation: Use `monkeypatch` for both env var and sane module mocking. This aligns with existing patterns in `test_scanner.py` and `conftest.py`. The `clean_env` autouse fixture already removes `SANELESS_*` vars but not `SANE_NET_HOSTS`, so tests should explicitly manage that var.

## Sources

### Primary (HIGH confidence)
- [sane-net(5) Arch man page](https://man.archlinux.org/man/extra/sane/sane-net.5.en) -- SANE_NET_HOSTS delimiter is colon, IPv6 uses brackets
- [sane-net(5) Debian man page](https://manpages.debian.org/bookworm/libsane-common/sane-net.5.en.html) -- confirms colon-separated list
- Source code review of `src/saneless/scanner/sane_backend.py`, `src/saneless/cli.py`, `src/saneless/config.py`, `tests/test_scanner.py`, `tests/conftest.py`

### Secondary (MEDIUM confidence)
- [SANE ArchWiki](https://wiki.archlinux.org/title/SANE) -- general SANE network setup patterns

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - stdlib only, no new deps
- Architecture: HIGH - straightforward env var injection, all code reviewed
- Pitfalls: HIGH - delimiter correction verified against official SANE man pages

**Research date:** 2026-03-22
**Valid until:** 2026-04-22 (stable domain, SANE spec rarely changes)
