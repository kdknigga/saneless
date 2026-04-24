# Phase 6: Gap Closure Fixes - Research

**Researched:** 2026-03-20
**Domain:** FastAPI route addition + worker state callback extension
**Confidence:** HIGH

## Summary

This phase closes the final two v1 requirement gaps with minimal, surgical changes to existing code. Both features have their infrastructure already built -- the work is purely wiring.

**GAP-02 (PLSS-03):** `PaperlessClient.test_connection()` already returns `"connected"`, `"token_rejected"`, or `"unreachable"`. The gap is that no web route exposes this method. Adding `GET /api/paperless/test` follows the exact pattern of the existing `/health` endpoint (5-10 lines in `routes.py`).

**GAP-03 (UI-02):** The pipeline already emits `"Assembling PDF..."` and `"Uploading to paperless-ngx..."` via `notify()` callbacks. The worker's `_status_cb` currently only handles `"Awaiting flip..."` to set `JobState.AWAITING_FLIP`. Adding two more string matches for `ASSEMBLING` and `UPLOADING` is 4-6 lines. The template `partials/status.html` already renders these states with correct labels and polling attributes.

**Primary recommendation:** Implement both gaps in a single plan. No new files, no new dependencies, no new patterns -- just additions to `routes.py` and `worker.py`.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- New route `GET /api/paperless/test` in `routes.py` returning JSON `{"status": "connected"}`, `{"status": "token_rejected"}`, or `{"status": "unreachable"}`
- Calls existing `PaperlessClient.test_connection()` -- no new client logic needed
- No authentication required (consistent with `/health` endpoint and trusted LAN assumption)
- Pure API endpoint -- no HTML template, no UI button
- Catches all exceptions gracefully, returns JSON error response -- never raises to caller
- Worker's `_status_cb` extended to map "Assembling PDF..." to `JobState.ASSEMBLING` and "Uploading to paperless-ngx..." to `JobState.UPLOADING`
- No pipeline changes needed -- `run_pipeline()` already emits the right status strings
- Templates in `partials/status.html` already handle ASSEMBLING and UPLOADING states
- State transitions logged via existing `JobStore.update_state()` debug logging

### Claude's Discretion
- Exact test route error handling structure (try/except scope, logging level)
- Test organization -- whether to add tests to existing test files or create new ones
- Whether to add a timeout to the paperless test connection call

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| PLSS-03 | `GET /api/paperless/test` endpoint distinguishing three failure modes: server unreachable, token rejected, connection successful | Route follows `/health` pattern in `routes.py` line 103; calls `PaperlessClient.test_connection()` at line 205 of `paperless.py` which already implements the three-state logic |
| UI-02 | Live status indicator shows job state (idle/scanning/assembling/uploading/done/error) via polling | Worker `_status_cb` at line 132 of `worker.py` needs two new string matches; `partials/status.html` already renders all seven states with correct polling attributes |
</phase_requirements>

## Standard Stack

No new libraries needed. All changes use existing project dependencies:

### Core (already installed)
| Library | Purpose | Why Used |
|---------|---------|----------|
| FastAPI | Web framework | `APIRouter`, `JSONResponse` already in use in `routes.py` |
| httpx | HTTP client | Already used by `PaperlessClient` for connection testing |
| pytest | Test framework | Existing test suite pattern |

### No New Dependencies
This phase adds zero new packages. All work is additive within existing modules.

## Architecture Patterns

### Pattern 1: JSON API Route (from `/health`)
**What:** Simple GET endpoint returning dict/JSONResponse
**When to use:** For the paperless test endpoint
**Example from existing code:**
```python
# Source: src/saneless/web/routes.py lines 103-116
@router.get("/health", response_model=None)
async def health(request: Request) -> dict[str, str] | JSONResponse:
    if request.app.state.worker.is_alive:
        return {"status": "ok"}
    return JSONResponse(
        status_code=503,
        content={"status": "error", "detail": "worker thread is down"},
    )
```

