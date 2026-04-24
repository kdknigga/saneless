# Phase 7: Tech Debt Cleanup - Research

**Researched:** 2026-03-21
**Domain:** Python packaging, Starlette deprecation, error typing, threading synchronization, Playwright browser testing
**Confidence:** HIGH

## Summary

This phase addresses 5 tech debt items from the v1.0 milestone audit. Research reveals one item is **already resolved** (TemplateResponse deprecation was fixed in commit `a5c7f1e`), reducing the workload to 4 items. The remaining items are well-scoped: python-sane optional dependency packaging, ErrorCategory enum addition, flip timing synchronization via threading.Event, and Playwright browser tests.

All changes are surgical -- no new features, no architectural changes. The exception hierarchy already exists and maps cleanly to error categories. The flip timing fix extends an existing threading.Event pattern. Playwright browser tests follow established test patterns with `create_app()` factory and `StubScanner`.

**Primary recommendation:** Verify TemplateResponse deprecation is already fixed (zero warnings in test output), then focus on the 4 remaining items. Use `pytest-playwright` for browser test fixtures rather than hand-rolling Playwright setup.

<user_constraints>

## User Constraints (from CONTEXT.md)

### Locked Decisions
- `python-sane` as optional dependency group: `[project.optional-dependencies] sane = ["python-sane"]`
- Dockerfile installs the `[sane]` extra
- Lazy import pattern in `sane_backend.py` remains
- Add `ErrorCategory` enum to `job.py`: FEEDER, CONFIG, SCANNER, UPLOAD, UNKNOWN
- Add `error_category` optional field to `Job` dataclass
- Worker catches specific exception types before bare `except Exception`
- Use `threading.Event` for flip timing synchronization (not busy-wait)
- Route `POST /api/flip/continue` waits on event with timeout (e.g., 2s)
- New `tests/test_browser.py` with Playwright fixtures
- Tests use `create_app()` factory with `StubScanner`

### Claude's Discretion
- Exact TemplateResponse signature migration (whether to use positional or keyword args for name)
- Playwright test fixture design (session-scoped server vs function-scoped)
- Whether ErrorCategory needs to be persisted to SQLite job_store or kept in-memory only
- Uvicorn server startup approach in Playwright fixtures (subprocess vs threading)

### Deferred Ideas (OUT OF SCOPE)
- Physical scanner verification (tech debt item 6) -- requires real hardware
- Nyquist compliance for phases 1, 4, 5, 6 -- separate validation runs

</user_constraints>

<phase_requirements>

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| PKG-01 | pip-installable Python package (pyproject.toml, published to PyPI) | Optional dependency `[sane]` extra makes python-sane installable without requiring it for base package |
| UI-01 | Web UI accessible from any browser on LAN with profile selector, metadata fields, and Scan button | Playwright browser tests verify real browser rendering of PicoCSS/HTMX UI |
| UI-02 | Live status indicator shows job state via polling | Playwright tests verify HTMX polling updates status area |
| UI-03 | For manual duplex, UI shows awaiting_flip state with flip prompt, Continue and Cancel buttons | Playwright tests verify flip prompt UI renders correctly |
| ARCH-02 | Background worker thread with queue.Queue for job coordination | Error categorization and flip timing improvements enhance worker reliability |

</phase_requirements>

## Standard Stack

### Core (already in project)
| Library | Version | Purpose | Status |
|---------|---------|---------|--------|
| fastapi | 0.135.1 | Web framework | Installed |
| starlette | 0.52.1 | ASGI framework (via FastAPI) | Installed |
| python-sane | 2.9.2 | SANE scanner bindings | To add as optional dep |
| playwright | 1.58.0+ | Browser automation | Installed (dev) |

### To Add
| Library | Version | Purpose | Why |
|---------|---------|---------|-----|
| pytest-playwright | 0.7.2 | Pytest fixtures for Playwright (page, browser, context) | Provides `page` fixture, browser lifecycle management, headless defaults |

### Not Needed
| Instead of | Why Not |
|------------|---------|
| Custom browser test fixtures | pytest-playwright provides battle-tested `page`, `browser_context`, `browser` fixtures |
| selenium | Playwright is already a dev dependency, project convention uses Playwright MCP |

**Installation:**
```bash
uv add --dev pytest-playwright
uv run playwright install chromium
```

## Architecture Patterns

### TemplateResponse Deprecation -- ALREADY FIXED

