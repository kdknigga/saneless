# Phase 3: Web UI - Research

**Researched:** 2026-03-20
**Domain:** FastAPI + Jinja2 + HTMX web application with server-side rendering
**Confidence:** HIGH

## Summary

Phase 3 builds the web UI for saneless using FastAPI as the web framework, Jinja2 for server-side templates, HTMX for dynamic interactions (no JS build step), and PicoCSS for styling. The existing codebase already provides the core infrastructure: `JobStore` with SQLite persistence, `ScanWorker` with queue-based job submission and flip event coordination, `PaperlessClient` for uploads, and `Settings` with profile definitions.

Key gaps that must be filled: (1) `JobStore` needs `list_recent()` and `prune()` methods for job history, (2) `PaperlessClient` needs `get_tags()` and `get_correspondents()` methods for dropdown population, (3) `Settings.output` needs a `paperless_cache_ttl_seconds` config field, (4) `ScanWorker` needs an `is_alive` property for the health endpoint, and (5) the entire FastAPI application layer (routes, templates, static assets) must be created from scratch.

**Primary recommendation:** Build the FastAPI app as a `src/saneless/web/` subpackage with separate modules for routes (app factory, API endpoints, page routes), templates (Jinja2 with HTMX partials), and a thin caching layer for paperless metadata. Use HTMX polling with `hx-trigger="every 1s"` for live status, conditionally enabled only during active jobs.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- Jinja2 server-side templates + HTMX for dynamic behavior -- no client-side SPA, no JS build step
- Single-page layout: all UI (scan form, status area, job history) on one page
- HTMX handles all dynamic interactions: form submission, status polling, dropdown refresh, flip prompt actions
- Status polling via `hx-trigger="every 1s"` on the status area during active scan; no polling when idle
- Tag/correspondent dropdowns use `hx-get` for initial population and per-resource refresh
- Flip prompt rendered as an HTMX-swapped partial with Continue and Cancel buttons
- Flip illustration as inline SVG embedded in the template
- PicoCSS as the CSS foundation -- classless/minimal, auto dark/light mode
- Responsive from the start -- single-column layout on mobile
- Live status indicator with spinner/checkmark/X visual indicators
- Thumbnail appears inline in status area as base64 JPEG
- Job history as simple HTML table, most recent first, refreshed via HTMX after job completion
- `GET /health` returns 200/503 JSON, no authentication
- PRD specifies exact flip prompt wording

### Claude's Discretion
- Exact HTMX attribute patterns and swap strategies
- PicoCSS customization variables (accent color, border radius)
- SVG flip illustration design details
- Polling conditional logic implementation (how to start/stop polling based on job state)
- FastAPI route organization (single router vs multiple)
- Jinja2 template structure (single file vs partials)
- Status area transition animations (if any)
- Job history table column widths and date formatting