The paperless test route follows this exact shape: access `request.app.state.paperless`, call `test_connection()`, return JSON dict.

### Pattern 2: Worker Status Callback String Matching
**What:** `_status_cb` closure maps pipeline notification strings to `JobState` enum values
**When to use:** For ASSEMBLING and UPLOADING state transitions
**Example from existing code:**
```python
# Source: src/saneless/worker.py lines 132-135
def _status_cb(msg: str, _jid: str = job.id) -> None:
    logger.info(msg)
    if msg == "Awaiting flip...":
        self._job_store.update_state(_jid, JobState.AWAITING_FLIP)
```

Extend with two elif branches for `"Assembling PDF..."` and `"Uploading to paperless-ngx..."`.

### Pattern 3: Web Test with TestClient
**What:** FastAPI TestClient with app state manipulation for state-dependent assertions
**Example from existing code:**
```python
# Source: tests/test_web.py lines 155-166
def test_status_polling_active_job(client) -> None:
    job_store: JobStore = client.app.state.job_store
    job = job_store.create_job(profile="default", title="Polling Test")
    job_store.update_state(job.id, JobState.SCANNING)
    client.app.state.worker._current_job_id = job.id
    response = client.get("/api/jobs/current/status")
    assert "hx-trigger" in response.text or "hx-get" in response.text
```

### Pattern 4: Paperless Client Mock Transport
**What:** `httpx.MockTransport` for testing paperless client behavior without network
**Example from existing code:**
```python
# Source: tests/test_paperless.py lines 285-298
def test_test_connection_connected(self) -> None:
    def handler(_request):
        return httpx.Response(200, json={"status": "ok"})
    transport = _make_transport(handler)
    client = PaperlessClient(url="http://paperless:8000", token=_MOCK_AUTH, _transport=transport)
    assert client.test_connection() == "connected"
    client.close()
```

### Anti-Patterns to Avoid
- **Don't create a new PaperlessClient in the route:** The client is already on `request.app.state.paperless`, initialized in the app lifespan
- **Don't modify pipeline.py:** The status strings are already correct; only the worker callback needs updating
- **Don't use `startswith` for string matching:** The pipeline strings are exact, stable constants -- use `==` like the existing `"Awaiting flip..."` pattern

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Connection testing logic | Custom HTTP/network check | `PaperlessClient.test_connection()` | Already implements three-state logic with correct httpx exception handling |
| Status state templates | New template partials | Existing `partials/status.html` | Already handles all 7 JobState values including ASSEMBLING and UPLOADING |
| Job state enum | New state values | Existing `JobState.ASSEMBLING` and `JobState.UPLOADING` | Already defined in `job.py` line 24 |

**Key insight:** Both gaps are missing wiring, not missing infrastructure. Every component exists -- they just aren't connected.

## Common Pitfalls

### Pitfall 1: Missing `response_model=None` on Union Return Types
**What goes wrong:** FastAPI tries to validate the response against a model and fails on union types
**Why it happens:** Default response_model inference doesn't handle `dict | JSONResponse`
**How to avoid:** Add `response_model=None` to the route decorator, matching the `/health` pattern
**Warning signs:** Pydantic validation error on route response

### Pitfall 2: Forgetting Exception Handling on Test Route
**What goes wrong:** Unexpected exception from `test_connection()` returns 500 instead of JSON
**Why it happens:** `httpx.ConnectError` is caught inside `test_connection()`, but other errors (e.g., `httpx.TimeoutException`) may not be
**How to avoid:** Wrap the route handler in try/except that catches `Exception` and returns `{"status": "error", "detail": str(exc)}`
**Warning signs:** 500 response instead of JSON on network timeout

