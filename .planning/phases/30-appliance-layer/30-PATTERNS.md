# Phase 30: Appliance Layer - Pattern Map

**Mapped:** 2026-09-16
**Files analyzed:** 31 (6 new source/template files, 4 new test files, 21 modified)
**Analogs found:** 30 / 31 (1 partial — see § No Analog Found)

Every path below is relative to `/home/kris/git/saneless`. Line numbers were read on
2026-09-16 against the `autodev` working tree and are quoted so the planner can cite them
directly in plan actions.

---

## File Classification

### New files

| New file | Role | Data Flow | Closest analog | Match quality |
|---|---|---|---|---|
| `src/saneless/checks.py` | core library (registry + frozen result type) | request-response (probe → result list) | `src/saneless/vocabulary.py` (StrEnum + `match`/`assert_never` lookups) **and** `src/saneless/paperless.py::test_connection` (the probe body) | role-match (two analogs, split by concern) |
| `src/saneless/web/checks_cache.py` | service (TTL cache, last-known-good) | transform / cache | `src/saneless/web/cache.py::MetadataCache` | role-match (value type and failure semantics differ — see § Pitfall) |
| `src/saneless/web/refresher.py` | service (daemon thread) | event-driven / polling loop | `src/saneless/worker.py::ScanWorker` (`__init__` 357–408, `start` 405, `stop` 496–531) | exact (this is the named precedent, D-07) |
| `src/saneless/web/templates/partials/checks.html` | component (Jinja partial, htmx swap target) | request-response | `partials/status.html` (self-stopping poll idiom) + `partials/history.html` (loop over rows) | exact |
| `src/saneless/web/templates/partials/profile_description.html` | component (text-only partial) | request-response | `partials/tags.html` (single-expression partial) | exact |
| `src/saneless/web/templates/partials/terminal_reload.html` | component (hidden htmx loader) | event-driven | `partials/status.html:20,26,30,33` (the four duplicated hidden loaders it factors out) | exact |
| `tests/test_checks.py` | test (unit, pure) | request-response | `tests/test_vocabulary.py::TestErrorMessage` (parametrised completeness) | exact |
| `tests/test_checks_cache.py` | test (unit, clock) | transform | `tests/test_cache.py` — **as a counter-example**: it uses `time.sleep(1.1)`; the new file must inject a clock instead | partial (invert the analog) |
| `tests/test_web_checks.py` | test (route, TestClient) | request-response | `tests/test_web_state_rendering.py` (app build + `TestClient` + regex on markup) | exact |
| `tests/test_doctor.py` | test (CLI, CliRunner) | request-response | `tests/test_cli.py` (`_patch_cli` → `runner.invoke(cli, [...])`) | exact |

### Modified files

| Modified file | Role | Data Flow | In-file analog to copy from | Match quality |
|---|---|---|---|---|
| `src/saneless/vocabulary.py` | core library | transform | `error_message` (476), `connection_status_message` (525), `rejection_message` (588), `rejection_status_code` (661) | exact (add beside) |
| `src/saneless/config.py` | config schema | — | `ProfileConfig` (248–282), `OutputConfig` (~360), `Settings` (455–475), `resolve_job_title` (~333) | exact |
| `src/saneless/auto_profiles.py` | core library | transform | `_OWNED_KEYS` (491), `_generated_values` (590), `_duplex` (305), `_auto_source_mode` (280) | exact |
| `src/saneless/paperless.py` | service (HTTP client) | request-response | `test_connection` (949–992) | exact |
| `src/saneless/job.py` | model / store | CRUD | `create_job` (669–675), `JobResult` (534–551), `list_pending` (933) | exact |
| `src/saneless/worker.py` | service (thread) | event-driven | `_profiles_lock` (373) for the new `scanner_gate`; `current_job_id` property; `_process_job` `finally` (1246–1254) | exact |
| `src/saneless/pipeline.py` | service | streaming/event | `thumbnail_callback` on `PipelineRequest` (355) — the precedent for `pass_count_callback` | exact |
| `src/saneless/cli.py` | CLI controller | request-response | `jobs` command (705+), `_load_cli_settings` (451), `_failure_line` (267), `_GuardedGroup.invoke` (386+) | exact |
| `src/saneless/web/app.py` | config / composition | — | lifespan (72–143), filter registration (159–166), `app.state` block (150–155) | exact |
| `src/saneless/web/routes.py` | controller | request-response | `get_tags` (477), `invalidate_cache` (513), `start_scan` (371), `continue_flip` (565) | exact |
| `src/saneless/web/templates/index.html` | component | — | itself (the form block, 13–17 landmine comment) | exact |
| `partials/status.html`, `error.html`, `flip.html`, `history.html`, `scan_button.html`, `tags.html` | components | request-response | themselves | exact |
| `src/saneless/web/static/app.css` | config (styles) | — | `.status-fallback` amber token block (36–64), `.refresh-btn` (89–102), `@media (max-width: 576px)` (111) | exact |
| `tests/test_app_lifespan.py` | test | — | `test_shutdown_closes_resources_after_the_worker_stops` (302–340) | exact (extend) |
| `tests/test_vocabulary.py` | test | — | `TestErrorMessage` (285–300) | exact (retarget) |
| `tests/test_deployment_config.py` | test (doc-truth) | — | `test_cli_reference_command_exit_codes_are_real` (412–447) | exact (extend) |
| `tests/test_browser.py` | test (browser) | — | `context` fixture (284–311), `egress_allowlist` (267–277) | exact (extract gate) |
| `docker-compose.yml`, `docs/**` | config / docs | — | existing sections | exact |

---

## Pattern Assignments

### `src/saneless/checks.py` (core library, three-state registry)

**Analogs:** `src/saneless/vocabulary.py` (module shape, enums, lookups) and
`src/saneless/paperless.py:949-992` (probe body + ordered classification).

**Module-docstring / leaf-import convention** (`vocabulary.py:1-21`) — `checks.py` is
*not* a leaf (it needs `Settings`, the scanner backend and the Paperless client injected),
so state its own import rule explicitly in the same voice:

```python
"""
Shared vocabulary for the saneless domain -- every word the system uses about itself.

This is a leaf module.  It must not import from ``job.py``, ``pipeline.py``,
``worker.py``, ``cli.py``, or anything under ``web/``.  Every consumer imports
from here; nothing here imports from a consumer.  The only intra-package import
permitted is ``saneless.exceptions``, which is itself a leaf.
"""
```