**Critical finding:** Commit `a5c7f1e` ("fix(web): replace deprecated Starlette and asynccontextmanager signatures") already migrated all TemplateResponse calls to the new signature. The current code uses `TemplateResponse(request, "name.html", {context})` which is the new-style pattern.

Verification: running `uv run pytest tests/test_web.py -W all 2>&1 | grep -i deprecat` produces zero output. No deprecation warnings in current code.

**Recommendation:** Verify this during implementation. If zero warnings confirmed, skip this item entirely. The audit was written before the fix was applied.

### Error Category Pattern

Add `ErrorCategory` enum alongside `JobState` in `job.py`:

```python
class ErrorCategory(StrEnum):
    """Categories of errors for programmatic handling."""
    FEEDER = "FEEDER"
    CONFIG = "CONFIG"
    SCANNER = "SCANNER"
    UPLOAD = "UPLOAD"
    UNKNOWN = "UNKNOWN"
```

Exception-to-category mapping in worker:
```python
from saneless.exceptions import (
    ConfigError, FeederEmptyError, PaperlessError, ScanError
)

try:
    run_pipeline(...)
    self._job_store.update_state(job.id, JobState.DONE)
except FeederEmptyError as exc:
    self._job_store.update_state(
        job.id, JobState.ERROR, error=str(exc),
        error_category=ErrorCategory.FEEDER,
    )
except ConfigError as exc:
    self._job_store.update_state(
        job.id, JobState.ERROR, error=str(exc),
        error_category=ErrorCategory.CONFIG,
    )
except ScanError as exc:
    self._job_store.update_state(
        job.id, JobState.ERROR, error=str(exc),
        error_category=ErrorCategory.SCANNER,
    )
except PaperlessError as exc:
    self._job_store.update_state(
        job.id, JobState.ERROR, error=str(exc),
        error_category=ErrorCategory.UPLOAD,
    )
except Exception as exc:
    self._job_store.update_state(
        job.id, JobState.ERROR, error=str(exc),
        error_category=ErrorCategory.UNKNOWN,
    )
```