### Deferred Ideas (OUT OF SCOPE)
None -- discussion stayed within phase scope.
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| UI-01 | Web UI accessible from any browser on the LAN with profile selector, metadata fields, and Scan button | FastAPI + Jinja2 templates, PicoCSS styling, form with profile dropdown and metadata fields |
| UI-02 | Live status indicator shows job state via polling | HTMX `hx-trigger="every 1s"` on status partial, conditional polling via HX-Trigger response header |
| UI-03 | Manual duplex shows flip prompt with Continue and Cancel buttons | HTMX-swapped partial when job state is AWAITING_FLIP, POST endpoints for continue/abort |
| UI-04 | First-page thumbnail displayed in status area | Base64 JPEG from JobStore.thumbnail rendered as `<img src="data:image/jpeg;base64,...">` |
| UI-05 | Job history table with timestamp, profile, title, status | New `JobStore.list_recent()` method, HTML table partial refreshed via HTMX |
| UI-06 | Job history pruned by age (7 days) and count (500 rows) | New `JobStore.prune()` method called at startup and periodically |
| UI-07 | Scan button disabled while job is in progress | HTMX swap replaces form area; button re-enabled when status returns to idle/done/error |
| UI-08 | Refresh icon on tag/correspondent dropdowns | Per-resource cache invalidation endpoint, HTMX `hx-get` to re-fetch dropdown options |
| PROF-03 | User can select a profile from dropdown in web UI | `Settings.profiles` keys populate `<select>` element |
| PLSS-04 | User can set title, tags, correspondent before scanning | Form fields; new `PaperlessClient.get_tags()` and `get_correspondents()` methods |
| PLSS-05 | Tag/correspondent lists cached with TTL and per-resource manual refresh | In-memory cache with configurable TTL (default 60s), per-resource invalidation |
| HLTH-01 | `GET /health` returns 200 when healthy, 503 when worker down | Check `ScanWorker._thread.is_alive()`, expose as property |
| HLTH-02 | Health endpoint requires no authentication | Separate route, no middleware |
| LOG-03 | All errors displayed in web UI for current/most recent job | Job.error field already exists; render in status area partial |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| FastAPI | 0.135.1 | Web framework | PRD specifies FastAPI; async-capable, built-in validation, Jinja2 integration |
| uvicorn | 0.42.0 | ASGI server | Standard FastAPI production server |
| Jinja2 | 3.1.6 | Server-side templates | FastAPI native support via `Jinja2Templates`, no build step |
| python-multipart | 0.0.22 | Form data parsing | Required by FastAPI for form submissions |

### Frontend (CDN, no install)
| Library | Version | Purpose | CDN URL |
|---------|---------|---------|---------|
| HTMX | 2.0.8 | Dynamic interactions | `https://cdn.jsdelivr.net/npm/htmx.org@2.0.8/dist/htmx.min.js` |
| PicoCSS | 2.1.1 | CSS framework | `https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css` |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| CDN-hosted HTMX/PicoCSS | Vendored copies in static/ | CDN is simpler; vendor for air-gapped deployments (defer to packaging phase) |
| Separate API + template routers | Single FastAPI app | Multiple routers is cleaner for organization |

**Installation:**
```bash
uv add fastapi uvicorn python-multipart
```

Note: Jinja2 is already a dependency of FastAPI (installed transitively). `python-multipart` is required for form body parsing.

## Architecture Patterns

### Recommended Project Structure
```
src/saneless/
├── web/
│   ├── __init__.py          # create_app() factory
│   ├── app.py               # FastAPI app factory, lifespan, startup/shutdown
│   ├── routes.py             # All route handlers (page, API, health)
│   ├── cache.py              # TTL cache for paperless tags/correspondents
│   ├── templates/
│   │   ├── base.html         # HTML shell: head, PicoCSS, HTMX, body container
│   │   ├── index.html        # Main page extending base: form + status + history
│   │   └── partials/
│   │       ├── status.html   # Job status area (polled via HTMX)
│   │       ├── flip.html     # Flip prompt partial (swapped in during AWAITING_FLIP)
│   │       ├── history.html  # Job history table body
│   │       ├── tags.html     # Tag <option> elements for dropdown
│   │       └── correspondents.html  # Correspondent <option> elements
│   └── static/
│       └── app.css           # App-specific CSS overrides (minimal)
├── job.py                    # (existing) + add list_recent(), prune()
├── worker.py                 # (existing) + add is_alive property
├── paperless.py              # (existing) + add get_tags(), get_correspondents()
├── config.py                 # (existing) + add paperless_cache_ttl_seconds
└── ...
```

