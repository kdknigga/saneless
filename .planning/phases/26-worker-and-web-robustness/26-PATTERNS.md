# Phase 26: Worker and Web Robustness - Pattern Map

**Mapped:** 2026-09-14
**Files analyzed:** 40 (22 source/template/config/CI, 14 test, docs as a group)
**Analogs found:** 34 / 40 (the other 6 are new kinds of file with no in-repo precedent: pure ASGI middleware, exception handlers, vendored minified assets, `.gitattributes`, SRI test, browser CI job; each has a RESEARCH.md pattern instead)

All line numbers below were read on 2026-09-14 at commit `9de2b8a`.

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|---|---|---|---|---|
| `src/saneless/worker.py` (M) | service (background thread) | event-driven (queue) | itself (`ScanWorker`, `WorkerFlipCoordinator`) | exact (in-place rewrite) |
| `src/saneless/web/app.py` (M) | config / app factory | lifecycle | itself (`create_app`, `lifespan`) | exact |
| `src/saneless/web/routes.py` (M) | controller | request-response | itself | exact |
| `src/saneless/web/cache.py` (M) | utility | CRUD (in-memory) | itself + `job.py` `_locked`/`RLock` | exact + lock idiom |
| `src/saneless/web/errors.py` (NEW) | middleware helper / exception handlers | request-response | `routes.py` `health` (JSON branch) + `TemplateResponse` calls | partial |
| `src/saneless/web/cross_origin.py` (NEW) | middleware (pure ASGI) | request-response | none | no analog (RESEARCH Pattern 10) |
| `src/saneless/config.py` (M) | config | file-I/O | itself (`load_settings`, `Settings`) | exact |
| `src/saneless/auto_profiles.py` (M, delete `resolve_config_path`) | utility | file-I/O | itself | exact |
| `src/saneless/cli.py` (M, `auto-profiles` caller) | CLI command | file-I/O | itself `cli.py:469-497` | exact |
| `src/saneless/vocabulary.py` (M) | model (enums + label fns) | transform | `flip_answer_label` / `error_message` / `ErrorCategory` | exact |
| `src/saneless/job.py` (M: `latest_run_job`, `probe`, docstring) | model / store | CRUD | `list_recent`, `list_pending`, `_SELECT_RECENT`, `_LIST_PENDING` | exact |
| `web/templates/base.html` (M) | template | n/a | itself | exact |
| `web/templates/index.html` (M) | template | n/a | itself | exact |
| `web/templates/partials/scan_button.html` (NEW) | template partial | n/a | `index.html:57-60` (moved verbatim) | exact |
| `web/templates/partials/status_response.html` (NEW) | template partial | n/a | `index.html:65` include idiom | role-match |
| `web/templates/partials/error.html` (NEW) | template partial | n/a | `partials/status.html:27-29` ERROR branch | exact |
| `web/static/app.css` (M, one rule) | stylesheet | n/a | `app.css:3-9` `#status-area` | exact |
| `web/static/app.js` (DELETE) | n/a | n/a | n/a | n/a |
| `web/static/vendor/*` (NEW, 4 files) | vendored asset | n/a | none | no analog (RESEARCH Pattern 9) |
| `.pre-commit-config.yaml` (M) | config | n/a | itself | exact |
| `.gitattributes` (NEW) | config | n/a | none | no analog |
| `.github/workflows/ci.yml` (M, `browser` job) | CI config | batch | `ci.yml:33-51` `test` job | exact |
| `tests/test_worker.py` (M) | test | threaded | `TestWorkerFinish` (`:1922-1953`), `_GatedScanner` (`:1238-1308`) | exact |
| `tests/test_web.py` (M) | test | request-response | itself | exact |
| `tests/test_web_state_rendering.py` (M) | test | request-response | itself (`_SCAN_BUTTON`, parametrised `list(JobState)`) | exact |
| `tests/test_browser.py` (M) | test (e2e) | browser | itself (`browser_server`, `TestFallbackStatusRendering.fallback_page`) | exact |
| `tests/test_cache.py` (M) | test | unit | itself | exact |
| `tests/test_config.py` (M) | test | unit | `TestConfigFileSearch` (`:107-142`) | exact |
| `tests/test_auto_profiles.py` (M) | test | unit | `TestIsBareDefault` (`:332-397`) | exact |
| `tests/test_job.py` (M) | test | unit | `TestQueryMethods` (`:1289-1380`) | exact |
| `tests/test_vocabulary.py` (M) | test | unit | `TestErrorCategoryMembers` (`:65-86`) | exact |
| `tests/test_outcomes_e2e.py` (M, comment `:90-95`) | test | n/a | n/a | trivial |
| `tests/test_web_errors.py` (NEW) | test | request-response | `tests/test_web.py` fixtures `:45-129`, `test_paperless_test_502_sanitizes_exception` `:622-642` | role-match |
| `tests/test_cross_origin.py` (NEW) | test | request-response | `tests/test_web.py` fixtures | role-match |
| `tests/test_vendor_assets.py` (NEW) | test | file-I/O | `tests/test_web.py::test_css_spacing_normalized` `:611-619` | partial |
| `tests/test_app_lifespan.py` (NEW) | test | lifecycle | `tests/test_web.py` fixtures + `tests/test_job.py::TestQueryMethods` | role-match |
| `docs/reference/web-api.md`, `docs/explanation/architecture.md`, `docs/reference/configuration.md`, `environment-variables.md`, `cli-commands.md`, `docs/how-to/deploy-docker-compose.md` / `install-bare-metal.md`, `docs/reference/docker.md`, `CONTRIBUTING.md`, `.planning/UI-SPEC.md` | docs | n/a | existing prose at the cited lines | exact (edit in place) |