`checks.py` must import **nothing from `web/` and nothing from `cli.py`** (D-02: both
surfaces read it). The check functions take their dependencies as parameters.

**`__all__` + explicit alphabetised export list** (`vocabulary.py:23-52`):

```python
__all__ = [
    "ACTIVE_STATES",
    ...
    "connection_status_message",
    "error_message",
    ...
]
```

**StrEnum with a doc-truth docstring** (`vocabulary.py:227-249`) — copy this shape for
`CheckState` and `CheckKey`. Note the docstring explains *why* the values are spelled the
way they are; `CheckKey`'s should say it is the "neither surface may define a check the
other does not have" contract (D-02):

```python
class RequestRejection(StrEnum):
    """
    Every error the web layer renders, one member per message.

    ``rejection_message`` and ``rejection_status_code`` give each member its
    user-facing sentence and its HTTP status.  The messages are developer
    constants: none of them contains request input or exception text, so
    nothing a client sent and nothing internal can reach the page through this
    path (V7).
    """

    QUEUE_FULL = "QUEUE_FULL"
    WORKER_DOWN = "WORKER_DOWN"
    ...
```

**`match` + `assert_never` lookup — the mandatory shape for every new label/message/
class/glyph function** (`vocabulary.py:525-557`). Note: one `message` local assigned in
every arm, `assert_never` in `case _`, a single `return` at the end, and a `Raises:`
docstring section:

```python
def connection_status_message(status: ConnectionStatus) -> str:
    """
    Return the plain-language user message for a connection-test outcome.

    Every message is a developer-authored constant.  No status code, no
    response body, no URL and no token is interpolated, so a paperless-ngx
    error page cannot reach the UI through this path.

    Args:
        status: The connection-test outcome to describe.

    Returns:
        A short sentence a non-technical reader can act on.

    Raises:
        AssertionError: If the value is not a ConnectionStatus member.

    """
    match status:
        case ConnectionStatus.CONNECTED:
            message = "Connected to paperless-ngx."
        case ConnectionStatus.TOKEN_REJECTED:
            message = "Paperless-ngx rejected the API token."
        case ConnectionStatus.NOT_FOUND:
            message = "The paperless-ngx API was not found at that URL."
        case ConnectionStatus.SERVER_ERROR:
            message = "Paperless-ngx returned a server error."
        case ConnectionStatus.UNREACHABLE:
            message = "Could not reach paperless-ngx."
        case _:
            assert_never(status)
    return message
```

**These five sentences are the Paperless check's `message` column verbatim** (UI-SPEC S1);
the check calls `connection_status_message(...)` rather than re-authoring them.

**Frozen dataclass result** — the codebase's frozen-dataclass idiom is
`routes.py:246-256`:

```python
@dataclass(frozen=True, slots=True)
class _ScanForm:
    """The validated fields of one scan submission."""

    profile: str
    title: str
    tags: list[int]
    correspondent: int | None
```

Use `@dataclass(frozen=True, slots=True)` for `CheckResult` and for `ErrorAdvice` (D-10).

**Ordered-comparison classification is allowed only where `assert_never` cannot apply** —
`paperless.py:958-971` documents the exception and why it is one. Cite it if a check body
uses an `if` chain instead of a `match`:

```python
        The classification is an ordered chain of integer comparisons rather
        than a ``match`` with ``assert_never``, for the same reason
        ``vocabulary.classify_error`` is an ``isinstance`` chain: the input
        is a range of integers, not a closed set of members, so exhaustive
        matching does not apply and a trailing fallback is the correct total
        answer.
```

**Module-level tunables as `Final`, "read at call time"** (`worker.py:50-67`) — copy this
comment discipline for `_PROBE_CONNECT_SECONDS`, `_PROBE_READ_SECONDS`, `_SANED_PORT`:

```python
# How long stop() waits for the worker thread before reporting it still alive
# (D-08).  Five seconds leaves room for uvicorn inside Docker's 10 s SIGKILL
# grace.  Deliberately not configurable.  Read at call time, so tests can
# shorten it.
STOP_JOIN_SECONDS: Final = 5.0
```

---

### `src/saneless/web/refresher.py` (service, daemon thread)

**Analog:** `src/saneless/worker.py::ScanWorker` — named by CONTEXT D-07 as *the*
precedent.

**Thread construction** (`worker.py:369-374`):

```python
        self._queue: queue.Queue[Job] = queue.Queue(maxsize=_QUEUE_DEPTH)
        self._thread = threading.Thread(target=self._run, daemon=True)
        # Set once by stop(); read by the loop, submit() and the flip callback.
        self._stopping = threading.Event()
        # Guards every read and every rebind of self._settings.profiles (D-19).
        self._profiles_lock = threading.Lock()
```

Copy: `daemon=True`, a `_stopping` `Event` built in `__init__`, a small `Lock` per shared
mutable (the "someone is watching" float goes behind its own lock, exactly as
`_profiles_lock` guards the profiles).

**`start()` is separate from `__init__` and logs** (`worker.py:405-408`):

```python
    def start(self) -> None:
        """Start the worker thread."""
        self._thread.start()
        logger.info("ScanWorker started")
```

**`stop()` returns whether it stopped, with a bounded join** (`worker.py:496-531`) — this
is the whole contract A-7 depends on:

```python
    def stop(self) -> bool:
        """
        Stop the worker without waiting on its queue, and report whether it stopped.
        ...
        The join is bounded by ``STOP_JOIN_SECONDS`` (D-08).  When this returns
        ``False`` the thread is still running and may still write to the job
        store, so the caller must leave the store and the Paperless client open
        (D-09).  Calling it again, or on a worker never started, is safe.

        Returns:
            Whether the worker thread has stopped.

        """
        self._stopping.set()
        ...
        if self._thread.is_alive():
            self._thread.join(timeout=STOP_JOIN_SECONDS)
        stopped = not self._thread.is_alive()
        if stopped:
            logger.info("ScanWorker stopped")
        return stopped
```

`CheckRefresher.stop()` copies all four properties: set the event first, join only if
alive, recompute liveness after the join, return the boolean, log on success. Import
`STOP_JOIN_SECONDS` from `saneless.worker` rather than redefining it (`web/app.py:25`
already imports it: `from saneless.worker import STOP_JOIN_SECONDS, ScanWorker`).

**Idle sleep is `Event.wait(timeout)`, never `time.sleep`.** The worker gets the
wake-immediately property from `queue.shutdown(immediate=True)`; the refresher gets it from
`while not self._stopping.wait(_TICK_SECONDS):`.