### Pattern 1: FastAPI App Factory with Lifespan
**What:** Use FastAPI's `lifespan` context manager for startup/shutdown (create worker, start thread, run initial prune; on shutdown stop worker, close connections).
**When to use:** Always -- this is the modern FastAPI pattern replacing deprecated `on_event`.
**Example:**
```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create worker, start thread, prune history
    worker = ScanWorker(scanner, paperless, settings, job_store)
    worker.start()
    job_store.prune(settings.output.history_retention_days, settings.output.history_max_rows)
    app.state.worker = worker
    app.state.job_store = job_store
    yield
    # Shutdown
    worker.stop()
    job_store.close()

app = FastAPI(lifespan=lifespan)
```

### Pattern 2: HTMX Partial Responses
**What:** Return HTML fragments (not full pages) for HTMX requests. Use `HX-Request` header to distinguish HTMX requests.
**When to use:** All dynamic updates (status polling, dropdown refresh, form submission response).
**Example:**
```python
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="templates")

@app.get("/api/jobs/current/status")
async def job_status(request: Request) -> HTMLResponse:
    job = job_store.get_current_or_latest()
    return templates.TemplateResponse(
        "partials/status.html",
        {"request": request, "job": job},
    )
```

### Pattern 3: Conditional HTMX Polling
**What:** Start/stop polling based on job state. When idle, the status area has no `hx-trigger`. When a job starts, the response includes `hx-trigger="every 1s"` to begin polling. When the job completes, the final status response omits the trigger to stop polling.
**When to use:** Status area to avoid unnecessary polling when idle.
**Implementation approach:** Include `hx-trigger` attribute in the partial template conditionally:
```html
<div id="status-area"
     {% if job and job.state not in ['DONE', 'ERROR', 'PENDING'] %}
     hx-get="/api/jobs/current/status"
     hx-trigger="every 1s"
     hx-swap="outerHTML"
     {% endif %}>
    <!-- status content -->
</div>
```
After scan form submission, the response swaps in a status area that includes the polling trigger. Once the job reaches a terminal state, the polled response returns a status area without the trigger, stopping further polls.

### Pattern 4: TTL Cache for Paperless Metadata
**What:** Simple dict-based cache with timestamp, keyed by resource name (tags, correspondents). Per-resource invalidation.
**When to use:** Tag and correspondent dropdowns.
**Example:**
```python
import time

class MetadataCache:
    def __init__(self, ttl: int = 60) -> None:
        self._ttl = ttl
        self._store: dict[str, tuple[float, list]] = {}

    def get(self, key: str) -> list | None:
        if key in self._store:
            ts, data = self._store[key]
            if time.monotonic() - ts < self._ttl:
                return data
        return None

    def set(self, key: str, data: list) -> None:
        self._store[key] = (time.monotonic(), data)

    def invalidate(self, key: str) -> None:
        self._store.pop(key, None)
```

### Anti-Patterns to Avoid
- **Full page reload for dynamic updates:** Always use HTMX partial swaps, never redirect-after-POST for status updates
- **WebSocket for status:** Unnecessary complexity; 1s polling with HTMX is simpler and sufficient for this use case
- **Client-side state management:** All state lives server-side (SQLite); the browser only renders what the server sends
- **Async database calls:** SQLite is synchronous; do not wrap in `run_in_executor` unless performance demands it (unlikely for this workload). Use sync route handlers where appropriate.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Form parsing | Manual request body parsing | FastAPI `Form()` + python-multipart | Handles multipart, validation, CSRF-adjacent concerns |
| Template rendering | String concatenation / f-strings | Jinja2Templates | Auto-escaping, template inheritance, partials |
| CSS framework | Custom CSS from scratch | PicoCSS classless | Responsive, dark mode, accessible out of the box |
| Dynamic behavior | Custom JavaScript | HTMX attributes | Declarative, no JS to maintain, handles swap/polling/triggers |
| Health check logic | Custom thread monitoring | `threading.Thread.is_alive()` | Built-in Python, reliable, no polling needed |

**Key insight:** The locked decision to use HTMX + Jinja2 + PicoCSS means the entire frontend has zero build tooling and minimal custom JavaScript. Almost all interaction logic is declarative HTMX attributes in HTML templates.