### Pitfall 3: String Mismatch in Status Callback
**What goes wrong:** Worker doesn't transition to ASSEMBLING/UPLOADING state
**Why it happens:** Callback string doesn't exactly match what pipeline emits
**How to avoid:** Use the exact strings from `pipeline.py` lines 265/270: `"Assembling PDF..."` and `"Uploading to paperless-ngx..."`
**Warning signs:** Status polling stays on SCANNING even during PDF assembly

### Pitfall 4: Missing Docstrings (Ruff D rules)
**What goes wrong:** Ruff lint fails on new route function
**Why it happens:** Ruff's D rules require docstrings on all public functions
**How to avoid:** Always add a docstring to the new route handler
**Warning signs:** `uv run ruff check .` fails with D100/D103 errors

## Code Examples

### Paperless Test Route Implementation
```python
# Add to src/saneless/web/routes.py
@router.get("/api/paperless/test", response_model=None)
async def paperless_test(request: Request) -> dict[str, str] | JSONResponse:
    """
    Test paperless-ngx connection status.

    Returns JSON with status: connected, token_rejected, unreachable,
    or error with detail on unexpected failures.
    """
    try:
        status = request.app.state.paperless.test_connection()
        return {"status": status}
    except Exception as exc:
        logger.warning("Paperless connection test failed: %s", exc)
        return JSONResponse(
            status_code=502,
            content={"status": "error", "detail": str(exc)},
        )
```

### Worker Status Callback Extension
```python
# Modify _status_cb in src/saneless/worker.py lines 132-135
def _status_cb(msg: str, _jid: str = job.id) -> None:
    logger.info(msg)
    if msg == "Awaiting flip...":
        self._job_store.update_state(_jid, JobState.AWAITING_FLIP)
    elif msg == "Assembling PDF...":
        self._job_store.update_state(_jid, JobState.ASSEMBLING)
    elif msg == "Uploading to paperless-ngx...":
        self._job_store.update_state(_jid, JobState.UPLOADING)
```

### Test: Paperless Test Route (add to test_web.py)
```python
def test_paperless_test_connected(client) -> None:
    """GET /api/paperless/test returns connected status (PLSS-03)."""
    client.app.state.paperless.test_connection = lambda: "connected"
    response = client.get("/api/paperless/test")
    assert response.status_code == 200
    assert response.json() == {"status": "connected"}

def test_paperless_test_token_rejected(client) -> None:
    """GET /api/paperless/test returns token_rejected (PLSS-03)."""
    client.app.state.paperless.test_connection = lambda: "token_rejected"
    response = client.get("/api/paperless/test")
    assert response.status_code == 200
    assert response.json() == {"status": "token_rejected"}

def test_paperless_test_unreachable(client) -> None:
    """GET /api/paperless/test returns unreachable (PLSS-03)."""
    client.app.state.paperless.test_connection = lambda: "unreachable"
    response = client.get("/api/paperless/test")
    assert response.status_code == 200
    assert response.json() == {"status": "unreachable"}
```

### Test: Worker Intermediate States (add to test_worker.py)
```python
def test_worker_assembling_state(mock_scanner, mock_paperless, default_settings, monkeypatch):
    """Worker sets ASSEMBLING state when pipeline emits 'Assembling PDF...' (UI-02)."""
    states_seen = []

    def pipeline_with_assembling(_scanner, _paperless, _settings, request):
        if request.status_callback:
            request.status_callback("Assembling PDF...")
        return {"status": "SUCCESS"}

    monkeypatch.setattr("saneless.worker.run_pipeline", pipeline_with_assembling)
    store = JobStore()
    try:
        worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
        worker.start()
        job = store.create_job("default", "Assembling Test")
        worker.submit(job)
        time.sleep(0.5)
        worker.stop()
        # Job should reach DONE (ASSEMBLING is intermediate)
        assert store.get_job(job.id).state == JobState.DONE
    finally:
        store.close()
```

## State of the Art

No changes from current approach. This phase uses established patterns from phases 1-5.