---

## Pattern Assignments

### `src/saneless/worker.py` (service, event-driven)

**Analog:** itself. The rewrite keeps the module shape and replaces `_run`, `stop`, `submit`, `_maybe_auto_generate`, and the `_process_job` try/finally.

**Imports pattern** (lines 9-46). Keep `from __future__ import annotations`, `TYPE_CHECKING` block for `Settings`/`Job`/`JobStore`/`PaperlessClient`/`ScannerBackend`, and `__all__`. Remove `resolve_config_path` from the `auto_profiles` import (`:16-21`); new names (`SubmitResult`, `WorkerHealth`, rejection copy) come from `.vocabulary`:
```python
from .auto_profiles import (
    generate_profiles,
    is_bare_default,
    resolve_config_path,          # DELETE (D-16)
    write_profiles_to_config,
)
from .job import JobResult
...
from .vocabulary import (
    ACTIVE_STATES,
    FlipOutcome,
    JobState,
    classify_error,
    job_state_for,
)
```

**Cross-thread flag pattern to copy** (lines 79-83, 90-102): `threading.Event` used as a one-way latch read by request threads. Use the same shape for `_stopping` and `_degraded`:
```python
def __init__(self, job_id: str) -> None:
    """Start unarmed and unanswered, bound to ``job_id``."""
    self._job_id = job_id
    self._slot = FlipAnswerSlot()
    self._armed = threading.Event()

@property
def armed(self) -> bool:
    """Whether the flip prompt exists, so a signal can claim the answer."""
    return self._armed.is_set()
```

**Current code being replaced** (lines 197, 208-223, 333-339): blocking `put`, sentinel, unguarded loop:
```python
self._queue: queue.Queue[Job | None] = queue.Queue(maxsize=10)   # -> queue.Queue[Job]; keep maxsize=10
...
def stop(self) -> None:
    self._queue.put(None)                 # C-09: blocks on a full queue
    self._thread.join(timeout=5)          # result ignored
def submit(self, job: Job) -> None:
    self._queue.put(job)                  # C-09: blocks the request thread
def _run(self) -> None:
    while True:
        item = self._queue.get()
        if item is None:
            break
        self._process_job(item)           # no guard: any store error kills the thread
```
Replace with RESEARCH Pattern 1 (`get(timeout=_IDLE_TICK_SECONDS)`, `queue.ShutDown`, `shutdown(immediate=True)`, `put_nowait` -> `queue.Full`, `stop() -> bool`, `submit() -> SubmitResult`).

**Job-vs-loop failure split** (lines 433-486). The existing `try` wraps pipeline + `finish_job`; the `except` calls `finish_job` again and `finally` prunes. Restructure so only the pipeline raise is a job failure, the `finish_job` in the except can escape (loop-level), and `prune` leaves this method (D-13):
```python
        except Exception as exc:
            category = classify_error(exc)
            self._job_store.finish_job(
                job.id,
                JobState.ERROR,
                error=str(exc),
                error_category=category,
            )
            logger.error("Job %s failed (%s): %s", job.id, category.value.lower(), exc)
        finally:
            self._flip_coordinator = None
            self._current_job_id = None
            self._job_store.prune(               # MOVE OUT (D-13): startup + rate-limited idle tick
                self._settings.output.history_retention_days,
                self._settings.output.history_max_rows,
            )
```
Keep the `finally` that clears `_flip_coordinator` / `_current_job_id` (tests in `test_browser.py:599,611` and `test_web_state_rendering.py:135` write `worker._current_job_id` directly; keep the attribute name).

**Flip-arm hook for the shutdown race** (lines 421-431): add `if self._stopping.is_set(): coordinator.signal_abort()` right after the existing `coordinator.arm()`:
```python
            if state is JobState.AWAITING_FLIP and coordinator is not None:
                # Arm BEFORE persisting. ...
                coordinator.arm()
            self._job_store.update_state(_jid, state)
```

**Profile lookup that must go through the D-19 lock** (line 391):
```python
        profile = self._settings.profiles.get(job.profile)
```