## Common Pitfalls

### Pitfall 1: FastAPI Form() vs JSON Body
**What goes wrong:** FastAPI defaults to JSON request bodies. HTMX form submissions send `application/x-www-form-urlencoded`. Forgetting to use `Form()` parameters results in 422 Unprocessable Entity errors.
**Why it happens:** FastAPI's typical use case is JSON APIs; form handling requires explicit `Form()` type annotations AND `python-multipart` installed.
**How to avoid:** Use `Form()` for all HTMX form submission endpoints. Ensure `python-multipart` is in dependencies.
**Warning signs:** 422 errors on form submission; "field required" validation errors.

### Pitfall 2: HTMX Swap Target Mismatch
**What goes wrong:** HTMX swaps content into the wrong element or fails silently because the `id` in the response doesn't match `hx-target`.
**Why it happens:** Partial templates must return elements with matching IDs for `hx-swap="outerHTML"`.
**How to avoid:** Use consistent ID naming (`id="status-area"`, `id="history-table"`, `id="tags-select"`). Test with Playwright that swaps work correctly.
**Warning signs:** Content disappears after swap; duplicate elements appear.

### Pitfall 3: Thread Safety with SQLite
**What goes wrong:** Concurrent reads from the web thread and writes from the worker thread cause "database is locked" errors.
**Why it happens:** SQLite WAL mode allows concurrent reads but only one writer.
**How to avoid:** Already using `check_same_thread=False` and WAL mode. Keep write transactions short. The worker writes state updates; the web layer only reads (except for job creation).
**Warning signs:** Intermittent "database is locked" errors under load.

### Pitfall 4: Stale HTMX Polling After Navigation
**What goes wrong:** If the user navigates away and back (browser back button, bookmark), HTMX polling state may be inconsistent.
**Why it happens:** HTMX polling is tied to DOM elements; page reload resets all state.
**How to avoid:** Single-page layout (locked decision) means no navigation. Full page load always renders current state correctly. Polling starts only when server returns active job state.
**Warning signs:** Not applicable with single-page layout.

### Pitfall 5: Missing `request` in Jinja2 Template Context
**What goes wrong:** FastAPI's `Jinja2Templates.TemplateResponse` requires a `request` object in the context dict or it raises an error.
**Why it happens:** Starlette's template response needs the request for URL generation.
**How to avoid:** Always include `{"request": request, ...}` in template context.
**Warning signs:** TypeError or KeyError mentioning "request".

### Pitfall 6: HTMX Multi-Select for Tags
**What goes wrong:** Standard HTML `<select multiple>` sends multiple values for the same form field name. FastAPI needs `Form()` with `list[int]` type to receive them.
**Why it happens:** HTML multi-select behavior differs from single-value fields.
**How to avoid:** Use `tags: list[int] = Form(default=[])` in the endpoint signature. In the template, use `<select name="tags" multiple>`.
**Warning signs:** Only the first tag is received; or tags parameter is always empty.

### Pitfall 7: PicoCSS Semantic HTML Requirements
**What goes wrong:** PicoCSS styles elements semantically (e.g., `<nav>`, `<main>`, `<article>`, `<select>`, `<table>`). Using non-semantic wrappers (`<div>` everywhere) results in unstyled elements.
**Why it happens:** PicoCSS is classless -- it styles HTML elements, not CSS classes.
**How to avoid:** Use semantic HTML: `<main>` for content, `<article>` for grouped sections, `<header>/<footer>` for page chrome, `<table>` for job history. Avoid wrapping everything in `<div>`.
**Warning signs:** Elements look unstyled; form controls lack PicoCSS styling.

## Code Examples