**Testability requirement (from RESEARCH §9):** expose the loop body as a synchronous
`_tick()` so the logic tests need no thread at all; reserve the threaded test for
start/stop/join. The suite forbids new `time.sleep` calls.

---

### `src/saneless/web/checks_cache.py` (service, TTL + last-known-good)

**Analog:** `src/saneless/web/cache.py::MetadataCache` — **pattern, not drop-in.**

**Copy this — the lock/TTL/single-flight skeleton** (`cache.py:27-56`):

```python
    def __init__(self, ttl: int = 60) -> None:
        """Initialize the cache with the given TTL."""
        self._ttl = ttl
        self._store: dict[str, tuple[float, list[dict[str, object]]]] = {}
        # Guards creation of the per-key locks, the generation counters, and
        # the store-if-unchanged check below; never a fetch.
        self._locks_guard = threading.Lock()
        ...

    def get(self, key: str) -> list[dict[str, object]] | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, data = entry
        if time.monotonic() - ts < self._ttl:
            return data
        return None
```

**Deviate in exactly three ways, and say so in the new class's docstring:**

1. **Injectable clock.** `cache.py:54,66` call `time.monotonic()` inline, which is the sole
   reason `tests/test_cache.py:26` has to `time.sleep(1.1)`:
   ```python
   def test_cache_ttl_expiry() -> None:
       """Cached entries expire after TTL elapses (PLSS-05)."""
       cache = MetadataCache(ttl=1)
       cache.set("tags", [{"id": 1, "name": "receipt"}])
       assert cache.get("tags") is not None
       time.sleep(1.1)
       assert cache.get("tags") is None
   ```
   Take `clock: Callable[[], float] = time.monotonic` as a constructor parameter.
2. **Keep last-known-good.** `MetadataCache.get_or_fetch` re-raises and caches nothing
   (`cache.py:69-116`, and `routes.py:79-86` is what swallows it). D-08 needs the previous
   results plus their age retained.
3. **Typed to `Sequence[CheckResult]`,** not `list[dict[str, object]]`.

---

### `src/saneless/web/routes.py` (controller, request-response)

**Analog for a new GET partial route** — `get_tags` (`routes.py:477-492`). Note the
`state = request.app.state` first line, the `Response` return type, and the
`TemplateResponse(request, "partials/x.html", {...})` three-arg call:

```python
@router.get("/api/tags")
def get_tags(request: Request) -> Response:
    """
    Fetch tag options for the dropdown selector.

    Uses cached data when available, falling back to a fresh fetch
    from paperless-ngx. Returns empty options on API errors.
    """
    state = request.app.state
    tags = _get_cached_or_fetch(state.cache, state.paperless, "tags")
    return state.templates.TemplateResponse(
        request,
        "partials/tags.html",
        {"tags": tags},
    )
```

**Analog for validating a new query parameter** (`routes.py:50-54`, used at `:514`) — this
is the shape the profile-description route's `profile` parameter must follow, and the tag
filter's `q` must be bounded the same way:

```python
# The only metadata resources the cache holds.  A runtime alias, not a
# TYPE_CHECKING import, because FastAPI reads it to validate the ``resource``
# query parameter: anything else is a 422 instead of reaching the cache (N-20).
MetadataResource = Literal["tags", "correspondents"]
```

**Analog for "every handler is a plain `def`"** (`routes.py:40-46`) — the new
`/api/checks` routes must be `def`, not `async def`:

```python
# Every handler below is a plain ``def`` on purpose.  Each one calls blocking
# code -- sync httpx to Paperless, sqlite through the job store, the worker --
# and FastAPI runs ``def`` handlers on its threadpool, so a slow Paperless call
# cannot stall ``/health`` or the status poll (ROBU-05, M-01).
```

**Analog for the refusal guard (D-15/APPL-07)** — `start_scan` (`routes.py:419-426`).
Copy the placement: validate → build `_ScanForm` → refuse before `create_job`:

```python
    unhealthy = _unhealthy_rejection(state.worker.health)
    if unhealthy is not None:
        rejection, error = unhealthy
        written = _record_refused_submit(state.job_store, form, error=error)
        raise RequestRejected(rejection, refresh_history=written)
```

The placeholder-token guard is the same three lines with a new rejection member and the
new job-row error constant. Do **not** invent a new refusal mechanism: `RequestRejected`
(`web/errors.py:72-96`) already derives status and detail from the vocabulary:

```python
        super().__init__(
            status_code=rejection_status_code(rejection),
            detail=rejection_message(rejection),
        )
```

**Analog for attaching the owner cookie (D-23)** — `start_scan` currently *returns the
expression* (`routes.py:434-439`); it must become a named local:

```python
            return state.templates.TemplateResponse(
                request,
                "partials/status_response.html",
                {"job": job, "flip_answer": None, "clear_message": True},
            )
```

becomes

```python
            response = state.templates.TemplateResponse(...)
            if minted:
                response.set_cookie(
                    "saneless_owner", token,
                    httponly=True, samesite="lax", path="/",
                )
            return response
```

(no `max_age`/`expires` → session cookie; no `secure` → the LAN deployment is plain HTTP.)

**Analog for the owner comparison's failure mode** — `continue_flip` (`routes.py:565-596`)
already establishes "a dropped answer returns the current status, not an error" (D-16). A
non-owner POST does the same:

```python
    The posted ``job_id`` scopes the answer (CR-01).  A Continue for any other
    job, one sent before that job reached the flip prompt, or one arriving
    after the prompt was already answered is dropped -- and the route still
    returns the current status rather than an error (D-16).
```

**Analog for the followed-job id (D-25)** — `_status_context` (`routes.py:131-171`) takes
an optional extra fact (`claimed`) alongside the inferred job, and documents why. The
followed job id lands as a second such parameter, with the same docstring discipline.

---

### `src/saneless/web/app.py` (composition: lifespan + filters)

**Lifespan shutdown gate (A-7)** — the branch to extend, verbatim (`app.py:121-142`):

```python
        yield
        # worker.stop() blocks the event loop for at most STOP_JOIN_SECONDS,
        # during lifespan shutdown, after uvicorn has stopped serving (D-08).
        if not worker.stop():
            # D-09: the store closes only after a confirmed stop, so a stuck
            # thread never hits "Cannot operate on a closed database".  D-07:
            # the abandoned job is not written here -- that would race its own
            # final write; the next startup's recovery records it.
            logger.warning(
                "Scan worker did not stop within %s s (job %s still running); "
                "leaving the job store, Paperless client and scanner open for "
                "process exit",
                STOP_JOIN_SECONDS,
                worker.current_job_id,
            )
            return
        paperless.close()
        job_store.close()
        # Last, and only here: closing the scanner shuts SANE down for the
        # whole process, and sane_exit() closes every open handle while
        # holding the GIL.  A worker that did not stop may still be inside a
        # read, which is why the branch above returns instead (D-18).
        scanner.close()
        logger.info("App shutdown complete")
```