**SQLite persistence decision (Claude's discretion):** Persist `error_category` to SQLite. The column is cheap (TEXT, nullable), enables future error analytics, and avoids losing category info on app restart. Add an `error_category` column to the jobs table schema.

### Flip Timing Synchronization

Current flow (timing gap):
1. User clicks Continue
2. `POST /api/flip/continue` calls `worker.continue_flip()` which sets `_flip_event`
3. Route immediately reads job state from store -- still shows AWAITING_FLIP
4. Response shows stale state; 1s polling catches up

Fixed flow:
1. User clicks Continue
2. `POST /api/flip/continue` calls `worker.continue_flip()`
3. Route waits on a **transition event** with 2s timeout
4. Worker sets the transition event after updating state to SCANNING
5. Route reads job state -- now shows SCANNING

Implementation: Add a `_transition_event: threading.Event` to the worker. The route waits on this event. The worker sets it after `update_state(_jid, JobState.SCANNING)` in the `_status_cb` when transitioning from AWAITING_FLIP.

```python
# In ScanWorker.__init__:
self._transition_event = threading.Event()

# In continue_flip route:
state.worker.continue_flip()
state.worker.wait_transition(timeout=2.0)  # New method
job = state.job_store.get_job(state.worker.current_job_id)

# In worker._status_cb, after setting SCANNING state:
self._transition_event.set()

# In worker.wait_transition:
def wait_transition(self, timeout: float = 2.0) -> bool:
    result = self._transition_event.wait(timeout=timeout)
    self._transition_event.clear()
    return result
```

### Playwright Browser Test Pattern

**Fixture approach (Claude's discretion):** Use session-scoped server with function-scoped pages. Starting a uvicorn server per test is expensive (~200ms). A session-scoped server with function-scoped browser contexts gives isolation without startup cost.

**Server startup (Claude's discretion):** Use threading (not subprocess). The `create_app()` factory returns a FastAPI app that can run in a thread via `uvicorn.Server`. This avoids subprocess management, port discovery, and process cleanup complexity.

```python
import threading
import uvicorn
import pytest
from saneless.web.app import create_app

@pytest.fixture(scope="session")
def app_server(tmp_path_factory):
    """Start a real uvicorn server for browser tests."""
    # Create settings, StubScanner, app
    settings = _test_settings(tmp_path_factory.mktemp("browser"))
    scanner = StubScanner()
    app = create_app(settings, scanner)

    config = uvicorn.Config(app, host="127.0.0.1", port=0)
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # Wait for server to be ready
    while not server.started:
        pass
    # Get actual port
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)
```

**Test markers:** Use `@pytest.mark.browser` to allow selective test execution. Note: `pytest-playwright` uses markers internally but a custom marker provides flexibility.

### Optional Dependency Pattern

pyproject.toml addition:
```toml
[project.optional-dependencies]
sane = ["python-sane>=2.9.1"]
```

Dockerfile change:
```dockerfile
# Current:
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl
# Fixed:
RUN pip install --no-cache-dir '/tmp/saneless-*.whl[sane]' && rm /tmp/*.whl
```

The glob with brackets needs quoting in the shell to prevent globbing issues. Use single quotes around the pattern.

### Anti-Patterns to Avoid
- **Catching FeederEmptyError after ScanError:** FeederEmptyError is a subclass of ScanError. The more specific exception MUST come first in the except chain.
- **Busy-wait for flip transition:** Never poll in a loop. Use threading.Event.wait() with timeout.
- **Starting uvicorn server per test function:** Session-scoped server with function-scoped browser contexts is the correct pattern.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Browser test fixtures | Custom playwright setup/teardown | pytest-playwright `page` fixture | Handles browser lifecycle, headless mode, context isolation |
| Exception ordering | Manual if/isinstance chains | Ordered except clauses | Python's native exception handling already does this correctly |
| Server for browser tests | Raw socket server or TestClient | uvicorn.Server in thread | TestClient (httpx) doesn't serve real HTTP for browser to connect to |

## Common Pitfalls

### Pitfall 1: Exception Subclass Ordering
**What goes wrong:** `except ScanError` catches `FeederEmptyError` before the `except FeederEmptyError` clause ever runs
**Why it happens:** FeederEmptyError inherits from ScanError
**How to avoid:** Always put FeederEmptyError BEFORE ScanError in except chain
**Warning signs:** All scan errors categorized as SCANNER, never FEEDER

### Pitfall 2: Uvicorn Port 0 Actual Port Discovery
**What goes wrong:** Cannot connect to server because you don't know what port was assigned
**Why it happens:** `port=0` means OS assigns a random free port
**How to avoid:** After server starts, read `server.servers[0].sockets[0].getsockname()[1]` for actual port
**Warning signs:** Connection refused in browser tests

### Pitfall 3: Threading Event Race Condition
**What goes wrong:** Transition event is set before route starts waiting, so route misses it and times out
**Why it happens:** Worker thread may transition faster than the route can set up its wait
**How to avoid:** Clear the event BEFORE calling continue_flip(), not after. Or use a fresh Event per flip.
**Warning signs:** Flip continue always times out despite worker progressing

### Pitfall 4: Dockerfile Glob with Brackets
**What goes wrong:** Shell interprets `[sane]` as a character class glob pattern
**Why it happens:** `[sane]` looks like `[saen]` glob to bash
**How to avoid:** Quote the entire path: `'/tmp/saneless-*.whl[sane]'`
**Warning signs:** "No matching distribution found" during docker build

### Pitfall 5: SQLite Schema Migration
**What goes wrong:** Existing databases don't have the new `error_category` column
**Why it happens:** CREATE TABLE IF NOT EXISTS won't add columns to existing tables
**How to avoid:** Use `ALTER TABLE jobs ADD COLUMN error_category TEXT` with a try/except for "duplicate column" error, OR check if column exists first
**Warning signs:** OperationalError when updating error_category on existing installs

## Code Examples

### Current TemplateResponse Usage (ALREADY CORRECT)
```python
# Source: src/saneless/web/routes.py line 90-100 (current code)
return state.templates.TemplateResponse(
    request,          # First arg: Request (new style)
    "index.html",     # Second arg: template name
    {"profiles": profiles, "tags": tags, ...},  # Third arg: context
)
```

### Exception Hierarchy
```python
# Source: src/saneless/exceptions.py
SanelessError
├── ConfigError          → ErrorCategory.CONFIG
├── ScanError            → ErrorCategory.SCANNER
│   └── FeederEmptyError → ErrorCategory.FEEDER  (MUST catch first!)
└── PaperlessError       → ErrorCategory.UPLOAD
```

### Worker Except Block (lines 159-165, to be modified)
```python
# Source: src/saneless/worker.py
# Current: bare except catches all
except Exception as exc:
    self._job_store.update_state(job.id, JobState.ERROR, error=str(exc))

# Target: typed catches with categories
except FeederEmptyError as exc:
    self._job_store.update_state(job.id, JobState.ERROR, error=str(exc),
                                 error_category=ErrorCategory.FEEDER)
except ConfigError as exc:
    ...
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `TemplateResponse(name, {"request": request})` | `TemplateResponse(request, name, context)` | Starlette 0.38+ | Already fixed in this codebase |
| `[project.optional-dependencies]` with pip | Same syntax, uv supports natively | uv 0.1+ | `uv add --optional sane python-sane` or manual edit |
| Manual Playwright lifecycle | pytest-playwright 0.7+ fixtures | 2025 | `page` fixture handles all setup |

## Open Questions

1. **TemplateResponse: confirm already fixed**
   - What we know: Commit a5c7f1e fixed it, zero warnings in test output
   - What's unclear: Whether the audit caught a genuine issue that was since fixed, or was wrong
   - Recommendation: Verify during implementation, skip if confirmed fixed

2. **pytest-playwright vs raw Playwright**
   - What we know: CLAUDE.md says "use Playwright MCP for browser validation"
   - What's unclear: Whether pytest-playwright test files satisfy the "Playwright MCP" requirement or if MCP verification is also needed
   - Recommendation: Use pytest-playwright for repeatable test files; Playwright MCP is for ad-hoc verification during development. Both serve different purposes.

3. **ErrorCategory SQLite migration strategy**
   - What we know: JobStore uses CREATE TABLE IF NOT EXISTS (no migration framework)
   - What's unclear: Whether existing databases need migration or if this is greenfield enough to just add the column to CREATE TABLE
   - Recommendation: Add column to CREATE TABLE statement AND add ALTER TABLE with try/except for existing databases

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.0.2 |
| Config file | `pyproject.toml` [tool.pytest.ini_options] |
| Quick run command | `uv run pytest tests/test_job.py tests/test_worker.py tests/test_web.py -x` |
| Full suite command | `uv run pytest` |

### Phase Requirements to Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| PKG-01 | python-sane as optional dep, Dockerfile installs correctly | unit | `uv run pytest tests/test_job.py -x` (verify no import of python-sane at import time) | Partial -- needs Dockerfile verification |
| UI-01 | Web UI renders in real browser | browser/e2e | `uv run pytest tests/test_browser.py -x` | Wave 0 |
| UI-02 | Live status polling works | browser/e2e | `uv run pytest tests/test_browser.py::test_status_polling -x` | Wave 0 |
| UI-03 | Flip prompt renders with buttons | browser/e2e | `uv run pytest tests/test_browser.py::test_flip_prompt -x` | Wave 0 |
| ARCH-02 | Worker typed error categories | unit | `uv run pytest tests/test_worker.py -x` | Exists -- needs new test cases |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_job.py tests/test_worker.py tests/test_web.py -x`
- **Per wave merge:** `uv run pytest`
- **Phase gate:** Full suite green before verify

### Wave 0 Gaps
- [ ] `tests/test_browser.py` -- new file for Playwright browser tests (covers UI-01, UI-02, UI-03)
- [ ] `pytest-playwright` dev dependency -- `uv add --dev pytest-playwright`
- [ ] Playwright chromium browser -- `uv run playwright install chromium`
- [ ] New test cases in `tests/test_worker.py` for error category propagation

## Sources

### Primary (HIGH confidence)
- Direct code inspection of `src/saneless/web/routes.py`, `src/saneless/worker.py`, `src/saneless/job.py`, `src/saneless/exceptions.py`
- Starlette 0.52.1 source (`Jinja2Templates.TemplateResponse`) -- verified deprecation logic
- git log showing commit `a5c7f1e` fixing TemplateResponse deprecation
- PyPI `python-sane` 2.9.2 -- verified version and Python requirement (>=3.8)
- PyPI `pytest-playwright` 0.7.2 -- verified latest version
- `pyproject.toml` and `Dockerfile` -- verified current state

### Secondary (MEDIUM confidence)
- [uv optional dependencies docs](https://docs.astral.sh/uv/concepts/projects/dependencies/)
- [pytest-playwright PyPI](https://pypi.org/project/pytest-playwright/)
- [Playwright Python test runners](https://playwright.dev/python/docs/test-runners)

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- all libraries verified against PyPI and current codebase
- Architecture: HIGH -- patterns derived from direct code inspection of existing patterns
- Pitfalls: HIGH -- exception hierarchy verified in source, threading patterns standard Python
- TemplateResponse fix: HIGH -- verified via code inspection and test output

**Research date:** 2026-03-21
**Valid until:** 2026-04-21 (stable domain, no fast-moving dependencies)