### FastAPI App Factory
```python
# src/saneless/web/app.py
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from saneless.config import Settings
from saneless.job import JobStore
from saneless.paperless import PaperlessClient
from saneless.scanner.base import ScannerBackend
from saneless.web.cache import MetadataCache
from saneless.worker import ScanWorker

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def create_app(
    settings: Settings,
    scanner: ScannerBackend,
) -> FastAPI:
    """Create and configure the FastAPI application."""
    paperless = PaperlessClient(
        url=settings.paperless.url,
        token=settings.paperless.token,
        consume_dir=settings.paperless.consume_dir,
    )
    job_store = JobStore(db_path=str(
        Path(settings.output.tmp_dir) / "saneless.db"
    ))
    cache = MetadataCache(ttl=settings.output.paperless_cache_ttl_seconds)
    worker = ScanWorker(scanner, paperless, settings, job_store)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        worker.start()
        job_store.prune(
            settings.output.history_retention_days,
            settings.output.history_max_rows,
        )
        yield
        worker.stop()
        paperless.close()
        job_store.close()

    app = FastAPI(lifespan=lifespan)
    app.state.worker = worker
    app.state.job_store = job_store
    app.state.settings = settings
    app.state.paperless = paperless
    app.state.cache = cache

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    # Register routes...
    return app
```

### HTMX Status Polling Partial
```html
{# partials/status.html #}
{% set active_states = ["SCANNING", "ASSEMBLING", "UPLOADING", "AWAITING_FLIP"] %}
<div id="status-area"
     {% if job and job.state.value in active_states %}
     hx-get="/api/jobs/current/status"
     hx-trigger="every 1s"
     hx-swap="outerHTML"
     {% endif %}>
  {% if job %}
    {% if job.state.value == "SCANNING" %}
      <p aria-busy="true">Scanning...</p>
    {% elif job.state.value == "AWAITING_FLIP" %}
      {% include "partials/flip.html" %}
    {% elif job.state.value == "ASSEMBLING" %}
      <p aria-busy="true">Assembling PDF...</p>
    {% elif job.state.value == "UPLOADING" %}
      <p aria-busy="true">Uploading to paperless-ngx...</p>
    {% elif job.state.value == "DONE" %}
      <p>&#10003; Done: {{ job.title }}</p>
    {% elif job.state.value == "ERROR" %}
      <p role="alert">&#10007; Error: {{ job.error }}</p>
    {% endif %}
    {% if job.thumbnail %}
      <img src="data:image/jpeg;base64,{{ job.thumbnail }}" alt="First page preview">
    {% endif %}
  {% else %}
    <p>Ready to scan.</p>
  {% endif %}
</div>
```

### Scan Form Submission Handler
```python
@app.post("/api/scan")
async def start_scan(
    request: Request,
    profile: str = Form(...),
    title: str = Form(default=""),
    tags: list[int] = Form(default=[]),
    correspondent: int | None = Form(default=None),
) -> HTMLResponse:
    """Submit a scan job and return updated status area."""
    worker: ScanWorker = request.app.state.worker
    job_store: JobStore = request.app.state.job_store

    if not title:
        title = f"Scan {datetime.now(tz=UTC).strftime('%Y-%m-%d %H:%M')}"

    job = job_store.create_job(
        profile=profile,
        title=title,
        tags=tags,
        correspondent=correspondent,
    )
    worker.submit(job)

    return templates.TemplateResponse(
        "partials/status.html",
        {"request": request, "job": job},
    )
```

### Health Endpoint
```python
@app.get("/health")
async def health(request: Request) -> dict:
    """Health check for container orchestration."""
    worker: ScanWorker = request.app.state.worker
    if worker.is_alive:
        return {"status": "ok"}
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=503,
        content={"status": "error", "detail": "worker thread is down"},
    )
```