Set **both** stop events before joining either, so the two bounded joins overlap and the
worst case stays `STOP_JOIN_SECONDS` rather than doubling.

**Jinja filter registration — the only place templates get vocabulary** (`app.py:156-166`):

```python
    app.state.templates = Jinja2Templates(directory=str(TEMPLATE_DIR))
    # Registered before any template is loaded, which is the only requirement
    # Jinja places on mutating `filters` and `globals` on a live Environment.
    # The templates own no vocabulary of their own: labels come from these
    # filters and state comparisons go through the JobState global.
    app.state.templates.env.filters["state_label"] = state_label
    app.state.templates.env.filters["progress_label"] = progress_label
    app.state.templates.env.filters["flip_answer_label"] = flip_answer_label
```

Every new filter this phase needs — `check_state_class`, `check_state_glyph`,
`check_state_label`, `check_name`, `error_message`, `error_next_step`, `page_counts`,
`local_time` — is registered in this block and implemented in `vocabulary.py` (or
`checks.py` for the check ones). No `{% if state == 'FAIL' %}` in a template.

**`app.state` is the injection channel** (`app.py:150-155`) — `app.state.checks` and
`app.state.refresher` join it:

```python
    app.state.worker = worker
    app.state.job_store = job_store
    app.state.settings = settings
    app.state.paperless = paperless
    app.state.cache = cache
```

---

### `src/saneless/cli.py` (`doctor` command)

**Analog:** the `jobs` command (`cli.py:705-762`) — a non-SANE command that loads settings,
prints a table and exits.

**Command skeleton** (`cli.py:705-712`):

```python
@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.option("--limit", default=20, type=int, help="Maximum jobs to show.")
@click.pass_context
def jobs(ctx: click.Context, *, as_json: bool, limit: int) -> None:
    """List recent scan job history."""
    settings = _load_cli_settings(ctx)
```

`doctor` copies: `@cli.command()` + `@click.pass_context`, a one-line docstring (click uses
it as help; ruff `D` requires it), `settings = _load_cli_settings(ctx)` as the first
statement. **A-1: `doctor` must NOT call `require_sane()`** — contrast `scan`
(`cli.py:515-519`), which does and which is the pattern to deliberately *not* copy:

```python
def scan(ctx: click.Context, profile: str, title: str) -> None:
    """Scan a document and upload to paperless-ngx."""
    # python-sane is mandatory: a command that needs it refuses before loading
    # config or touching the device, exit 2 through the guard (D-05). --help
    # never reaches this body, so it needs no python-sane (CFG-10).
    require_sane()
```

**Terminal-width table rendering** (`cli.py:742-761`) — the exact block D-35's zone suffix
must keep fitting, and the analog for `doctor`'s own table:

```python
            cols = shutil.get_terminal_size((80, 24)).columns
            ts_w = 22
            profile_w = 15
            # Three single spaces separate the four columns.
            title_w = max(15, cols - (ts_w + profile_w + _STATUS_COL_WIDTH + 3))
            header = (
                f"{'Timestamp':<{ts_w}} {'Profile':<{profile_w}} "
                f"{'Title':<{title_w}} {'Status'}"
            )
            click.echo(header)
            click.echo("-" * min(len(header), cols))
            for j in recent:
                click.echo(
                    f"{j.created_at.strftime('%Y-%m-%d %H:%M:%S'):<{ts_w}} "
                    f"{_truncate(j.profile, profile_w):<{profile_w}} "
                    f"{_truncate(j.title, title_w):<{title_w}} "
                    f"{state_label(j.state)}"
                )
```

Change the format string to `local_time(j.created_at)` (drops seconds, gains `%Z`, `ts_w`
stays 22). **Derive a column width, never hard-code it** — `cli.py:77-81` is the precedent:

```python
# Width of the Status column in `saneless jobs`, derived rather than written
# down: the humanised labels are longer than the raw enum values they replaced,
# and a ninth JobState member must not be able to overflow an 80-column
# terminal without anyone noticing.
_STATUS_COL_WIDTH = max(len(state_label(state)) for state in JobState)
```

`doctor`'s name column gets the same treatment: `max(len(check_name(k)) for k in CheckKey)`.

**Exit code — through the guard, never a bare `sys.exit`.** `_GuardedGroup.invoke`
(`cli.py:386-424`) produces every code with `ctx.exit(code)`; `doctor` calls
`ctx.exit(ExitCode.CONFIG)` itself when any check is `FAIL` (no new `ExitCode` member —
`tests/test_deployment_config.py:393,403` pin the enum against the docs).

**The second advice line (D-12)** — `_failure_line` (`cli.py:267-297`) is the first line
and must stay byte-identical; the `Try: ` line is printed after it in the guard's
`SanelessError` arm (`cli.py:412-420`):

```python
        except SanelessError as exc:
            category = classify_error(exc)
            code = exit_code_for(category)
            if code is ExitCode.UNEXPECTED:
                _report_unexpected(ctx, exc)
            else:
                _log_failure(ctx, exc)
                click.echo(_failure_line(exc, category), err=True)
            ctx.exit(code)
```

---

### `src/saneless/vocabulary.py` — `error_advice` (D-10), new rejection member (D-15)

**Analog:** `error_message` (`vocabulary.py:476-521`). Its docstring is the thing this
phase retires — the plan must rewrite it, not leave it:

```python
    This function is deliberately NOT wired to any template, route, or CLI
    output yet.  The status partial keeps rendering ``job.error`` verbatim,
    because the specific messages are more truthful today than a generic
    category sentence would be -- swapping them now would be a user-visible
    regression.  The plain-language display arrives with the error-message
    rework; until then the completeness test is this function's only consumer.
```

**One `match`, not two** (Pitfall 7): fold `error_message` into `error_advice` returning a
frozen `ErrorAdvice`, and keep `error_message` as a one-line accessor if at all.

**New `RequestRejection` member** — add to the enum (`vocabulary.py:238-249`), then to
**both** `rejection_message` (588) and `rejection_status_code` (661). Note
`rejection_status_code` groups members that share a code, so the new member joins the
existing 503 arm:

```python
        case RequestRejection.WORKER_DOWN | RequestRejection.WORKER_DEGRADED:
            status_code = 503
```

**New job-row error constant** — sits with its siblings (`vocabulary.py:257-263`), and the
comment states the no-trailing-period rule the UI-SPEC's
`Not started: the paperless-ngx API token has not been set` obeys:

```python
# Job-row error texts.  A submit refused because the queue was full or the
# worker was down or degraded still writes a job row, so history shows the
# attempt (D-05); these are that row's ``error``.  Like every other
# ``job.error`` they carry no trailing period.
QUEUE_FULL_JOB_ERROR: Final = "Not started: the scan queue was full"
WORKER_DOWN_JOB_ERROR: Final = "Not started: the scan service was not running"
WORKER_DEGRADED_JOB_ERROR: Final = "Not started: the scan service was unavailable"
```

---

### `src/saneless/config.py` — `ProfileConfig.label` / `.description`, `[web]` section

**Analog:** `ProfileConfig` (`config.py:248-282`). Note `extra="forbid"`, and that
`default_title` is the precedent for a bounded free-text field:

```python
class ProfileConfig(BaseModel):
    """Scan profile configuration."""

    # extra="forbid" (CFG-01) is safe alongside the legacy-duplex
    # before-validator: it only ever adds ``duplex``, which is a real field.
    # populate_by_name keeps both ``title`` and ``default_title`` accepted.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source: str = "Flatbed"
    ...
    # A literal title, not a template: no placeholder vocabulary (D-15). Used
    # when a scan is submitted with a blank title (resolve_job_title, D-16).
    # Bounded because the route's Form(max_length=...) only checks the typed
    # title, so an unbounded profile title would bypass ROBU-08.
    default_title: str = Field(default="", alias="title", max_length=TITLE_MAX_LENGTH)
```

`label` and `description` default to `""` (so an old config still loads) and carry an
explicit `max_length` (they are rendered into HTML).

**Analog for the new `[web]` section** — `PaperlessConfig` (`config.py:220-232`) is the
smallest nested-model example, and `Settings` (`config.py:465-471`) is where it is hung:

```python
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)
    paperless: PaperlessConfig = Field(default_factory=PaperlessConfig)
    output: OutputConfig = Field(default_factory=OutputConfig)
    profiles: dict[str, ProfileConfig] = {"default": ProfileConfig()}
```

```python
    # default_factory, not a plain instance: an ``OutputConfig()`` default is
    # built once at import and would freeze the HOME/XDG state defaults
    # (CFG-03, RESEARCH Pitfall 3). scanner and paperless match for consistency.
```

`WebConfig` uses `default_factory=WebConfig` for the same reason, and
`model_config = ConfigDict(extra="forbid")`.

**Placeholder-token predicate** — the token is `SecretStr` (`config.py:231`) and its
comment names the only two unwrap sites:

```python
    # Masked in repr, tracebacks and model_dump (CFG-05, N-15). Unwrapped with
    # get_secret_value only where PaperlessClient is built: cli.py scan and
    # web/app.py create_app.
    token: SecretStr = SecretStr("")
```

The predicate must take the already-unwrapped string (or unwrap in exactly one new named
place) and must never log or render it.

**`resolve_job_title` fallback** (`config.py:333`) — the line that goes local, with the
docstring that points at this phase:

```python
    return f"Scan {now.astimezone(UTC).strftime('%Y-%m-%d %H:%M')}"
```

---

### `src/saneless/auto_profiles.py` — `label` / `description` as owned keys (D-18)

**Analog:** `_OWNED_KEYS` (`auto_profiles.py:485-498`) — the tuple order *is* the file key
order for a newly written table, which is why UI-SPEC/RESEARCH put the new keys first:

```python
# The keys a generation writes, and so the keys the tool owns in a profile that
# carries ``auto_generated = true`` (D-02). ``--force`` overwrites exactly these
# on the existing table and deletes any of them the fresh generation omits
# (D-03); every other key -- default_tags, title, thresholds -- is the user's.
# A hand edit to an owned key is overwritten while the flag is set: to keep it,
# remove ``auto_generated`` and the profile is never touched again (D-01).
_OWNED_KEYS: Final = (
    "source",
    "resolution",
    "mode",
    "auto_source_mode",
    "duplex",
    "auto_generated",
)
```