**Startup generation analog** (lines 341-373) -> becomes `_generate_startup_profiles()`, called first in `_run`. Keep the device/caps/generate sequence; replace `resolve_config_path()` with `self._settings.config_path`; replace the blanket message with `type(exc).__name__`; rebind `settings.profiles` to a new dict under the lock instead of in-place mutation:
```python
        try:
            devices = self._scanner.get_devices()
            if not devices:
                logger.warning("Auto-profiles: no scanners found, using bare default")
                return
            device_id = self._settings.scanner.device or devices[0].name
            caps = self._scanner.get_capabilities(device_id)
            profiles = generate_profiles(caps)
            config_path = resolve_config_path()                  # -> self._settings.config_path (None => memory only, INFO)
            written = write_profiles_to_config(config_path, profiles)   # OSError / ConfigError => WARNING + memory (D-18)
            # Update in-memory settings
            for name, profile in profiles.items():
                self._settings.profiles[name] = profile          # -> rebind new dict under _profiles_lock (D-19)
            ...
        except Exception:
            logger.warning(
                "Auto-profiles: scanner unreachable, using bare default",   # -> name the real class (D-15)
                exc_info=True,
            )
```

**Logging style to copy** (lines 294-320): `%`-args, job id always named, no f-strings in log calls (ruff `G`):
```python
            logger.info(
                "Manual duplex: %s for job %s dropped: "
                "not the job waiting at the flip prompt",
                action,
                job_id,
            )
```

**Docstring density** (lines 50-77, 275-290): multi-line summary on line 2, cites finding ids (`CR-01`, `D-16`). New methods cite `C-09`, `M-03`, `M-04`, `D-07`..`D-19`.

---

### `src/saneless/web/app.py` (app factory, lifecycle)

**Analog:** itself.

**Lifespan to reorder** (lines 70-86). Current order is wrong for D-13 (worker starts before prune, no recovery, unconditional close):
```python
    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        """Manage worker lifecycle and job pruning on startup/shutdown."""
        validate_settings_dirs(settings)
        worker.start()
        job_store.prune(
            settings.output.history_retention_days,
            settings.output.history_max_rows,
        )
        logger.info("App started, old jobs pruned")
        yield
        worker.stop()
        paperless.close()
        job_store.close()
        logger.info("App shutdown complete")

    app = FastAPI(lifespan=lifespan)
```
Target order (RESEARCH Pattern 7): `validate_settings_dirs` -> `fail_active_jobs(<restart reason from vocabulary>)` -> guarded prune -> `worker.start()`; shutdown `if not worker.stop(): logger.warning(... job id ...); return` then close.

**Template environment registration** (lines 93-108): new globals/filters (e.g. a rejection label filter, `TITLE_MAX_LENGTH` for `maxlength`) follow this exact idiom, including the typed-`Any` widening instead of a suppression:
```python
    app.state.templates.env.filters["state_label"] = state_label
    app.state.templates.env.filters["progress_label"] = progress_label
    app.state.templates.env.filters["flip_answer_label"] = flip_answer_label
    # Jinja2 3.1.6 builds `Environment.globals` from the unannotated
    # `DEFAULT_NAMESPACE` dict, ... Handing the enum over through an explicitly
    # `Any`-typed name states that widening in code rather than suppressing the
    # resulting false positive with a comment.
    job_state_global: Any = JobState
    app.state.templates.env.globals["JobState"] = job_state_global
```

**Wiring point** (lines 110-113): add `install_error_handlers(app)` and `app.add_middleware(CrossOriginGuard)` next to these:
```python
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(router)
```
`STATIC_DIR` (line 39) is the constant `tests/test_vendor_assets.py` should import to map `href`/`src` to bytes.

---

### `src/saneless/web/routes.py` (controller, request-response)

**Analog:** itself.

**Imports** (lines 3-26). Add `Annotated`, `Literal` from `typing` (runtime, not under `TYPE_CHECKING`, because FastAPI reads them), `HTTPException` from `fastapi`:
```python
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from saneless.vocabulary import FlipOutcome, JobState

if TYPE_CHECKING:
    from starlette.responses import Response
    ...
router = APIRouter()

_TAGS_FORM_DEFAULT = Form(default=[])
```
Put the ROBU-05 "why every handler is `def`" comment directly above `router = APIRouter()`.

**Handler signature to convert** (every `async def` at lines 142, 175, 191, 210, 252, 269, 286, 305, 339, 356, 386). Current scan handler (lines 209-248):
```python
@router.post("/api/scan")
async def start_scan(
    request: Request,
    profile: str = Form(...),
    title: str = Form(default=""),
    tags: list[int] = _TAGS_FORM_DEFAULT,
    correspondent: int | None = Form(default=None),
) -> Response:
    ...
    job = state.job_store.create_job(
        profile=profile,
        title=title,
        tags=tags,
        correspondent=correspondent,
    )
    state.worker.submit(job)

    # A job created by this request cannot have a flip answer yet.
    return state.templates.TemplateResponse(
        request,
        "partials/status.html",
        {"job": job, "flip_answer": None},
    )
```
Changes: `def`; `title: Annotated[str, Form(max_length=_TITLE_MAX)]`; unknown profile -> `HTTPException(422)` **before** `create_job`; `match` on `SubmitResult` -> `finish_job(job.id, JobState.ERROR, error=<row text>, error_category=ErrorCategory.REJECTED)` then `HTTPException(429|503)`; success renders `partials/status_response.html` with `clear_message=True`.