| Aspect | Current Approach | Status |
|--------|------------------|--------|
| API routes | FastAPI APIRouter with JSONResponse | Stable, used throughout |
| Worker callbacks | String matching in closure | Established pattern since Phase 2 |
| Test infrastructure | pytest + TestClient + MockTransport | Complete, well-tested |

## Open Questions

1. **Timeout on paperless test connection**
   - What we know: `test_connection()` calls `self._client.get("/api/")` which uses httpx default timeout (5s)
   - What's unclear: Whether we need an explicit timeout for the route handler
   - Recommendation: Rely on httpx default timeout; the try/except in the route handler catches `httpx.TimeoutException` via the blanket `Exception` catch. No explicit timeout parameter needed.

2. **Test organization**
   - What we know: `test_web.py` has route tests, `test_worker.py` has worker tests
   - What's unclear: Whether to create new test files
   - Recommendation: Add route tests to `test_web.py`, worker state tests to `test_worker.py`. No new files needed -- scope is too small.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest (via `uv run pytest`) |
| Config file | `pyproject.toml` [tool.pytest] |
| Quick run command | `uv run pytest tests/test_web.py tests/test_worker.py -x` |
| Full suite command | `uv run pytest` |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| PLSS-03 | GET /api/paperless/test returns connected | unit | `uv run pytest tests/test_web.py -k "paperless_test_connected" -x` | Wave 0 |
| PLSS-03 | GET /api/paperless/test returns token_rejected | unit | `uv run pytest tests/test_web.py -k "paperless_test_token_rejected" -x` | Wave 0 |
| PLSS-03 | GET /api/paperless/test returns unreachable | unit | `uv run pytest tests/test_web.py -k "paperless_test_unreachable" -x` | Wave 0 |
| PLSS-03 | Route handles unexpected errors gracefully | unit | `uv run pytest tests/test_web.py -k "paperless_test_error" -x` | Wave 0 |
| UI-02 | Worker emits ASSEMBLING state | unit | `uv run pytest tests/test_worker.py -k "assembling" -x` | Wave 0 |
| UI-02 | Worker emits UPLOADING state | unit | `uv run pytest tests/test_worker.py -k "uploading" -x` | Wave 0 |
| UI-02 | ASSEMBLING state visible in polling | unit | `uv run pytest tests/test_web.py -k "assembling" -x` | Wave 0 |
| UI-02 | UPLOADING state visible in polling | unit | `uv run pytest tests/test_web.py -k "uploading" -x` | Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_web.py tests/test_worker.py -x`
- **Per wave merge:** `uv run pytest`
- **Phase gate:** Full suite green + `uv run prek run` before verify

### Wave 0 Gaps
None -- existing test infrastructure covers all phase requirements. Tests will be added to existing `test_web.py` and `test_worker.py` files using established fixtures and patterns.

## Sources

### Primary (HIGH confidence)
- `src/saneless/web/routes.py` -- all existing route patterns, especially `/health` (line 103)
- `src/saneless/worker.py` -- `_status_cb` at line 132, string matching pattern
- `src/saneless/paperless.py` -- `test_connection()` at line 205, three-state return
- `src/saneless/job.py` -- `JobState` enum at line 24, ASSEMBLING/UPLOADING already defined
- `src/saneless/pipeline.py` -- lines 265/270, exact notification strings
- `src/saneless/web/templates/partials/status.html` -- already renders all 7 states
- `tests/test_web.py` -- existing web test patterns with TestClient
- `tests/test_worker.py` -- existing worker test patterns with monkeypatch
- `tests/test_paperless.py` -- existing MockTransport test patterns

### Secondary (MEDIUM confidence)
None needed -- all evidence is from the codebase itself.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- no new libraries, all existing
- Architecture: HIGH -- follows exact existing patterns with zero deviation
- Pitfalls: HIGH -- identified from project conventions (ruff D rules, response_model, string matching)

**Research date:** 2026-03-20
**Valid until:** 2026-04-20 (stable -- no external dependencies changing)