**Analog:** `_generated_values` (`auto_profiles.py:590-622`) — note the conditional
emission for non-default values, which `label`/`description` deliberately do **not** copy
(they are emitted unconditionally, which makes D-03's delete branch unreachable for them):

```python
    values: dict[str, str | int | bool] = {
        "source": profile.source,
        "resolution": profile.resolution,
        "mode": profile.mode,
    }
    if profile.auto_source_mode != "flatbed":
        values["auto_source_mode"] = profile.auto_source_mode
    ...
    if profile.duplex != "none":
        values["duplex"] = profile.duplex
    values["auto_generated"] = True
    return values
```

**Analog for the derivation helpers** — `_duplex` (`auto_profiles.py:305-336`) and
`_auto_source_mode` (`auto_profiles.py:280-302`). Both are `Literal`-returning pure
functions on `(source, *, has_flatbed)`; the new `_profile_label` / `_profile_description`
are siblings with the same signature style (keyword-only booleans — a positional boolean
fails this project's lint rules, see `_auto_source_mode`'s `Args:` note). `_duplex`'s
docstring already names this phase as its reader and must be updated once it has one:

```python
    Nothing reads ``"hardware"`` (D-05). It records operator intent and makes a
    generated profile self-describing, and Phase 30's APPL-05 -- generated
    ``label`` / ``description`` such as "Feeder, double-sided" -- is its
    eventual reader.
```

---

### `src/saneless/paperless.py` — bounded probe

**Analog:** `test_connection` (`paperless.py:949-992`). The only change is a `timeout`
parameter on the one `get`; the `except httpx.TransportError` arm already covers
`ConnectTimeout`/`ReadTimeout` and its comment says so:

```python
        try:
            response = self._client.get("/api/tags/", params={"page_size": 1})
        except httpx.TransportError:
            # The base class of ConnectError, ConnectTimeout and ReadTimeout.
            # Catching only ConnectError let the timeout siblings escape to
            # routes.py's blanket handler, which answers HTTP 502
            # {"status": "error"} -- none of the five outcomes.
            logger.warning("Paperless is unreachable")
            return ConnectionStatus.UNREACHABLE
```

Add `timeout: httpx.Timeout | None = None` and pass
`timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT`, leaving the existing
`GET /api/paperless/test` route's behaviour untouched.

---

### `src/saneless/job.py` — `owner_token` writer (Pitfall 5)

**Analog:** `create_job` (`job.py:669-680`) — it already has exactly five non-`self`
parameters, so a sixth trips `PLR0913` (`max-args = 5`) and this project adds no
suppressions:

```python
    @_locked
    def create_job(
        self,
        profile: str,
        title: str,
        tags: list[int] | None = None,
        correspondent: int | None = None,
        thumbnail: str | None = None,
    ) -> Job:
```

The in-tree answer is the frozen-dataclass bundle — `JobResult` (`job.py:534-551`) exists
for exactly this reason and says so in its docstring, and `_ScanForm` (`routes.py:246-256`)
is already almost the right shape. **Verify before dropping `thumbnail`:** RESEARCH
assumption A5 claims nothing passes it (`update_thumbnail` at `worker.py:1288` is the live
writer) and flags it as unverified.

**Analog for the queue-position source** — `list_pending` (`job.py:933-958`), whose
docstring names APPL-08 and must be updated once it has a caller:

```python
        This method has NO production caller in this phase.  Showing a job its
        position in the queue is APPL-08, which belongs to Phase 30; until then
        its tests are its only consumer.
```

---

### Templates

#### `partials/checks.html` (new)

**Analog for the self-stopping poll** — `partials/status.html:1-6`. This is the in-tree
idiom the UI-SPEC mandates instead of HTTP 286: the attributes are emitted only while the
poll is wanted, so the swapped-in replacement ends it:

```jinja
<div id="status-area"
     {% if job and job.is_active %}
     hx-get="/api/jobs/current/status"
     hx-trigger="every 1s"
     hx-swap="outerHTML"
     {% endif %}>
```

**Analog for a row loop with an empty state** — `partials/history.html:1-15`, which also
shows the `state → class` mapping style the check rows replace with a filter (the strip
must use `{{ c.state | check_state_class }}`, *not* an inline `{% if %}` chain — the
template owns no vocabulary):

```jinja
<tbody id="history-body">
{% for job in jobs %}
<tr>
  <td>{{ job.created_at.strftime("%Y-%m-%d %H:%M") }}</td>
  ...
  <td class="{% if job.state == JobState.DONE %}status-done{% elif ... %}">
    {{ job.state | state_label }}
  </td>
</tr>
{% endfor %}
{% if not jobs %}
<tr><td colspan="4">No scan history yet.</td></tr>
{% endif %}
</tbody>
```

The `{{ job.created_at.strftime(...) }}` on line 4 is the exact expression S7 replaces with
`{{ job.created_at | local_time }}`.

**Analog for an out-of-band swap wrapper** — `partials/status_response.html` (whole file).
The `refresh_checks` flag (UI-SPEC S1) is modelled on `clear_message`, including the
comment explaining why *only* one route sets it:

```jinja
{# Used by every htmx route that renders status: scan success, the status poll,
   and flip continue/abort.  The status area is the swap target; the Scan button
   rides along out-of-band so the server alone decides its state (ROBU-04).
   The #status-message clear is for a successful scan only: a poll or flip
   response carrying it would erase a 429 shown mid-scan within a second (D-03). #}
{% include "partials/status.html" %}
{% with oob = true %}{% include "partials/scan_button.html" %}{% endwith %}
{% if clear_message %}<div id="status-message" hx-swap-oob="innerHTML"></div>{% endif %}
```

#### `partials/terminal_reload.html` (new)

**Analog:** the four identical hidden loaders in `partials/status.html:20,26,30,33` that it
factors out:

```jinja
<div hx-get="/api/jobs/history" hx-target="#history-body" hx-swap="outerHTML" hx-trigger="load" class="htmx-hidden"></div>
```

`.htmx-hidden` is `display: none` (`app.css:135-137`) — reuse it, do not add a new class.

#### `partials/scan_button.html` (modified)

**Analog:** itself. The file header is a load-bearing constraint, not decoration — the new
`scan_blocked` flag must not disturb the first two attributes:

```jinja
{# The ONLY copy of the Scan button markup (ROBU-04, C-10).  index.html includes
   it inline; partials/status_response.html includes it with oob = true so every
   status response re-renders it from server state.  Keep type="submit"
   id="scan-btn" as the first two attributes, in that order: the _SCAN_BUTTON
   regex in tests/test_web_state_rendering.py depends on it.  Never include this
   inside partials/status.html -- the page includes that partial too, so the
   page would carry a duplicate id="scan-btn". #}
<button type="submit" id="scan-btn"{% if oob %} hx-swap-oob="true"{% endif %}
        {% if job and job.is_active %}disabled{% if job.is_busy %} aria-busy="true"{% endif %}{% endif %}>
    {% if job and job.state == JobState.AWAITING_FLIP %}Waiting for flip&#8230;{% elif job and job.is_busy %}Scanning&#8230;{% else %}Scan{% endif %}
</button>
```

#### `index.html` (modified)

**Analog:** itself. The form comment is the landmine guard (Pitfall 8) — the UI-SPEC's
solution is that **nothing is added to the `<form>` element**, so this block stays byte-
identical:

```jinja
    {# hx-disabled-elt replaces the deleted app script's beforeRequest handler:
       the button is disabled for the round-trip, so there is no double submit.
       hx-disinherit is mandatory: the tags/correspondent selects and refresh
       buttons inside this form issue their own requests and, on htmx 2.0.8, an
       inherited hx-disabled-elt strips disabled from a server-disabled button
       when they finish (C-10 again; fixed only in 2.0.9). #}
    <form hx-post="/api/scan"
          hx-target="#status-area"
          hx-swap="outerHTML"
          hx-disabled-elt="#scan-btn"
          hx-disinherit="hx-disabled-elt">
```

**Analog for the tag/correspondent refresh button** (`index.html:30-45`) — the markup the
checkbox fieldset replaces, and the `.refresh-btn` + `.sr-only` pattern the new `<legend>`
keeps:

```jinja
        <label for="tags-select">
            Tags
            <button type="button"
                    hx-post="/api/cache/invalidate?resource=tags"
                    hx-target="#tags-select"
                    hx-swap="innerHTML"
                    class="refresh-btn"
                    title="Refresh tags"
                    aria-label="Refresh tags">&#x21bb;<span class="sr-only">Refresh tags</span></button>
        </label>
        <select name="tags" id="tags-select" multiple
                hx-get="/api/tags" hx-trigger="load" hx-target="this" hx-swap="innerHTML">
            {% for tag in tags %}
            <option value="{{ tag.id }}">{{ tag.name }}</option>
            {% endfor %}
        </select>
```

Note the swap target/`hx-swap` change: the `<select>` swapped `innerHTML`; the checkbox
list is a `<div id="tags-list">` swapped `outerHTML`, so `partials/tags.html` must render
the wrapper too.

---

### `src/saneless/web/static/app.css`

**Analog for a new colour token with a light/dark pair** — `.status-fallback`
(`app.css:36-64`). The amber `warn` colour **is this token**; do not add a new value:

```css
/* Consume-directory fallback: a degraded success, so amber -- deliberately
   neither the ins green nor the del red. PicoCSS has no amber among its
   semantic tokens, so the app owns this one and gives it a light and a dark
   value; the darker amber is unreadable on a dark surface. The dark selectors
   mirror PicoCSS v2's own: automatic dark applies only when <html> carries no
   data-theme attribute, and forced dark uses data-theme="dark". */
:root {
    --saneless-status-fallback: #a16207;
}

@media only screen and (prefers-color-scheme: dark) {
    :root:not([data-theme]) {
        /* Paired with the [data-theme="dark"] block below -- same value, and
           it has to be written out twice. Change one, change the other. */
        --saneless-status-fallback: #ca8a04;
    }
}

[data-theme="dark"] {
    --saneless-status-fallback: #ca8a04;
}

.status-fallback {
    color: var(--saneless-status-fallback);
}
```

`.check-warn` is an alias over `--saneless-status-fallback`; `.check-ok` / `.check-fail`
alias the existing ins/del tokens (`app.css:20-26`); `.check-checking` aliases
`--pico-muted-color` directly (`.status-cancelled`, `app.css:27-34`, is the precedent for
using a Pico token with no app-owned pair).

**Analog for a rule with a stated reason** — every block in this file opens with a
sentence explaining *why*. New rules (`.tag-list`, `label.tag-option`,
`details.tech-details > summary`, `#scan-blocked-reason`) must do the same; the UI-SPEC
already drafted those comments.

**Analog for the mobile breakpoint** — `@media (max-width: 576px)` (`app.css:111+`) already
gives `#history-body td` wrapping, which is why S3's two-line Title cell needs no new
breakpoint.

---

### Tests

#### `tests/test_checks.py`, `tests/test_vocabulary.py` (completeness)

**Analog:** `TestErrorMessage` (`tests/test_vocabulary.py:284-300`) — parametrised over
`list(EnumType)`, so a new member forces a decision:

```python
class TestErrorMessage:
    """error_message category-message lookup tests."""

    @pytest.mark.parametrize("category", list(ErrorCategory))
    def test_error_message_is_complete(self, category: ErrorCategory) -> None:
        """Every ErrorCategory has a plain-language message (CTR-05)."""
        message = error_message(category)
        assert message
        assert message != category.value

    def test_error_message_strings(self) -> None:
        """error_message returns developer-authored prose, not exception text (CTR-05)."""
        assert error_message(ErrorCategory.FEEDER) == (
            "The document feeder is empty or jammed."
        )
```

Retarget this class at `error_advice` and assert `.next_step` is non-empty for every member
(D-11's seven next steps). `test_checks.py` uses the same shape over `CheckKey`/`CheckState`.

#### `tests/test_web_checks.py`

**Analog:** `tests/test_web_state_rendering.py:1-90`. Copy the module docstring convention
(it names the requirements covered and explains why the file exists), the
`StubScannerBackend` subclass, and the compiled-regex-over-whole-markup assertion style:

```python
"""
Per-state rendering contract for the web templates.

Covers requirements: UI-03, UI-07, CTR-01, ROBU-01, ROBU-04, ROBU-08.
...
Every case is parametrised over ``list(JobState)`` rather than a hand-written
list of names, so an eighth member cannot be added without forcing a decision
here -- the same discipline ``tests/test_vocabulary.py`` applies to the lookup
functions themselves.
"""
```

```python
class _StubScanner(StubScannerBackend):
    """The shared stub backend, but reporting one device instead of none."""

    def get_devices(self) -> list[DeviceInfo]:
        return [DeviceInfo(name="test:device:001", vendor="Test", model="Stub",
                           device_type="virtual")]
```

Build the app with `create_app(settings, _StubScanner())` and drive it with
`fastapi.testclient.TestClient`. Two `TestClient` instances give two cookie jars for the
owner/non-owner unit test.

#### `tests/test_doctor.py`

**Analog:** `tests/test_cli.py` — `_patch_cli(monkeypatch)` returns `(CliRunner(), settings)`
and every test is `result = runner.invoke(cli, [...]); assert result.exit_code == N`:

```python
    def test_scan_command_help(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Scan subcommand --help shows --profile and --title options."""
        runner, _ = _patch_cli(monkeypatch)
        result = runner.invoke(cli, ["scan", "--help"])
        assert result.exit_code == 0
        assert "--profile" in result.output
```

#### `tests/test_app_lifespan.py` (extend)

**Analog:** `test_shutdown_closes_resources_after_the_worker_stops` (302–340) — the
recording-wrapper technique, and the assertion A-7 changes:

```python
    def recording_stop() -> bool:
        calls.append("worker.stop")
        return original_stop()
    ...
    monkeypatch.setattr(worker, "stop", recording_stop)
    monkeypatch.setattr(paperless, "close", recording_paperless_close)
    monkeypatch.setattr(store, "close", recording_store_close)
    caplog.set_level(logging.INFO, logger=_APP_LOGGER)
    with TestClient(app):
        pass
    assert calls == ["worker.stop", "paperless.close", "job_store.close"]
```

The new assertion is `["worker.stop", "refresher.stop", "paperless.close",
"job_store.close"]` (or both stops before either close) — a legitimate "could not have
passed before" test.

#### `tests/test_deployment_config.py` (extend)

**Analog:** `test_cli_reference_command_exit_codes_are_real` (412–447) — it iterates every
`## \`saneless <cmd>\`` heading and will hard-fail the moment the docs grow a `doctor`
section without an `**Exit codes:**` table. The expected-set block is what must gain a row:

```python
    assert _documented_codes(tables["scan"]) == {0, 1, 2, 3, 4, 5, 130}
    assert _documented_codes(tables["devices"]) == {0, 1, 2, 5, 130}
    assert _documented_codes(tables["jobs"]) == {0, 2, 5, 130}
    assert _documented_codes(tables["serve"]) == {0, 2, 3, 5, 130}
    assert _documented_codes(tables["auto-profiles"]) == {0, 1, 2, 5, 130}
```

Add `tables["doctor"]` with its own set, and mirror `jobs`' reasoning in the docstring
(doctor never touches SANE fatally — A-1 — so no 1).

#### `tests/test_browser.py` (extend)

**Analog:** the `context` fixture override (284–311). The gate closure is *inside* the
fixture today, which is exactly the trap for a hand-made second context — extract `_gate`
to a module-level factory and apply it to both contexts:

```python
@pytest.fixture
def context(
    context: BrowserContext, egress_allowlist: list[str]
) -> Iterator[BrowserContext]:
    blocked: list[str] = []

    def _gate(route: Route) -> None:
        url = route.request.url
        if _is_allowed(url, egress_allowlist):
            route.continue_()
        else:
            blocked.append(url)
            route.abort()

    context.route("**/*", _gate)
    yield context
    assert blocked == [], f"the page tried to reach the network: {blocked}"
```

---

## Shared Patterns

### Pattern A — `match` + `assert_never` over every closed set
**Source:** `src/saneless/vocabulary.py:440,476,525,559,588,661,701`
**Apply to:** `error_advice`, `check_state_label/class/glyph`, `check_name`,
`worst_state`, `rejection_message`, `rejection_status_code`, `_failure_line`.
Single assignment target per arm, `case _: assert_never(x)`, one trailing `return`, a
`Raises: AssertionError:` docstring section. This is how the type checkers are made to find
every consumer when a member is added. **Never two `match` statements over the same enum**
(Pitfall 7).

### Pattern B — user-facing strings are developer constants
**Source:** `vocabulary.py:227-236` (`RequestRejection` docstring), `vocabulary.py:531-534`
(`connection_status_message` docstring), `paperless.py:461` (`_without_userinfo`)
**Apply to:** every new string in this phase — check messages, next steps, the
scan-blocked reason, the queue-position line.

```python
    Every message is a developer-authored constant.  No status code, no
    response body, no URL and no token is interpolated, so a paperless-ngx
    error page cannot reach the UI through this path.
```

No exception text, no request input, no URL, no token, no filesystem path (D-13 extends
this to the log path; UI-SPEC S1 extends it to the fallback folder path).

### Pattern C — templates own no vocabulary
**Source:** `src/saneless/web/app.py:159-166`
**Apply to:** `partials/checks.html`, `status.html`, `history.html`, `tags.html`.
Every label, class name, glyph and formatted timestamp comes from a Jinja filter
implemented in Python. The one tolerated exception in-tree is the `{% if job.state ==
JobState.DONE %}` class chain in `history.html:7`, and the `JobState` global exists so even
that compares against real enum members rather than string literals.

### Pattern D — the stop-Event + bounded-join thread
**Source:** `src/saneless/worker.py:369-374, 405-408, 496-531`; joined at
`src/saneless/web/app.py:121-142`
**Apply to:** `CheckRefresher`.
`daemon=True`; an `Event` set once by `stop()`; `Event.wait(tick)` as the idle sleep;
`join(timeout=STOP_JOIN_SECONDS)`; `stop()` returns whether it actually stopped; the caller
closes shared resources only on a confirmed stop.

### Pattern E — NULL is not zero, and `is not none` is not truthiness
**Source:** D-32 / Pitfall 4; enforced in Jinja
**Apply to:** `page_counts` and every count render.
`{% if job.pages_scanned is not none %}`, never `{% if job.pages_scanned %}`. Four of six
terminal cases are NULL (`worker.py:1348-1352`, `job.py:782-786`), so this is the common
path.

### Pattern F — comments state the *reason*, and name the decision id
**Source:** everywhere; representative: `worker.py:50-56`, `cache.py:31-37`,
`index.html:7-12`, `app.css:36-42`, `scan_button.html:1-7`
**Apply to:** every new module, constant, CSS rule and template.
Each comment says why the thing is the way it is and cites the decision (`D-07`, `A-5`,
`C-10`, `ROBU-04`). A plan action that adds code without this is off-pattern for this
codebase.

### Pattern G — docstrings on every public module, class and function
**Source:** project-wide; ruff `D` rules enforce it (`CLAUDE.md`)
**Apply to:** all new code. Style is no-blank-line-before-class + multi-line-summary-second-
line (`D203`/`D212` ignored). `Args:` / `Returns:` / `Raises:` sections. Click command
docstrings double as `--help` text, so keep them to one line.

### Pattern H — no suppressions
**Source:** `CLAUDE.md`
**Apply to:** everything. No `# type: ignore`, no `# noqa`, no disabled rules.
Concretely: `PLR0913` on `create_job` (Pitfall 5) must be solved by bundling parameters,
and a positional boolean parameter must be keyword-only (`_auto_source_mode`'s `Args:` note
records the rule).

---

## No Analog Found

| File | Role | Data Flow | Reason |
|---|---|---|---|
| the bounded saned TCP pre-probe inside `src/saneless/checks.py` | utility (socket) | request-response | **No reachability probe exists anywhere in the tree.** `SaneBackend.get_devices()` (`sane_backend.py:2036-2078`) is the nearest thing and is an unbounded blocking C call. `socket.create_connection((host, port), timeout=...)` has no in-tree precedent; RESEARCH §2 supplies the sketch and flags assumption A1 (port 6566) as needing verification. Treat as new code with its own unit tests, and fall back to `get_devices()` rather than reporting `FAIL` if the pre-probe itself is wrong. |

Partial-analog notes (have a precedent but must deviate deliberately):

- `web/checks_cache.py` — `MetadataCache` is the shape, but the value type, the
  last-known-good retention (Pitfall 6) and the injectable clock all differ.
- `tests/test_checks_cache.py` — `tests/test_cache.py` is the *counter*-example: it sleeps,
  and the new file must not.
- `pipeline.py::pass_count_callback` — `thumbnail_callback` (`pipeline.py:355`) is an exact
  structural analog, but no existing callback carries a `(label, count)` payload, so the
  signature is new.

---

## Metadata

**Analog search scope:** `src/saneless/` (all modules), `src/saneless/web/` (app, routes,
cache, errors, cross_origin), `src/saneless/web/templates/` (index + all seven partials),
`src/saneless/web/static/app.css`, `tests/` (lifespan, cache, vocabulary, cli, browser,
web_state_rendering, deployment_config).
**Files scanned:** 24 source/template/CSS files, 8 test files.
**Pattern extraction date:** 2026-09-16