**D-06 lookup to adjust** (lines 69-95): replace `list_recent(limit=1)` with the new `job_store.latest_run_job()`; keep the docstring's M-02 / D-17 reasoning and add D-06:
```python
    job = None
    if worker.current_job_id:
        job = job_store.get_job(worker.current_job_id)
    if job is None:
        recent = job_store.list_recent(limit=1)
        job = recent[0] if recent else None
    return job
```
Note: `index` (line 159) and `job_history` (line 347) keep `list_recent(limit=50)`; rejection rows **must** appear in history.

**Profile read to lock** (line 151): `profiles = list(state.settings.profiles.keys())` -> `state.worker.profile_names()`.

**Health** (lines 174-187) -> total `match` over `WorkerHealth` (RESEARCH "`/health` with a total enum"). Keep `response_model=None` and the `dict[str, str] | JSONResponse` return type:
```python
@router.get("/health", response_model=None)
async def health(request: Request) -> dict[str, str] | JSONResponse:
    if request.app.state.worker.is_alive:
        return {"status": "ok"}
    return JSONResponse(
        status_code=503,
        content={"status": "error", "detail": "worker thread is down"},
    )
```

**Cache route validation** (line 305): `resource: str` -> `resource: Literal["tags", "correspondents"]`. `_get_cached_or_fetch` (lines 31-66) delegates to the new `cache.get_or_fetch`, keeping its `except Exception: logger.warning(...)` empty-list fallback (stale-on-error is SWP-04, not here).

**Status-rendering routes switching template** (lines 260-265, 374-382, 402-410): `"partials/status.html"` -> `"partials/status_response.html"`, context unchanged (`_status_context(...)`), no `clear_message`.

---

### `src/saneless/web/cache.py` (utility)

**Analog:** itself (whole file, lines 1-65) + the store's lock discipline.

**Class/docstring shape to extend** (lines 10-25):
```python
class MetadataCache:
    """
    In-memory cache with per-key TTL expiration.
    ...
    """

    def __init__(self, ttl: int = 60) -> None:
        """Initialize the cache with the given TTL."""
        self._ttl = ttl
        self._store: dict[str, tuple[float, list[dict[str, object]]]] = {}
```
Add `self._locks_guard = threading.Lock()` and `self._key_locks: dict[str, threading.Lock] = {}`, then `get_or_fetch(key, fetch: Callable[[], list[dict[str, object]]])` per RESEARCH Pattern 6 (per-key lock + re-check). `Callable` goes under `TYPE_CHECKING` from `collections.abc` (as in `job.py:30-31`). The docstring must name the deferred M-01 second half (waiters retry one after another while Paperless is unreachable).

---

### `src/saneless/web/errors.py` (NEW; exception handlers + renderer)

**Analog (partial):** `routes.py` response construction. No exception handler exists in the repo yet.

**HTML branch** copies the `TemplateResponse` call form used by every route (`routes.py:278-282`); add `status_code=` and `headers=`:
```python
    return state.templates.TemplateResponse(
        request,
        "partials/tags.html",
        {"tags": tags},
    )
```
**JSON branch** copies `routes.py:184-187` / `:203-206` shape `{"status": "error", "detail": ...}`:
```python
        return JSONResponse(
            status_code=502,
            content={"status": "error", "detail": type(exc).__name__},
        )
```
**Never echo input or exception text**: the house precedent is `routes.py:201-206` (detail is `type(exc).__name__`, not `str(exc)`) and its test `tests/test_web.py:622-633`. The renderer goes further (fixed vocabulary constant only).

Module skeleton, `render_error(request, status_code, message, *, retry_after=None)`, `install_error_handlers(app)`: RESEARCH Pattern 4. Widen handler `exc` parameters to `Exception` and narrow with `isinstance` if ty/pyrefly object (no suppression).

---

### `src/saneless/web/cross_origin.py` (NEW; pure ASGI middleware)

**No analog.** Use RESEARCH Pattern 10 verbatim (`cross_origin_verdict(method, headers) -> bool`, `CrossOriginGuard.__call__` calls `render_error(Request(scope), 403, ...)` directly, never raises `HTTPException`). Shared conventions still apply: module docstring, `from __future__ import annotations`, `__all__`, `logger = logging.getLogger(__name__)`, `%r` header values in the 403 log line, starlette types (`ASGIApp`, `Scope`, `Receive`, `Send`) under `TYPE_CHECKING`.

---

### `src/saneless/config.py` (config, file-I/O)

**Analog:** itself.