### Job History with Pruning (new JobStore methods)
```python
# Addition to JobStore class
def list_recent(self, limit: int = 50) -> list[Job]:
    """Fetch most recent jobs ordered by creation time descending."""
    rows = self._conn.execute(
        "SELECT id, profile, title, state, error, tags, correspondent, "
        "thumbnail, created_at FROM jobs ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [self._row_to_job(row) for row in rows]

def prune(self, max_age_days: int = 7, max_rows: int = 500) -> int:
    """Delete old jobs by age and count. Return number deleted."""
    from datetime import timedelta
    cutoff = (datetime.now(tz=UTC) - timedelta(days=max_age_days)).isoformat()
    self._conn.execute("DELETE FROM jobs WHERE created_at < ?", (cutoff,))
    # Also enforce max row count
    self._conn.execute(
        "DELETE FROM jobs WHERE id NOT IN "
        "(SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?)",
        (max_rows,),
    )
    deleted = self._conn.total_changes
    self._conn.commit()
    return deleted
```

### PaperlessClient Tag/Correspondent Fetching (new methods)
```python
# Addition to PaperlessClient class
def get_tags(self) -> list[dict]:
    """Fetch all tags from paperless-ngx."""
    response = self._client.get("/api/tags/", params={"page_size": 1000})
    response.raise_for_status()
    data = response.json()
    return data.get("results", []) if isinstance(data, dict) else data

def get_correspondents(self) -> list[dict]:
    """Fetch all correspondents from paperless-ngx."""
    response = self._client.get("/api/correspondents/", params={"page_size": 1000})
    response.raise_for_status()
    data = response.json()
    return data.get("results", []) if isinstance(data, dict) else data
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| FastAPI `on_event("startup")` | `lifespan` context manager | FastAPI 0.95+ | Must use lifespan, not deprecated decorators |
| HTMX 1.x `hx-ws` | HTMX 2.x removed WebSocket ext from core | HTMX 2.0 | Use polling (our approach), not WebSocket |
| PicoCSS 1.x | PicoCSS 2.x with improved semantics | PicoCSS 2.0 | Use `@picocss/pico@2`, not v1 |
| `Starlette.TemplateResponse(name, context)` | `TemplateResponse(request, name, context)` | Starlette 0.29+ | Check which signature style FastAPI 0.135 expects |

**Deprecated/outdated:**
- `@app.on_event("startup")` / `@app.on_event("shutdown")` -- use `lifespan` parameter instead
- HTMX 1.x attribute names (some changed in 2.0, e.g., `hx-ws` moved to extension)

## Open Questions

1. **Starlette TemplateResponse signature**
   - What we know: Recent Starlette versions changed the TemplateResponse constructor. Some versions want `TemplateResponse(request, name, context)` instead of `TemplateResponse(name, context)`.
   - What's unclear: Exact signature in FastAPI 0.135.1's bundled Starlette version.
   - Recommendation: Use `templates.TemplateResponse(name, {"request": request, ...})` which works across versions. Verify at implementation time.

2. **HTMX script serving strategy**
   - What we know: CDN is simplest. Air-gapped/offline deployments need vendored copies.
   - What's unclear: Whether users will deploy in air-gapped environments.
   - Recommendation: Use CDN for now; vendoring is a Phase 4 packaging concern.

3. **Periodic pruning mechanism**
   - What we know: PRD says prune at startup AND periodically at runtime.
   - What's unclear: Best mechanism for periodic pruning in a FastAPI app without adding a scheduler dependency.
   - Recommendation: Prune at startup (in lifespan). For periodic pruning, piggyback on status polling -- prune once per hour by checking a timestamp. Alternatively, use `asyncio.create_task` with a simple sleep loop.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.0.2 |
| Config file | pyproject.toml `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/ -x -q` |
| Full suite command | `uv run pytest tests/ -v` |

### Phase Requirements to Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| UI-01 | Web UI loads with form fields and profile selector | e2e (Playwright) | `uv run pytest tests/test_web.py::test_page_loads -x` | No -- Wave 0 |
| UI-02 | Status polling returns correct job state HTML | unit | `uv run pytest tests/test_web.py::test_status_polling -x` | No -- Wave 0 |
| UI-03 | Flip prompt appears during AWAITING_FLIP state | unit | `uv run pytest tests/test_web.py::test_flip_prompt -x` | No -- Wave 0 |
| UI-04 | Thumbnail displayed when available | unit | `uv run pytest tests/test_web.py::test_thumbnail_display -x` | No -- Wave 0 |
| UI-05 | Job history table lists recent jobs | unit | `uv run pytest tests/test_web.py::test_job_history -x` | No -- Wave 0 |
| UI-06 | Job history pruned by age and count | unit | `uv run pytest tests/test_job.py::test_prune -x` | No -- Wave 0 |
| UI-07 | Scan button disabled during active job | e2e (Playwright) | `uv run pytest tests/test_web.py::test_scan_button_disabled -x` | No -- Wave 0 |
| UI-08 | Refresh icon triggers cache invalidation | unit | `uv run pytest tests/test_web.py::test_cache_invalidate -x` | No -- Wave 0 |
| PROF-03 | Profile dropdown populated from settings | unit | `uv run pytest tests/test_web.py::test_profile_dropdown -x` | No -- Wave 0 |
| PLSS-04 | Form accepts title, tags, correspondent | unit | `uv run pytest tests/test_web.py::test_scan_form_submit -x` | No -- Wave 0 |
| PLSS-05 | Tag/correspondent cache with TTL and invalidation | unit | `uv run pytest tests/test_cache.py -x` | No -- Wave 0 |
| HLTH-01 | Health returns 200/503 based on worker state | unit | `uv run pytest tests/test_web.py::test_health_endpoint -x` | No -- Wave 0 |
| HLTH-02 | Health endpoint requires no auth | unit | `uv run pytest tests/test_web.py::test_health_no_auth -x` | No -- Wave 0 |
| LOG-03 | Error message displayed in status area | unit | `uv run pytest tests/test_web.py::test_error_display -x` | No -- Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/ -x -q`
- **Per wave merge:** `uv run pytest tests/ -v`
- **Phase gate:** Full suite green + Playwright browser checks before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_web.py` -- covers UI-01 through UI-08, PROF-03, PLSS-04, HLTH-01, HLTH-02, LOG-03
- [ ] `tests/test_cache.py` -- covers PLSS-05 (TTL cache with invalidation)
- [ ] `tests/test_job.py::test_prune` / `tests/test_job.py::test_list_recent` -- covers UI-05, UI-06
- [ ] Dev dependency: `uv add --dev httpx` (for FastAPI TestClient)

## Sources

### Primary (HIGH confidence)
- PyPI registry -- verified versions: FastAPI 0.135.1, uvicorn 0.42.0, Jinja2 3.1.6, python-multipart 0.0.22
- unpkg.com redirect -- confirmed HTMX 2.0.8
- cdnjs.com -- confirmed PicoCSS 2.1.1
- Existing codebase: `src/saneless/job.py`, `worker.py`, `paperless.py`, `config.py` -- direct code analysis

### Secondary (MEDIUM confidence)
- [HTMX releases](https://github.com/bigskysoftware/htmx/releases) -- version 2.0.7/2.0.8 confirmed
- [PicoCSS documentation](https://picocss.com/docs) -- v2 CDN URLs and classless usage

### Tertiary (LOW confidence)
- FastAPI TemplateResponse signature -- needs verification at implementation time against actual installed version

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- all versions verified against PyPI/CDN registries
- Architecture: HIGH -- FastAPI + Jinja2 + HTMX is a well-documented pattern; existing codebase integration points are clear
- Pitfalls: HIGH -- based on direct code analysis and known FastAPI/HTMX patterns
- Validation: MEDIUM -- test structure planned but Playwright e2e tests need implementation verification

**Research date:** 2026-03-20
**Valid until:** 2026-04-20 (stable stack, no fast-moving components)