**Duplicated search list to collapse** (lines 424-438) - hoist into module-level `CONFIG_SEARCH_PATHS` and record the path on the settings:
```python
    if config_path:
        return _build_settings(toml_file=Path(config_path))

    search_paths = [
        Path("./saneless.toml"),
        Path.home() / ".config" / "saneless" / "config.toml",
        Path("/etc/saneless/config.toml"),
    ]

    for path in search_paths:
        if path.exists():
            return _build_settings(toml_file=path)

    # No config file found -- use defaults + env vars only
```
**Settings class to extend** (lines 204-221) with `_config_path: Path | None = PrivateAttr(default=None)` plus a read-only `config_path` property (not a public field: env-settable, RESEARCH Pattern 8):
```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SANELESS_",
        env_nested_delimiter="__",
    )

    scanner: ScannerConfig = ScannerConfig()
    paperless: PaperlessConfig = PaperlessConfig()
    output: OutputConfig = OutputConfig()
    profiles: dict[str, ProfileConfig] = {"default": ProfileConfig()}
```
**Do not copy** `# noqa: ARG003` at lines 229-230; it predates the no-suppression rule.

---

### `src/saneless/auto_profiles.py` and `src/saneless/cli.py` (file-I/O)

**Delete** `resolve_config_path` (`auto_profiles.py:425-448`), whose body is the second copy of the search list. **Keep** `write_profiles_to_config` (`:485-579`): it raises `ConfigError` for a non-table `[profiles]` (`:535-537`) and lets `OSError` from `config_path.write_text` (`:578`) propagate. The worker's D-18 handling catches both.

**CLI caller** (`cli.py:474-491`):
```python
    settings = ctx.obj["settings"]
    config_path_str: str | None = (
        ctx.parent.params.get("config_path") if ctx.parent else None
    )
    ...
    config_path = resolve_config_path(config_path_str)
    written = write_profiles_to_config(config_path, profiles, force=force)
```
-> `config_path = settings.config_path or Path("./saneless.toml")` (RESEARCH Open Question 4: CLI keeps today's fallback; daemon never uses it). Drop the `resolve_config_path` import at `cli.py:24`.

---

### `src/saneless/vocabulary.py` (model: enums + total label functions)

**Analog:** itself. New `ErrorCategory.REJECTED`, `WorkerHealth`, `SubmitResult`, a `RequestRejection`-style enum with its label function, row-text and restart-reason constants (UI-SPEC S3, verbatim copy).

**Enum member pattern** (lines 55-62): value equals name (pinned by `tests/test_vocabulary.py:84-86`):
```python
class ErrorCategory(StrEnum):
    """Categories of errors for programmatic handling."""

    FEEDER = "FEEDER"
    CONFIG = "CONFIG"
    SCANNER = "SCANNER"
    UPLOAD = "UPLOAD"
    UNKNOWN = "UNKNOWN"
```
**Total label function** (`flip_answer_label`, lines 268-300; `error_message`, lines 340-376). Single assignment per arm, `case _: assert_never(...)`, one `return`. `error_message` needs a `REJECTED` arm or both checkers fail:
```python
    match outcome:
        case FlipOutcome.CONTINUED:
            label = "Flip confirmed. Scanning reverse sides next..."
        case FlipOutcome.ABORTED:
            label = "Aborting scan..."
        case FlipOutcome.TIMED_OUT:
            label = "Flip wait timed out..."
        case _:
            assert_never(outcome)
    return label
```
**Leaf-module rule** (lines 1-8): vocabulary imports nothing from `job`/`worker`/`web`. Also update `__all__` (lines 22-38), which is kept sorted.

---

### `src/saneless/job.py` (store, CRUD)

**Analog:** `list_recent` / `list_pending` and the module-level statement constants.

**Statement constant + docstring pattern** (lines 156-170). `_SELECT_LATEST_RUN` follows `_LIST_PENDING` exactly: interpolate only module constants, bind every value, and give the constant a docstring with the S608 safety argument and the NULL-safe `IS NOT` note:
```python
_SELECT_RECENT = f"{_SELECT_ALL} ORDER BY created_at DESC LIMIT ?"
"""Read the newest jobs first, up to a bound limit."""

_LIST_PENDING = f"{_SELECT_ALL} WHERE state = ? ORDER BY created_at ASC"
"""Read the queued jobs oldest-first -- the order they will be worked in.
...
only interpolated value is the module-level ``_SELECT_ALL``.
"""
```
**Locked read method** (lines 765-790):
```python
    @_locked
    def list_pending(self) -> list[Job]:
        """..."""
        with self._conn:
            rows = self._conn.execute(
                _LIST_PENDING, (JobState.PENDING.value,)
            ).fetchall()
        return [self._row_to_job(row) for row in rows]
```
`latest_run_job()` uses `.fetchone()` and returns `None if row is None else self._row_to_job(row)`. `probe()` (RESEARCH "Store probe") also needs `@_locked` and `with self._conn:`. `tests/test_job.py::TestLockDiscipline` (`:1098`) reflectively checks every public method carries the `_LOCKED_MARKER`.

**Docstrings to correct in this phase:** module docstring lines 1-7 ("for crash recovery", doc row 33), and `fail_active_jobs` lines 820-824 ("NO production caller in this phase ... belongs to Phase 26"), which is no longer true once lifespan calls it.

---

### `web/templates/partials/scan_button.html` (NEW)

**Analog:** `index.html:57-60`, moved verbatim, plus the `oob` flag (UI-SPEC S4):
```html
        <button type="submit" id="scan-btn"
                {% if job and job.is_active %}disabled{% if job.is_busy %} aria-busy="true"{% endif %}{% endif %}>
            {% if job and job.state == JobState.AWAITING_FLIP %}Waiting for flip&#8230;{% elif job and job.is_busy %}Scanning&#8230;{% else %}Scan{% endif %}
        </button>
```
`type="submit" id="scan-btn"` must stay first and in that order (`tests/test_web_state_rendering.py:91-94` regex).

### `web/templates/partials/error.html` (NEW)

**Analog:** `partials/status.html:27-29`. Same glyph, class and hidden history loader, but **no** `role="alert"` on the `<p>` and no `Error:` prefix (UI-SPEC S2):
```html
    {% elif job.state == JobState.ERROR %}
      <p role="alert" class="status-error">&#10007; Error: {{ job.error }}</p>
      <div hx-get="/api/jobs/history" hx-target="#history-body" hx-swap="outerHTML" hx-trigger="load" class="htmx-hidden"></div>
```

### `web/templates/partials/status_response.html` (NEW)

**Analog:** include idiom at `index.html:65` (`{% include "partials/status.html" %}`). Content per UI-SPEC S4: include status, `{% with oob = true %}` include button, conditional OOB `<div id="status-message" hx-swap-oob="innerHTML"></div>`. The OOB button must **not** go into `partials/status.html` (duplicate id on `GET /`).

### `web/templates/base.html` and `index.html` (M)

`base.html:8,10` CDN tags -> vendored tags with `integrity`; add the `htmx-config` meta after line 6; delete line 19 (`<script src="/static/app.js">`). Exact markup: UI-SPEC S1. Line 2 stays `<html lang="en">`.

`index.html:7-9` form gains `hx-disabled-elt="#scan-btn"` and `hx-disinherit="hx-disabled-elt"`; `:19-20` gains `maxlength="256"`; `:57-60` becomes `{% include "partials/scan_button.html" %}`; between `:62` and `:65` insert `<div id="status-message" role="alert"></div>`.

### `web/static/app.css` (M)

**Analog:** lines 3-9. Insert the UI-SPEC S2 `#status-message > p` rule directly after it. The comment style is plain words, no planning IDs, as in lines 20-25:
```css
/* Status area */
#status-area {
    margin: 1rem 0;
    padding: 1rem;
    border-left: var(--pico-border-width) solid var(--pico-primary);
    border-radius: var(--pico-border-radius);
}
```
`tests/test_web.py:611-619` asserts no `!important` and that `var(--pico-border-width)` is present, so the new rule satisfies both.

---

### `.pre-commit-config.yaml` (M) and `.gitattributes` (NEW)

**Analog:** the file's own comment-first style (lines 1-16, 58-68). Add a top-level `exclude: ^src/saneless/web/static/vendor/` with a comment saying why (byte-rewriting hooks `end-of-file-fixer` `:59`, `mixed-line-ending` `:62-63`, `trailing-whitespace` `:70-71` would break SRI). `.gitattributes`: `src/saneless/web/static/vendor/** -text`.

### `.github/workflows/ci.yml` (M)

**Analog:** the `test` job, lines 33-51. Copy it verbatim, including the pinned SHAs and the version comments (line 1 rule: "comments must carry the full version"). Swap the final steps for `uv run playwright install --with-deps chromium` and `uv run pytest -m browser`:
```yaml
  test:
    name: test
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
      - name: Install SANE development headers
        run: |
          sudo apt-get update
          sudo apt-get install -y --no-install-recommends libsane-dev
      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1
      - run: uv sync --locked
      - run: uv run pytest -m "not browser and not sane_hardware"
```
`CONTRIBUTING.md:67,77,85-86` (job table, "run those locally") must be updated together with this.

---

### `tests/test_worker.py` (M)

**Conversion target pattern** (`TestWorkerFinish`, lines 1931-1953): `wait_for_state` fixture before `stop()`:
```python
    def test_finish_persists_done_for_a_success_outcome(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
        wait_for_state: Callable[..., Job],
    ) -> None:
        store = JobStore()
        try:
            monkeypatch.setattr(
                "saneless.worker.run_pipeline",
                lambda *_args, **_kwargs: _success_result(),
            )
            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()

            job = store.create_job("default", "Success Doc")
            worker.submit(job)
            finished = wait_for_state(store, job.id, TERMINAL_STATES)
            worker.stop()
```
**Tests to convert** (the `time.sleep(...)` + `stop()` shape at lines 209-217, 244-253, and the others in RESEARCH § Test Inventory):
```python
            job = store.create_job("default", "Worker Test")
            worker.submit(job)

            # Wait for processing
            time.sleep(0.5)
            worker.stop()
```
**Gated scanner for guard/stop/flip-abort tests** (`_GatedScanner`, lines 1238-1308; try/finally release at lines 1352-1355):
```python
        finally:
            scanner.release_all()
            worker.stop()
            store.close()
```
**Rewrite target** `TestLazyAutoGenerate` (lines 1531-1701): keep `_mock_caps_scanner` (lines 1534-1549), drop `monkeypatch.setattr("saneless.worker.resolve_config_path", ...)` (lines 1567-1570, 1676-1679), and inject the path through `Settings._config_path`. The assertion `"scanner unreachable" in caplog.text` (line 1660) inverts: the log must name the exception class.

**Store-raising guard tests**: monkeypatch store methods the way `test_web.py:518-522` swaps a raising callable onto an instance.

`wait_for_state` lives in `tests/conftest.py:228-295` (fixture `:281-295`); the default budget is 2.0 s.

---

### `tests/test_web.py`, `tests/test_web_state_rendering.py` (M)

**Fixture chain to reuse** (`test_web.py:78-129`): `test_settings` (two profiles, so **not** bare default, so no startup generation), `app`, `mock_paperless` (instance-attribute lambdas), `client` (`with TestClient(app) as tc`). `_app(client)` narrowing helper at `:45-51`.

**Adopt-as-current pattern** (`test_web.py:181-184`):
```python
    job_store: JobStore = _app(client).state.job_store
    job = job_store.create_job(profile="default", title="Polling Test")
    job_store.update_state(job.id, JobState.SCANNING)
    _app(client).state.worker._current_job_id = job.id
```
**Parametrised per-state rendering** (`test_web_state_rendering.py:244-272`): new T2 test ("poll OOB button == index button") copies `@pytest.mark.parametrize("state", list(JobState))` + `_job_in_state` + `_SCAN_BUTTON.search(...)`.

**Pitfall:** `test_web_state_rendering.py:100-106` builds a **bare default** `Settings` with a `_StubScanner` that reports a device, so startup generation now runs there (RESEARCH Pitfall 2). Decide per test: add a second profile or accept generated profiles.

---

### `tests/test_browser.py` (M)

**Session server** (lines 122-174) and the `_BrowserServer(url, app)` tuple (lines 108-119) are the base for the `context` egress override (RESEARCH Pattern 11), B5-B14, and a gate `threading.Event` on `_BrowserTestScanner.scan_pages` (lines 99-105).

**Per-test state + guaranteed cleanup** (`fallback_page`, lines 568-612): create or finish a row on the live store, set `worker._current_job_id`, and in `finally` clear the pointer **and** `delete_job`. Copy this for B6-B11; the teardown comment at lines 603-610 explains why deleting the row is required.

**To rewrite:** module docstring lines 8-14 and `_PICO_SURFACE` comment lines 57-69 (CDN wording); `test_scan_button_re_enables_after_a_fallback_swap` lines 661-684 (asserts `aria-busy == "false"` and uses `_DISABLE_SCAN_BUTTON` at `:350`, which mimics `app.js`).

**Load canaries to keep:** `test_pico_css_applied` (192-212), `test_htmx_loaded` (252-257).

---

### `tests/test_cache.py`, `tests/test_config.py`, `tests/test_auto_profiles.py`, `tests/test_job.py`, `tests/test_vocabulary.py` (M)

- **test_cache.py** (lines 12-17): one-behaviour-per-function style with a requirement id in the docstring (`(PLSS-05)`). The single-flight test adds `threading.Barrier` + call counter (RESEARCH Pattern 6), docstring `(ROBU-05)`.
- **test_config.py** `TestConfigFileSearch` (lines 107-142): `monkeypatch.chdir(tmp_path)` + `load_settings()`. Add `config_path` assertions on all three branches (explicit, searched, none) and a `SANELESS_CONFIG_PATH` env test that the path is not settable:
  ```python
  def test_no_config_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
      """No config file results in default settings."""
      monkeypatch.chdir(tmp_path)
      settings = load_settings()
      assert settings.scanner.host == ""
      assert "default" in settings.profiles
  ```
- **test_auto_profiles.py** `TestIsBareDefault` (lines 332-397): add one parametrised test over the RESEARCH Pattern 8 shapes table. Replace `TestResolveConfigPath` (RESEARCH cites `:400-411`).
- **test_job.py** `TestQueryMethods` (lines 1289-1380): `store = JobStore()` / `try` / `finally: store.close()`, seeded rows, exact-state asserts. `latest_run_job` and `probe` tests go here, plus a reopen test copying `test_worker.py:123-138` (`JobStore(db_path=...)` close/reopen).
- **test_vocabulary.py** (lines 65-86): add `"REJECTED"` to the membership set and update the "five documented categories (CTR-05)" docstring. New enums get the same membership + value-equals-name + total-label tests.

---

### NEW tests: `tests/test_web_errors.py`, `tests/test_cross_origin.py`, `tests/test_app_lifespan.py`, `tests/test_vendor_assets.py`

- **Fixtures:** copy `test_web.py:45-129` (`_app`, `StubScanner`, `test_settings`, `app`, `mock_paperless`, `client`). pytest 9 importlib mode means `from tests.test_web import ...` does work (`test_worker.py:29` does `from tests.conftest import scan_batch`). Copying the few fixtures is still clearer than cross-module fixture imports.
- **500 path:** `TestClient(app, raise_server_exceptions=False)` and a test-only `app.add_api_route` (RESEARCH Pitfall 4).
- **Sanitisation assertions:** copy `test_web.py:622-633` (assert the marker string is absent from `response.text`).
- **Lifespan tests:** seed rows in `SCANNING`/`PENDING` into a file-backed store (`OutputConfig(data_dir=str(tmp_path))`) *before* entering `with TestClient(app)`, then assert `RESTART_REASON` like `test_job.py:1301-1306`.
- **Vendor/SRI test:** `test_web.py:611-619` shows the file-path idiom; prefer importing `STATIC_DIR` / `TEMPLATE_DIR` from `saneless.web.app` (`app.py:38-39`) over a hand-built `Path(__file__).parent.parent / "src" ...`.

---

## Shared Patterns

### Total enum dispatch
**Source:** `src/saneless/vocabulary.py:268-300` (`flip_answer_label`), `:340-376` (`error_message`)
**Apply to:** `WorkerHealth` label / `/health`, `SubmitResult` mapping in `start_scan`, `RequestRejection` message lookup, the `error_message` `REJECTED` arm.
Single assignment per arm, `case _: assert_never(x)`, one `return`. ruff `PLR0911` caps returns at 6, so do not `return` inside each arm.

### Templates own no vocabulary
**Source:** `src/saneless/web/app.py:93-108`
**Apply to:** `error.html` (message passed in as a vocabulary constant), `scan_button.html` (uses the `JobState` global), `maxlength` (a registered global or context value from the same constant as `Form(max_length=...)`).

### Logging
**Source:** `src/saneless/worker.py:294-320`, `:369-373`; `routes.py:61-64`
**Apply to:** worker guard, degraded transition/recovery, abandoned job at shutdown, unpersisted profiles, 403 log line.
`%`-style args only (ruff `G`), `exc_info=True` with the real exception, never a guessed cause, `%r` for header values.

### Store method discipline
**Source:** `src/saneless/job.py:150-233` (statement constants with docstrings), `:749-790` (`@_locked` + `with self._conn:`)
**Apply to:** `latest_run_job`, `probe`.

### Thread-shared flags
**Source:** `src/saneless/worker.py:79-102` (`threading.Event` latch with a property reader)
**Apply to:** `_stopping`, `_degraded`, and read-only `health` / `is_alive` properties.

### Test resource hygiene
**Source:** `tests/test_worker.py:1335-1355` (try/finally: release gates, `stop()`, `close()`); `tests/test_browser.py:600-612` (delete session-store rows in teardown)
**Apply to:** every new threaded or browser test.

### Docstring and suppression rules
**Source:** `worker.py:50-77`, `routes.py:69-95` (finding ids in docstrings); `app.py:101-108` (typed widening instead of an ignore)
**Apply to:** all new/changed code. **Do not copy** the legacy suppressions at `config.py:229-230` (`# noqa: ARG003`) or `tests/test_web.py:636` (`# noqa: ANN202`).

### Response-with-status contract
**Source:** `routes.py:184-187`, `:203-206` (`{"status": "error", "detail": ...}`)
**Apply to:** the renderer's JSON branch, `/health` 503 degraded/down.

---

## No Analog Found

| File | Role | Data Flow | Reason / Use Instead |
|---|---|---|---|
| `src/saneless/web/cross_origin.py` | middleware | request-response | No middleware of any kind in the repo. RESEARCH Pattern 10 |
| `src/saneless/web/errors.py` (handler registration half) | exception handlers | request-response | No `add_exception_handler` anywhere. RESEARCH Pattern 4 (the response bodies do have analogs, above) |
| `src/saneless/web/static/vendor/*` | vendored assets | n/a | First vendored file. RESEARCH Pattern 9 (tarball fetch + SHA-512 check + SHA-384 compute) |
| `.gitattributes` | config | n/a | File does not exist yet. RESEARCH Pitfall 1 |
| `tests/test_vendor_assets.py` (SRI half) | test | file-I/O | No hashing test exists. RESEARCH Pattern 9 "Pinning test" |
| CI `browser` job egress gate (`context` fixture override in `tests/test_browser.py`) | test fixture | browser | No Playwright routing exists. RESEARCH Pattern 11 |

## Metadata

**Analog search scope:** `src/saneless/` (worker, job, vocabulary, config, auto_profiles, cli, web/*), `src/saneless/web/templates/**`, `src/saneless/web/static/`, `tests/` (conftest, test_worker, test_web, test_web_state_rendering, test_browser, test_cache, test_config, test_job, test_vocabulary, test_auto_profiles), `.github/workflows/ci.yml`, `.pre-commit-config.yaml`, `pyproject.toml`, `docs/reference/web-api.md`, `docs/explanation/architecture.md`, `CONTRIBUTING.md`
**Files scanned:** 27
**Pattern extraction date:** 2026-09-14
