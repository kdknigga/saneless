# Phase 23: Honest Outcomes and Never Lose a Scan - Pattern Map

**Mapped:** 2026-09-11
**Files analyzed:** 28 (10 source modified, 1 source-new function, 2 deploy, 9 test, 6 docs/spec)
**Analogs found:** 24 / 28 (4 have **no precedent in this repo** — see § "No Analog Found")

Every excerpt below is quoted verbatim from this repository with real line numbers. Where a
"closest analog" does not exist, the row says so instead of inventing one.

---

## File Classification

### Source — modified

| File | Role | Data Flow | Closest Analog | Match Quality |
|------|------|-----------|----------------|---------------|
| `src/saneless/vocabulary.py` | vocabulary / total-lookup leaf | transform (enum -> enum, enum -> str) | **itself**: `state_label` (`:102-136`), `error_message` (`:183-220`), and `PipelineEvent.job_state` (`pipeline.py:48-87`) | exact |
| `src/saneless/exceptions.py` | exception hierarchy | — | `FeederEmptyError(ScanError)` (`exceptions.py:30-31`) | exact |
| `src/saneless/job.py` (`finish_job`) | store / persistence | CRUD (single UPDATE) | `update_state` (`job.py:596-624`) for the SQL+logging shape; `create_job` (`:523-578`) for the `with self._conn:` + `_row_to_job` discipline | exact |
| `src/saneless/config.py` | config model + startup validation | config | `OutputConfig` (`:82-96`), `validate_settings_dirs`'s `tmp_dir` block (`:221-229`) | exact |
| `src/saneless/paperless.py` (`poll_task`) | HTTP client | request-response polling | `upload_document`'s retry/4xx-raise loop (`:150-208`) | role-match (the *shape* to copy is the raise-with-`msg`; the 4xx/5xx **split is explicitly rejected** by D-12) |
| `src/saneless/paperless.py` (`test_connection`) | HTTP client | request-response | `classify_error` (`vocabulary.py:223-248`) — the ordered-chain docstring reasoning | role-match |
| `src/saneless/paperless.py` (`.part` rename) | file I/O | file-I/O | **none** — see § "No Analog Found" | none |
| `src/saneless/pipeline.py` (preservation guard) | orchestration | file-I/O on error path | **partial**: `pdf.py:47`'s `TemporaryDirectory` + `pipeline.py:122-144` `_check_disk_space` for raise style; no prior "move a file out of a doomed temp dir" exists | partial |
| `src/saneless/pipeline.py` (`PipelineRequest.job_id`) | dataclass | — | `PipelineRequest` (`:93-104`) itself | exact |
| `src/saneless/pipeline.py` (`_handle_duplex_mismatch` poll) | orchestration | request-response | `run_pipeline:518-529` (the `task_uuid is not None` narrowing) | exact |
| `src/saneless/pdf.py` | assembly / file I/O | transform | `assemble_pdf` (`:29-63`) itself | exact |
| `src/saneless/worker.py` | worker thread | event-driven | `_process_job`'s existing `try/except` (`:233-260`) | exact |
| `src/saneless/cli.py` | CLI presenter | request-response | `jobs` command (`:220-265`) itself | exact |
| `src/saneless/web/app.py` | app factory | config | `:57-60` itself | exact |
| `src/saneless/web/routes.py` | HTTP route | request-response | `paperless_test` (`:119-136`) itself | exact |

### Source — new

| File | Role | Data Flow | Closest Analog | Match Quality |
|------|------|-----------|----------------|---------------|
| filename sanitiser (in `pdf.py`, per RESEARCH § Pattern 3) | utility | transform | `auto_profiles._slugify` (`:33-44`) — **quoted below as an ANTI-pattern per D-19**; no safe analog exists | **none** |

### Presentation

| File | Role | Data Flow | Closest Analog | Match Quality |
|------|------|-----------|----------------|---------------|
| `src/saneless/web/templates/partials/status.html` | template | server-rendered partial | its own `DONE` branch (`:12-14`) | exact |
| `src/saneless/web/templates/partials/history.html` | template | server-rendered partial | its own class expression (`:7-9`) | exact |
| `src/saneless/web/static/app.css` | stylesheet | — | `.status-done` / `.status-error` (`:11-18`) | exact |
| `src/saneless/web/static/app.js` | client script | event-driven | `htmx:afterSwap` handler (`:24-30`) | exact |

### Deploy

| File | Role | Data Flow | Closest Analog | Match Quality |
|------|------|-----------|----------------|---------------|
| `Dockerfile` | container image | config | **no `ENV` line exists today** — `EXPOSE`/`HEALTHCHECK` at `:19-21` are the only declarative directives | partial |
| `docker-compose.yml` | deployment | config | the `saneless-data:/tmp/saneless` line (`:20`) itself | exact |

### Tests (all Wave 0 items from `23-VALIDATION.md`)

| File | Role | Data Flow | Closest Analog (in the same file) | Match Quality |
|------|------|-----------|-----------------------------------|---------------|
| `tests/test_vocabulary.py` | unit | — | `TestStateLabel` (`:150-176`) / `TestProgressLabel` (`:178-205`) | exact |
| `tests/test_job.py` (`TestFinishJob`) | unit | CRUD | `TestResultColumns` (`:642+`) | exact |
| `tests/test_paperless.py` | unit | request-response | `TestPollTask` (`:251-322`), `TestConnectionTest` (`:330-400`), `_make_transport` (`:29-33`) | exact |
| `tests/test_pdf.py` | unit | transform | `TestAssemblePdf` (`:12-85`) | exact |
| `tests/test_config.py` | unit | config | `TestValidateSettingsDirs` (`:325-352`) | exact |
| `tests/test_pipeline.py` | integration | file-I/O | `test_run_pipeline_upload_error` (`:80-98`) + `test_run_pipeline_temp_cleanup_on_error` (`:125-146`) | exact |
| `tests/test_web_state_rendering.py` | unit | server-rendered | `test_history_cell_css_class` (`:160-167`) | exact |
| `tests/test_cli.py` | unit | CLI | `TestJobsCommand` (`:387+`) | exact |
| `tests/test_outcomes_e2e.py` (**new file**) | integration | event-driven | `tests/test_worker.py::test_worker_processes_job` (`:191-218`) for the worker-driving shape + `tests/test_paperless.py:29-33` for the transport seam | role-match (no existing test drives the **real** `run_pipeline` through a worker — all 49 `ScanWorker(...)` sites monkeypatch `saneless.worker.run_pipeline`) |
| `tests/conftest.py` (`wait_for_state`) | fixture/helper | — | **none** — 27 flat `time.sleep` calls in `test_worker.py` are what it replaces | none |

### Docs / spec

| File | Lines | Role |
|------|-------|------|
| `docs/explanation/consume-directory-fallback.md` | `:62`, `:66` | explanation |
| `docs/reference/web-api.md` | `:54-56` (**not** `:55-56` — the `connected` row is 54) | reference |
| `docs/reference/docker.md` | `:30` (volume table, "ephemeral OK"), `:106` (mount line), `:114` (volumes block) | reference |
| `docs/how-to/deploy-docker-compose.md` | `:53` (mount line), `:64` (volumes block) | how-to |
| `docs/reference/environment-variables.md` | `:44` (`SANELESS_OUTPUT__TMP_DIR` row — needs a `DATA_DIR` sibling) | reference |
| `docs/reference/configuration.md` | `:43` (`tmp_dir` table row), `:90` (TOML example) | reference |
| `.planning/UI-SPEC.md` | `:58-59`, `:176-177`, `:219-220`, `:242`, `:281`, `:286` | design contract |

**`README.md` carries no `/tmp/saneless` or `saneless.db` mention** [verified by grep across `docs/` + `README.md`: 8 hits in 5 files, all listed above]. The planner does not need to edit it for the path move.

---

## Pattern Assignments

### `src/saneless/vocabulary.py` — `JobState.FALLBACK`, `job_state_for`, `ConnectionStatus`

**Analog:** itself + `pipeline.py:48-87`.

**Enum + reserved-comment to replace** (`vocabulary.py:36-65`):

```python
class JobState(StrEnum):
    """States in the scan job lifecycle."""

    PENDING = "PENDING"
    SCANNING = "SCANNING"
    AWAITING_FLIP = "AWAITING_FLIP"
    ASSEMBLING = "ASSEMBLING"
    UPLOADING = "UPLOADING"
    DONE = "DONE"
    ERROR = "ERROR"
...
class ScanOutcome(StrEnum):
    """How a scan attempt resolved."""

    SUCCESS = "SUCCESS"
    FALLBACK = "FALLBACK"
    # FAILED belongs to the honest-outcomes work, which decides whether a
    # failure is a returned outcome or a raised exception. It is added together
    # with the code path that produces it, not ahead of it.
```

D-01 replaces lines 63-65 with the answer ("failures raise; `outcome` stays NULL"). The enum
values are identical to their names — pinned by `tests/test_vocabulary.py:51-54`
(`test_job_state_value_equals_name`), so `FALLBACK = "FALLBACK"`.

**Frozenset sets** (`vocabulary.py:83-88`) — `FALLBACK` joins this one, and `ACTIVE_STATES`
(`:68-76`) stays unchanged; `BUSY_STATES` (`:90-99`) is derived and needs no edit:

```python
TERMINAL_STATES: frozenset[JobState] = frozenset({JobState.DONE, JobState.ERROR})
"""Job states where the job has reached its final outcome.

Together with ``ACTIVE_STATES`` this partitions ``JobState``: every member is in
exactly one of the two sets.
"""
```

**Total-lookup pattern to copy for `state_label`/`progress_label` arms and for
`ConnectionStatus`'s message function** (`vocabulary.py:102-136`) — note the local `label`
variable and the single trailing `return`, which is how this module keeps ruff's `RET` quiet:

```python
def state_label(state: JobState) -> str:
    """
    Return the short human label for a job state.
    ...
    Raises:
        AssertionError: If the value is not a JobState member.

    """
    match state:
        case JobState.PENDING:
            label = "Pending"
        ...
        case JobState.DONE:
            label = "Complete"
        case JobState.ERROR:
            label = "Failed"
        case _:
            assert_never(state)
    return label
```

**Enum -> enum mapping pattern for D-02's `job_state_for(outcome)`** — copy
`PipelineEvent.job_state` (`pipeline.py:48-87`), including the docstring paragraph that explains
the odd arm:

```python
    @property
    def job_state(self) -> JobState | None:
        """
        Return the persisted job state this event implies, if any.

        ``None`` means this event changes no persisted state.  Today that is
        exactly ``SCANNING_REVERSE``: ...

        Returns:
            The matching JobState, or None when the event persists nothing.

        Raises:
            AssertionError: If the value is not a PipelineEvent member.

        """
        match self:
            case PipelineEvent.SCANNING:
                state = JobState.SCANNING
            ...
            case _:
                assert_never(self)
        return state
```

**Classification-chain docstring to mirror for `test_connection`'s status-code chain**
(`vocabulary.py:223-239`) — RESEARCH § Pattern 6 says `assert_never` applies to the *message*
lookup, not to the classification; this is the precedent that says why:

```python
def classify_error(exc: Exception) -> ErrorCategory:
    """
    Map an exception to its error category.

    The checks are ordered, not matched: ``FeederEmptyError`` subclasses
    ``ScanError``, so the narrower class has to be tested first.  This is an
    ``isinstance`` chain rather than a ``match`` because it dispatches on
    exception type instead of on an enum, so ``assert_never`` does not apply
    and the trailing ``UNKNOWN`` is the correct total fallback.
    ...
    """
```

**`__all__` is sorted and explicit** (`vocabulary.py:22-33`) — `ConnectionStatus` and
`job_state_for` must be added in sorted position (ruff `RUF022`).

---

### `src/saneless/exceptions.py` — `PaperlessTimeoutError`

**Analog:** `exceptions.py:26-35` (the whole file is 39 lines; this is the entire pattern):

```python
class ScanError(SanelessError):
    """Scanner operation failure."""


class FeederEmptyError(ScanError):
    """ADF feeder is empty -- no paper detected."""


class PaperlessError(SanelessError):
    """Paperless-ngx API operation failure."""
```

One-line docstring, no body, and the name added to `__all__` (`:8-15`) in sorted position.
`vocabulary.classify_error:246`'s `isinstance(exc, PaperlessError)` then covers the subclass with
no new arm (D-11).

---

### `src/saneless/job.py` — `finish_job()`

**Analog:** `update_state` (`job.py:596-624`) — the SQL + logging + `@_locked` shape:

```python
    @_locked
    def update_state(
        self,
        job_id: str,
        state: JobState,
        error: str | None = None,
        error_category: ErrorCategory | None = None,
    ) -> None:
        """
        Update the state (and optionally error) of a job.

        Args:
            job_id: The UUID string of the job.
            state: New job state.
            error: Optional error message (typically set with ERROR state).
            error_category: Optional error category for programmatic handling.

        """
        with self._conn:
            self._conn.execute(
                "UPDATE jobs SET state = ?, error = ?, error_category = ? WHERE id = ?",
                (
                    state.value,
                    error,
                    error_category.value if error_category else None,
                    job_id,
                ),
            )
        logger.debug("Job %s -> %s", job_id, state.value)
```

This is also **exactly the SQL D-04 forbids extending** — it is an unconditional `SET`, so adding
the six result columns here blanks them on every in-flight transition.

**Decorator contract** (`job.py:415-446`) — `finish_job` must carry `@_locked`, and the docstring
explains why the body opens its own `with self._conn:`:

```python
def _locked[**P, R](
    method: Callable[Concatenate[JobStore, P], R],
) -> Callable[Concatenate[JobStore, P], R]:
    """
    Serialise a JobStore method on the store's re-entrant lock.

    Takes ``self._lock`` and nothing else.  ...
    Each method that executes SQL therefore opens its own ``with self._conn:``
    in its body, where the transaction boundary is also more legible.
    """

    @functools.wraps(method)
    def wrapper(self: JobStore, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._lock:
            return method(self, *args, **kwargs)

    setattr(wrapper, _LOCKED_MARKER, True)
    return wrapper
```

**Enum-to-TEXT and nullable-read round trip** (`job.py:515`) — `finish_job` writes
`outcome.value if outcome else None`; the read side already exists:

```python
            outcome=ScanOutcome(row["outcome"]) if row["outcome"] else None,
```

**Two tests that police this method before it is written:**

`tests/test_job.py:800-823` — the reflective `@_locked` coverage test (Phase 22 D-12/D-24). Note
`PUBLIC_METHOD_FLOOR = 7` at `tests/test_job.py:58`; adding `finish_job` makes 8 and the floor
comment says it is a floor, not a roster, so **no edit is needed** — but a `finish_job` without
the decorator fails here:

```python
    def test_locked_coverage_spans_every_public_method(self) -> None:
        """Every public JobStore method carries the lock marker (STOR-01)."""
        public = [
            (name, member)
            for name, member in inspect.getmembers(JobStore, inspect.isfunction)
            if not name.startswith("_")
        ]
        assert len(public) >= PUBLIC_METHOD_FLOOR, (...)
        # No opt-out list -- not for close(), not for anything. ...
        unlocked = sorted(
            name
            for name, member in public
            if getattr(member, job_module._LOCKED_MARKER, False) is not True
        )
        assert unlocked == [], (...)
```

`tests/test_job.py:825-841` — the AST test that forbids `finish_job` from calling `update_state`:

```python
    def test_no_public_method_calls_another_public_method(self) -> None:
        """No public JobStore method calls another public method (STOR-01)."""
        # A correctness rule, not style: sqlite3 connection context managers do
        # not nest, so an inner `with conn:` commits the OUTER transaction and
        # half the caller's work lands early.  RLock re-entrancy prevents the
        # deadlock; nothing prevents the incorrectness, and ruff cannot see it.
        store = _job_store_classdef()
        offenders = [
            f"{method.name} -> {callee}"
            for method in store.body
            if isinstance(method, ast.FunctionDef) and not method.name.startswith("_")
            for callee in _public_self_calls(method)
        ]
        assert offenders == [], (...)
```

**`create_job`'s comment at `job.py:561-562` becomes false** the moment `finish_job` exists:

```python
                    # The six result columns are written by nothing in this
                    # phase.
                    None,  # outcome
```

Same for `tests/test_job.py:655-656` (`test_created_job_result_columns_stay_none` — "Nothing in
this phase writes the six result columns"). That test stays *true* (it only checks a freshly
created job), but its docstring is a claim this phase falsifies at the module level.

---

### `src/saneless/config.py` — `data_dir`, `db_path`, `failed_dir`, writability check

**Analog:** `OutputConfig` (`:82-96`) — note `log_file:86` is the hardcoded-`Path.home()` style
D-14 requires for `data_dir`, and that every field here is a `str`, not a `Path`:

```python
class OutputConfig(BaseModel):
    """Output and logging configuration."""

    tmp_dir: str = str(Path(tempfile.gettempdir()) / "saneless")
    log_file: str = str(Path.home() / ".local" / "state" / "saneless" / "saneless.log")
    log_level: str = "INFO"
    ...
    paperless_task_timeout: int = 300
```

There is **no existing `@property` on any config model** — `OutputConfig`, `ScannerConfig`,
`PaperlessConfig`, `ProfileConfig`, and `Settings` are all plain field bags. D-16's computed
properties are the first. The closest property precedent in the codebase is `Job.is_active` /
`Job.is_busy` (`job.py:404-412`), which is a dataclass, not a pydantic model:

```python
    @property
    def is_active(self) -> bool:
        """Whether this job is still in flight (not DONE or ERROR)."""
        return self.state in ACTIVE_STATES
```

**Writability block to copy for `data_dir`** (`config.py:221-229`) — the exists/not-exists pair,
and the `msg =` before `raise` that ruff `EM` requires:

```python
    tmp = Path(settings.output.tmp_dir)
    if tmp.exists() and not os.access(tmp, os.W_OK):
        msg = f"tmp_dir is not writable: {tmp}"
        raise ConfigError(msg)
    if not tmp.exists():
        parent = tmp.parent
        if parent.exists() and not os.access(parent, os.W_OK):
            msg = f"tmp_dir parent is not writable: {parent}"
            raise ConfigError(msg)
```

The docstring at `:207-208` says "Fail fast with ConfigError if tmp_dir or consume_dir are not
writable" — a sentence this phase falsifies and must rewrite.

---

### `src/saneless/paperless.py` — `poll_task` rewrite

**Analog (shape, not policy):** `upload_document`'s error handling (`paperless.py:179-194`). Copy
the `msg = f"..."` / `raise PaperlessError(msg) from exc` form; **do not** copy the 4xx/5xx split
— D-12 rejects it explicitly:

```python
            except httpx.HTTPStatusError as exc:
                if 400 <= exc.response.status_code < 500:
                    msg = (
                        f"Paperless rejected upload: "
                        f"{exc.response.status_code} {exc.response.text}"
                    )
                    raise PaperlessError(msg) from exc
                last_error = exc
                logger.warning(
                    "Upload attempt %d/%d failed: %s",
                    attempt + 1,
                    self._max_retries,
                    exc,
                )
                if attempt < self._max_retries - 1:
                    time.sleep(2**attempt)
```

Also `paperless.py:160-164`, the precedent for "a shape the server can return that we must reject
rather than pass along" — the same instinct `_extract_task` needs:

```python
                if task_id is None:
                    # A JSON null body would otherwise become the string
                    # "None" -- truthy, not None, and polled as a real task id.
                    msg = "Paperless accepted the upload but returned no task ID"
                    raise PaperlessError(msg)
```

**The code being replaced** (`paperless.py:210-248`) — the whole current contract, including the
sleep-only `elapsed` (OUTC-07) and the `{"status": "TIMEOUT"}` sentinel (OUTC-01):

```python
    def poll_task(self, task_id: str, timeout: int | float = 300) -> dict[str, object]:
        """
        Poll task endpoint until terminal state with exponential backoff.

        Handles the race condition where a task may not appear
        immediately after upload (Pitfall #8).
        ...
        Returns:
            Task dict with status field. Status is one of
            SUCCESS, FAILURE, or TIMEOUT.

        """
        delay = 0.5
        elapsed = 0.0

        while elapsed < timeout:
            response = self._client.get(
                "/api/tasks/",
                params={"task_id": task_id},
            )
            if response.status_code == 200:
                tasks = response.json()
                if isinstance(tasks, list) and tasks:
                    task = tasks[0]
                    status = task.get("status")
                    if status in ("SUCCESS", "FAILURE"):
                        logger.info("Task %s completed: %s", task_id, status)
                        return dict(task)

            time.sleep(delay)
            elapsed += delay
            delay = min(delay * 2, 30.0)

        logger.warning("Task %s timed out after %s seconds", task_id, timeout)
        return {"status": "TIMEOUT", "task_id": task_id}
```

**Header dict for the OUTC-11 `Accept` version pin** (`paperless.py:94-101`) — one key joins the
existing `Authorization`:

```python
        client_kwargs: dict = {
            "base_url": url.rstrip("/"),
            "headers": {"Authorization": f"Token {token}"},
            "timeout": 30.0,
        }
        if _transport is not None:
            client_kwargs["transport"] = _transport
        self._client = httpx.Client(**client_kwargs)
        self._consume_dir = consume_dir
        self._max_retries = max_retries
```

**Existing both-shapes-tolerant parse in this same module** (`paperless.py:283-284`, and the
identical line at `:299-300`) — the project already handles the bare-list-vs-paginated split for
tags and correspondents, so `_extract_task` is not a new idea here:

```python
        data = response.json()
        return data.get("results", []) if isinstance(data, dict) else data
```

---

### `src/saneless/paperless.py` — `test_connection` -> `ConnectionStatus`

**Code being replaced** (`paperless.py:250-268`):

```python
    def test_connection(self) -> str:
        """
        Test paperless-ngx connection.

        Distinguishes three states: connected (API reachable and
        authenticated), token_rejected (API reachable but auth
        failed), and unreachable (network error).

        Returns:
            One of "connected", "token_rejected", or "unreachable".

        """
        try:
            response = self._client.get("/api/tags/", params={"page_size": 1})
            if response.status_code in (401, 403):
                return "token_rejected"
            return "connected"
        except httpx.ConnectError:
            return "unreachable"
```

**Wire-compat consumer** (`web/routes.py:119-136`) — the `dict[str, str]` annotation is satisfied
by a `StrEnum` with no route edit:

```python
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
            content={"status": "error", "detail": type(exc).__name__},
        )
```

That docstring names the three states and is a sentence this phase must extend to five.

---

### `src/saneless/paperless.py` — consume-directory `.part` + atomic rename

**Code being replaced** (`paperless.py:196-205`):

```python
        # All retries exhausted
        if self._consume_dir:
            dest_dir = Path(self._consume_dir)
            if not dest_dir.exists():
                dest_dir.mkdir(parents=True, exist_ok=True)
                logger.warning("Created consume directory %s", dest_dir)
            dest = dest_dir / pdf_path.name
            shutil.copy2(pdf_path, dest)
            logger.warning("All retries exhausted. Copied PDF to %s", dest)
            return UploadResult(delivered_to_api=False, consume_dir_path=dest)
```

**Analog: none.** There is no `os.replace`, no `.part`/`.tmp` staging file, and no `fsync`
anywhere in `src/`. This is new ground — follow RESEARCH § "Pattern 4" rather than a codebase
precedent. `shutil` is already imported at `paperless.py:13`; `os` is **not** (it is imported in
`config.py:11`), so `os.replace` needs a new import in this module.

---

### `src/saneless/pipeline.py` — preservation guard, `job_id`, duplex poll

**Where the guard goes** (`pipeline.py:502-536`) — the `try` must open before `:510` and the
`except` close after `:526`; `pdf_path` at `:504` lives inside the `TemporaryDirectory` opened at
`:448`:

```python
        # Step 3: Assemble PDF
        notify(PipelineEvent.ASSEMBLING)
        pdf_path = assemble_pdf(filtered, tmp_path)
        logger.info("PDF assembled: %s", pdf_path)

        # Step 4: Upload
        notify(PipelineEvent.UPLOADING)
        created = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        upload_result = paperless.upload_document(
            pdf_path,
            request.title,
            request.tags,
            request.correspondent,
            created,
        )

        # Step 5: Poll for result.  UploadResult.__post_init__ guarantees a
        # task_uuid iff the document reached the API, so this test is exactly
        # `delivered_to_api` and additionally narrows the id to str.
        task_uuid = upload_result.task_uuid
        if task_uuid is not None:
            paperless.poll_task(
                task_uuid,
                timeout=settings.output.paperless_task_timeout,
            )
            outcome = ScanOutcome.SUCCESS
        else:
            outcome = ScanOutcome.FALLBACK
```

Note `paperless.poll_task(...)`'s return value is discarded at `:523` — OUTC-01 requires it be
consumed (or, once it raises, requires nothing more than that the raise propagates).

**`PipelineRequest` gains `job_id`** (`pipeline.py:93-104`) — a plain `@dataclass`, non-default
fields first:

```python
@dataclass
class PipelineRequest:
    """Parameters for a scan pipeline run."""

    profile_name: str
    title: str
    tags: list[int] | None = None
    correspondent: int | None = None
    status_callback: Callable[[PipelineEvent], None] | None = None
    thumbnail_callback: Callable[[str], None] | None = None
    flip_event: threading.Event | None = None
    abort_event: threading.Event | None = None
```

**`ScanResult`** (`pipeline.py:107-115`), unchanged but read by `finish_job`:

```python
@dataclass
class ScanResult:
    """How a scan pipeline run resolved, and how many pages it moved."""

    outcome: ScanOutcome
    pages_scanned: int
    pages_removed: int
    pages_uploaded: int
    warning: str | None = None
```

**`_handle_duplex_mismatch`'s two unguarded assemblies and two unpolled uploads**
(`pipeline.py:215-242`) — D-08 brings both PDFs inside the guard and polls both results:

```python
    fronts, backs = passes
    notify(PipelineEvent.ASSEMBLING)
    fronts_pdf = assemble_pdf(fronts, tmp_path / "fronts")
    backs_pdf = assemble_pdf(backs, tmp_path / "backs")
    ...
    notify(PipelineEvent.UPLOADING)
    created = datetime.now(tz=UTC).strftime("%Y-%m-%d")
    title = request.title
    fronts_result = paperless.upload_document(
        fronts_pdf,
        f"{title} (fronts)",
        request.tags,
        request.correspondent,
        created,
    )
    backs_result = paperless.upload_document(
        backs_pdf,
        f"{title} (backs)",
        request.tags,
        request.correspondent,
        created,
    )
    delivered = fronts_result.delivered_to_api and backs_result.delivered_to_api
```

The two `assemble_pdf` calls pass a *directory* (`tmp_path / "fronts"`), which D-09's filename
argument must accommodate. `shutil` is already imported in `pipeline.py:11` (for
`shutil.disk_usage` at `:137`), so `shutil.move` needs no new import.

**Raise style in this module** (`pipeline.py:140-144`) — multi-line `msg` then bare `raise`:

```python
        msg = (
            f"Insufficient disk space: {free_mb} MB free in {path}, "
            f"{min_free_mb} MB required (configure min_free_space_mb to adjust)"
        )
        raise ScanError(msg)
```

---

### `src/saneless/pdf.py` — filename argument + fixed-DPI layout

**Analog:** `assemble_pdf` (`pdf.py:29-63`) — the whole function, since both changes land inside
it. Line 55 is the hardcoded name; line 56 is the layout-function gap:

```python
def assemble_pdf(images: list[Image.Image], output_dir: Path) -> Path:
    """
    Assemble PIL Images into a single PDF using img2pdf.
    ...
    Args:
        images: List of PIL Image objects to include in the PDF.
        output_dir: Directory where the output PDF will be written.

    Returns:
        Path to the generated PDF file.

    """
    output_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=str(output_dir)) as tmp_dir:
        image_paths: list[str] = []
        for i, img in enumerate(images):
            img_path = Path(tmp_dir) / f"page_{i:04d}.png"
            img.save(str(img_path), format="PNG")
            image_paths.append(str(img_path))
            logger.debug("Saved page %d to %s", i, img_path)

        pdf_path = output_dir / "output.pdf"
        pdf_bytes = img2pdf.convert(image_paths)
        if pdf_bytes is None:
            msg = "img2pdf.convert returned None"
            raise RuntimeError(msg)
        pdf_path.write_bytes(pdf_bytes)
        logger.info("Assembled %d page(s) into %s", len(images), pdf_path)

    return pdf_path
```

Note `pdf.py:47`'s nested `TemporaryDirectory` — the reason D-07 says the page PNGs are already
gone before upload begins.

`assemble_pdf` has exactly **3 production call sites**: `pipeline.py:217`, `:218`, `:504`. The
two duplex ones pass a per-side subdirectory; the simplex one passes `tmp_path`.

---

### `src/saneless/pdf.py` — the filename sanitiser (NEW, no analog)

**ANTI-pattern — this is what D-19 forbids reusing** (`auto_profiles.py:33-44`), quoted so the
planner can see exactly why:

```python
def _slugify(lower: str) -> str:
    """
    Slugify an already-lowercased source name.

    Args:
        lower: Lowercased SANE source name.

    Returns:
        The name with spaces and underscores replaced by hyphens.

    """
    return lower.replace(" ", "-").replace("_", "-")
```

Two `.replace` calls. `/`, `..`, NUL, and every control character pass straight through. Its
input is a SANE source name from the device, which is why it was adequate; D-09's input is a
user-supplied job title bound for `consume_dir / name` and `failed_dir / name`.

**There is no safe filename sanitiser anywhere in `src/`.** The only other name-shaping code is
`cli.py:_truncate` (display-only, used at `:262`) and `auto_profiles.source_to_slug`, which calls
`_slugify`. This function has no precedent to copy — follow RESEARCH § "Pattern 3" (allow-list
`[A-Za-z0-9]`, 60-char cap, empty-after-sanitisation drops the segment) and
§ "Security Domain"'s threat table.

**Timestamp style already standardised** (`pipeline.py:226` and `:509`, identical):

```python
    created = datetime.now(tz=UTC).strftime("%Y-%m-%d")
```

ruff's `DTZ` is enabled (`pyproject.toml` `select` includes `DTZ`), so a naive `datetime.now()`
fails lint. `job.py:560` uses the same `datetime.now(tz=UTC).isoformat()`.

---

### `src/saneless/worker.py` — consume the `ScanResult`

**Analog / code being replaced** (`worker.py:233-260`):

```python
        try:
            request = PipelineRequest(
                profile_name=job.profile,
                title=job.title,
                tags=job.tags or None,
                correspondent=job.correspondent,
                status_callback=_status_cb,
                thumbnail_callback=_thumbnail_cb,
                flip_event=self._flip_event,
                abort_event=self._abort_event,
            )
            run_pipeline(
                self._scanner,
                self._paperless,
                self._settings,
                request,
            )
            self._job_store.update_state(job.id, JobState.DONE)
            self._transition_event.set()
        except Exception as exc:
            category = classify_error(exc)
            self._job_store.update_state(
                job.id,
                JobState.ERROR,
                error=str(exc),
                error_category=category,
            )
            logger.error("Job %s failed (%s): %s", job.id, category.value.lower(), exc)
```

The `run_pipeline(...)` call at `:244-249` throws the `ScanResult` away — C-03 in one expression.
`PipelineRequest(...)` at `:234-243` is where `job_id=job.id` joins.

**The invariant the worker already documents and must keep** (`worker.py:212-218`) — the status
callback deliberately refuses to write terminal states, which is why `finish_job` belongs in
`_process_job` and not in `_status_cb`:

```python
            if state not in ACTIVE_STATES:
                # DONE is terminal.  The worker writes it, and signals the
                # transition, only once run_pipeline has returned and its
                # temporary directory is gone -- never from in here.
                return
```

`FALLBACK` joining `TERMINAL_STATES` means this guard covers it for free.

---

### `src/saneless/cli.py` and `src/saneless/web/app.py` — db path

**Duplicated expression, site 1** (`cli.py:226-228`) — note there is **no `mkdir`** here, which
RESEARCH Pitfall 3 flags as a latent bug the `data_dir` move makes reachable:

```python
    settings = ctx.obj["settings"]
    db_path = str(Path(settings.output.tmp_dir) / "saneless.db")
    store = JobStore(db_path=db_path)
```

**Site 2** (`web/app.py:57-60`) — this one does mkdir; copy its ordering:

```python
    Path(settings.output.tmp_dir).mkdir(parents=True, exist_ok=True)
    job_store = JobStore(
        db_path=str(Path(settings.output.tmp_dir) / "saneless.db"),
    )
```

**CLI `FALLBACK` rendering sites** (`cli.py:238-239` JSON, `:258-263` table) — `:262` prints the
raw enum value, not `state_label`:

```python
                            "state": j.state.value,
...
                click.echo(
                    f"{j.created_at.strftime('%Y-%m-%d %H:%M:%S'):<{ts_w}} "
                    f"{_truncate(j.profile, profile_w):<{profile_w}} "
                    f"{_truncate(j.title, title_w):<{title_w}} "
                    f"{j.state.value}"
                )
```

---

### Web presentation — `FALLBACK` rendering (OUTC-02 / D-05 / D-20)

**Status partial** (`status.html:12-18`) — the `DONE` branch is the template to copy, *including*
the hidden history-reload `<div>` that UI-SPEC `:281` documents. D-20 says a FALLBACK branch
without that div leaves the history table stale:

```jinja
    {% elif job.state == JobState.DONE %}
      <p class="status-done">&#10003; Done: {{ job.title }}</p>
      <div hx-get="/api/jobs/history" hx-target="#history-body" hx-swap="outerHTML" hx-trigger="load" class="htmx-hidden"></div>
    {% elif job.state == JobState.ERROR %}
      <p role="alert" class="status-error">&#10007; Error: {{ job.error }}</p>
      <div hx-get="/api/jobs/history" hx-target="#history-body" hx-swap="outerHTML" hx-trigger="load" class="htmx-hidden"></div>
    {% endif %}
```

Glyphs are HTML entities, not literal characters: `&#10003;` = U+2713, `&#10007;` = U+2717
(UI-SPEC `:219-220`, `:248` — glyphs, not emoji). A FALLBACK glyph must follow that form.

**History partial** (`history.html:7-9`) — the class expression gains a third arm; the label comes
free from the filter:

```jinja
  <td class="{% if job.state == JobState.DONE %}status-done{% elif job.state == JobState.ERROR %}status-error{% endif %}">
    {{ job.state | state_label }}
  </td>
```

**Stylesheet** (`app.css:11-18`) — D-05 requires a token distinct from both of these:

```css
/* Status indicators */
.status-done {
    color: var(--pico-ins-color, green);
}

.status-error {
    color: var(--pico-del-color, red);
}
```

**The latent JS bug UI-SPEC `:286` names** (`app.js:24-30`) — a new `.status-fallback` class that
is not added to line 27 leaves `#scan-btn` permanently disabled after a fallback job:

```javascript
    document.addEventListener("htmx:afterSwap", function (evt) {
        var target = evt.detail.target;
        if (!target || target.id !== "status-area") return;
        if (target.querySelector(".status-done") || target.querySelector(".status-error")) {
            resetScanButton();
        }
    });
```

**Filter/global registration** (`web/app.py:92-101`) — `state_label` and `JobState` are already
exposed; no new template global is needed for FALLBACK:

```python
    app.state.templates.env.filters["state_label"] = state_label
    app.state.templates.env.filters["progress_label"] = progress_label
    ...
    job_state_global: Any = JobState
    app.state.templates.env.globals["JobState"] = job_state_global
```

---

### Deploy — `Dockerfile`, `docker-compose.yml`

**`Dockerfile` has no `ENV` line at all.** The runtime stage's declarative directives are
`:19-22`, and the new `ENV SANELESS_OUTPUT__DATA_DIR=/var/lib/saneless` goes alongside them:

```dockerfile
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1
ENTRYPOINT ["saneless"]
CMD ["serve"]
```

**`docker-compose.yml:11-20`** — line 20 is the mount D-15 repoints. Line 12's image name is
**Phase 31's, not this phase's**:

```yaml
  saneless:
    image: ghcr.io/kris-knigga/saneless:latest
    ports:
      - "8080:8080"
    volumes:
      # WARNING: config.toml must exist on the host before first run.
      # Create it with at minimum: [profiles.default] and [paperless] sections.
      # See: https://github.com/kris-knigga/saneless#configuration
      - ./config.toml:/etc/saneless/config.toml:ro
      - saneless-data:/tmp/saneless
```

That same mount line is duplicated verbatim at `docs/reference/docker.md:106` and
`docs/how-to/deploy-docker-compose.md:53`, and contradicted by `docs/reference/docker.md:30`:

```markdown
| `/tmp/saneless` | Scan temp files and SQLite database | No (ephemeral OK) |
```

---

## Test Patterns

### `tests/conftest.py` — existing fixtures (no new ones required except `wait_for_state`)

`default_settings` (`:82-97`) — note `tmp_dir` points at a shared
`/tmp/saneless-test`, so a `data_dir` field needs the same treatment or per-test `tmp_path`
override:

```python
@pytest.fixture
def default_settings() -> Settings:
    """Return a Settings instance with test-safe defaults."""
    auth = "test-token"
    return Settings(
        scanner=ScannerConfig(device="test:device:001"),
        paperless=PaperlessConfig(
            url="http://localhost:8000",
            token=auth,
        ),
        output=OutputConfig(
            tmp_dir=_TEST_TMP,
            log_file=_TEST_LOG,
        ),
        profiles={"default": ProfileConfig()},
    )
```

`mock_scanner` (`:100-108`) — the exact stub shape RESEARCH says OUTC-10's duplex case must
extend (two `scan_pages` calls returning different counts):

```python
@pytest.fixture
def mock_scanner() -> MagicMock:
    """Return a mock ScannerBackend that yields a single image with content."""
    scanner = MagicMock(spec=ScannerBackend)
    img = Image.new("RGB", (100, 100), "white")
    draw = ImageDraw.Draw(img)
    draw.rectangle([10, 10, 90, 90], fill="black")
    scanner.scan_pages.return_value = iter([img])
    return scanner
```

`mock_paperless` (`:133-140`) — **this fixture encodes the old `poll_task` contract** and breaks
when polling raises rather than returning:

```python
@pytest.fixture
def mock_paperless() -> MagicMock:
    """Return a mock PaperlessClient that succeeds."""
    paperless = MagicMock()
    paperless.upload_document.return_value = UploadResult(
        delivered_to_api=True, task_uuid="mock-task-uuid"
    )
    paperless.poll_task.return_value = {"status": "SUCCESS"}
    return paperless
```

`sample_pil_images` (`:64-71`) and `multi_page_images` (`:111-115`) are unchanged and available.

### `tests/test_vocabulary.py` — the four hand-written lists

Count guard (`:40-50`):

```python
    def test_job_state_has_exactly_seven_members(self) -> None:
        """
        JobState declares exactly seven lifecycle members (CTR-01).

        A count guard, not a name list: adding a member should fail the
        parametrised completeness tests below -- which force a label and a
        classification decision -- rather than a hand-written roster that only
        records what the enum happened to contain when it was written.
        """
        assert len(list(JobState)) == 7
```

Terminal-set membership (`:131-133`):

```python
    def test_terminal_states_membership(self) -> None:
        """TERMINAL_STATES is exactly DONE and ERROR (CTR-01)."""
        assert frozenset({JobState.DONE, JobState.ERROR}) == TERMINAL_STATES
```

Label table + the self-updating parametrisation (`:150-176`) — the second test needs no edit; the
first does:

```python
class TestStateLabel:
    """state_label short-label lookup tests."""

    @pytest.mark.parametrize(
        ("state", "expected"),
        [
            (JobState.PENDING, "Pending"),
            ...
            (JobState.DONE, "Complete"),
            (JobState.ERROR, "Failed"),
        ],
    )
    def test_state_label_strings(self, state: JobState, expected: str) -> None:
        """state_label returns the label the history table has always shown (CTR-01)."""
        assert state_label(state) == expected

    @pytest.mark.parametrize("state", list(JobState))
    def test_state_label_is_complete(self, state: JobState) -> None:
        """Every JobState has a label that is not just its raw value (CTR-01)."""
        label = state_label(state)
        assert label
        assert label != state.value
```

Totality spot check for terminal states (`:203-205`) — add a FALLBACK line:

```python
    def test_terminal_states_have_progress_prose_for_totality(self) -> None:
        """DONE and ERROR carry prose purely so the lookup stays total (CTR-01)."""
        assert progress_label(JobState.DONE) == "Complete"
        assert progress_label(JobState.ERROR) == "Failed"
```

`ConnectionStatus` gets a new `@pytest.mark.parametrize("status", list(ConnectionStatus))`
completeness class modelled on `TestErrorMessage` (`:207-215`).

### `tests/test_paperless.py` — the transport seam and the three cases to rewrite

Transport helper + PDF fixture (`:21-33`):

```python
@pytest.fixture
def sample_pdf(tmp_path: Path) -> Path:
    """Create a minimal PDF file for upload tests."""
    pdf_path = tmp_path / "test.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake content")
    return pdf_path


def _make_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.MockTransport:
    """Create an httpx.MockTransport from a handler function."""
    return httpx.MockTransport(handler)
```

Poll case to rewrite from return-value to raise (`:272-287`) — the v9 shape is hardcoded here and
the OUTC-11 cases parametrise over both shapes:

```python
    def test_poll_task_failure(self) -> None:
        """Polling returns FAILURE when task fails."""

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, json=[{"status": "FAILURE", "task_id": "t1", "result": "error"}]
            )

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            _transport=transport,
        )
        result = client.poll_task("t1", timeout=10)
        assert result["status"] == "FAILURE"
        client.close()
```

Empty-list tolerance, which must **keep passing** (`:305-322`) — the counter-dict idiom is the
house pattern for "assert we polled more than once":

```python
    def test_poll_task_not_found_first_retry(self) -> None:
        """Pitfall #8: task not found on first poll, succeeds on second."""
        call_count = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return httpx.Response(200, json=[])
            return httpx.Response(200, json=[{"status": "SUCCESS", "task_id": "t1"}])
        ...
        result = client.poll_task("t1", timeout=30)
        assert result["status"] == "SUCCESS"
        assert call_count["n"] >= 2
```

Connection case to parametrise (`:332-346`) — five near-identical methods become one
parametrisation over `list(ConnectionStatus)`:

```python
    def test_test_connection_connected(self) -> None:
        """Connection test returns 'connected' on 200 from /api/tags/."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert "/api/tags/" in str(request.url)
            assert "page_size=1" in str(request.url)
            return httpx.Response(200, json={"count": 0, "results": []})

        transport = _make_transport(handler)
        client = PaperlessClient(
            url="http://paperless:8000",
            token=_MOCK_AUTH,
            _transport=transport,
        )
        assert client.test_connection() == "connected"
        client.close()
```

The `== "connected"` comparison keeps working against a `StrEnum`, which is exactly the
wire-compat property D-13 requires — leave these assertions string-shaped so they *prove* it.

Consume-dir case to extend with a "no `.part` remains" assertion (`:213-242`, tail):

```python
        result = client.upload_document(sample_pdf, title="Fallback")
        assert result.delivered_to_api is False
        assert result.task_uuid is None
        # PDF should have been copied to consume dir
        copied = list(consume_dir.iterdir())
        assert len(copied) == 1
        assert copied[0].name == "test.pdf"
        # The result names the exact file the PDF was copied to -- something
        # the old magic-string sentinel could not carry.
        assert result.consume_dir_path == copied[0]
        client.close()
```

`len(copied) == 1` already fails if a `.part` survives — extend it to name the extension so the
failure message is legible.

### `tests/test_pdf.py` — MediaBox and naming classes

Existing class (`:12-24`) — every method takes `tmp_path` and calls `assemble_pdf([img], tmp_path)`
positionally, so **every one of the 6 existing tests breaks when a required `filename` argument is
added**. That is Wave 0 work, not incidental:

```python
class TestAssemblePdf:
    """PDF assembly tests."""

    def test_assemble_single_page(self, tmp_path: Path) -> None:
        """Single image produces a valid PDF file."""
        img = Image.new("RGB", (100, 100), "white")
        pdf_path = assemble_pdf([img], tmp_path)

        assert pdf_path.exists()
        assert pdf_path.stat().st_size > 0
        content = pdf_path.read_bytes()
        assert content[:5] == b"%PDF-"
```

`tests/test_pdf.py:78-85` is the one that asserts the output path shape and is the closest analog
for the OUTC-05 naming test:

```python
    def test_output_path(self, tmp_path: Path) -> None:
        """Output PDF is written to the specified directory with .pdf extension."""
        img = Image.new("RGB", (100, 100), "white")
        pdf_path = assemble_pdf([img], tmp_path)

        assert isinstance(pdf_path, Path)
        assert pdf_path.parent == tmp_path
        assert pdf_path.suffix == ".pdf"
```

No PDF-reading test exists anywhere — `content[:5] == b"%PDF-"` is the deepest inspection today.
`pikepdf` has no current importer in `tests/`; the OUTC-06 test is the first
(hence `uv add --dev pikepdf`).

### `tests/test_job.py` — `TestFinishJob`

Closest analog `TestResultColumns` (`:642-668`) — note how it reaches through `store._conn` to
*simulate* the write that `finish_job` will now perform for real, and how it asserts value **and**
Python type:

```python
class TestResultColumns:
    """The six result columns, the one column list, and the one row mapping."""

    def test_job_result_columns_default_to_none(self) -> None:
        """A bare Job exposes all six result columns, every one None (STOR-03)."""
        job = Job(id="test", profile="default", title="Test")
        assert job.outcome is None
        ...

    def test_created_job_result_columns_stay_none(self) -> None:
        """Nothing in this phase writes the six result columns (STOR-03)."""
        store = JobStore()
        try:
            created = store.create_job("default", "Unwritten")
            fetched = store.get_job(created.id)
            ...
        finally:
            store.close()
```

And the type-assertion discipline (`:695-699`) that `TestFinishJob` must copy — `sqlite3.Row`
is typed `Any`, so these assertions are the only control:

```python
            # The value AND the Python type: sqlite3.Row.__getitem__ is typed
            # Any, so neither ty nor pyrefly can catch a column that comes back
            # as the wrong type.  These assertions are the only control.
            assert fetched.outcome is ScanOutcome.SUCCESS
            assert isinstance(fetched.outcome, ScanOutcome)
```

`store = JobStore()` / `try: ... finally: store.close()` is the universal shape in this file —
in-memory by default (`job.py:459`).

### `tests/test_config.py` — `data_dir` cases

Analog `TestValidateSettingsDirs` (`:325-352`) — copy both halves, including the `chmod` restore:

```python
class TestValidateSettingsDirs:
    """Writability validation via validate_settings_dirs."""

    def test_validate_writable_tmp_dir_passes(self, tmp_path: Path) -> None:
        """No error when tmp_dir is writable."""
        settings = Settings(
            output=OutputConfig(tmp_dir=str(tmp_path)),
            profiles={"default": ProfileConfig()},
        )
        # Should not raise
        validate_settings_dirs(settings)

    def test_validate_unwritable_tmp_dir_fails_with_config_error(
        self, tmp_path: Path
    ) -> None:
        """Unwritable tmp_dir raises ConfigError with 'not writable' message."""
        unwritable = tmp_path / "readonly"
        unwritable.mkdir()
        unwritable.chmod(0o444)
        settings = Settings(
            output=OutputConfig(tmp_dir=str(unwritable)),
            profiles={"default": ProfileConfig()},
        )
        with pytest.raises(ConfigError, match="not writable"):
            validate_settings_dirs(settings)
        # Restore permissions for cleanup
        unwritable.chmod(0o755)
```

Defaults assertion (`:157-167`) is where a `data_dir` default check belongs:

```python
    def test_settings_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default settings provide empty strings and standard paths."""
        monkeypatch.chdir(tmp_path)
        settings = load_settings()
        ...
        assert settings.output.tmp_dir.endswith("saneless")
```

### `tests/test_pipeline.py` — preservation-guard cases

Analog `test_run_pipeline_upload_error` (`:80-98`) — the exact shape the upload-raises
preservation case extends with a `failed_dir` assertion:

```python
    def test_run_pipeline_upload_error(
        self,
        mock_scanner: MagicMock,
        default_settings: Settings,
        tmp_path: Path,
    ) -> None:
        """Paperless raises PaperlessError -> pipeline raises PaperlessError."""
        default_settings.output.tmp_dir = str(tmp_path)

        paperless = MagicMock()
        paperless.upload_document.side_effect = PaperlessError("Upload failed")

        request = PipelineRequest(profile_name="default", title="Upload Error Doc")
        with pytest.raises(PaperlessError, match="Upload failed"):
            run_pipeline(
                scanner=mock_scanner,
                paperless=paperless,
                settings=default_settings,
                request=request,
            )
```

The **poll-raises** twin — the case the roadmap note exists for — has no analog: no existing test
sets `paperless.poll_task.side_effect`. Build it by copying the above and moving the `side_effect`
onto `poll_task` (with `upload_document` returning a real `UploadResult(delivered_to_api=True,
task_uuid=...)`, which `tests/test_pipeline.py:14` already imports).

Temp-cleanup assertion to keep working (`:117-122`, `:144-146`):

```python
        # The TemporaryDirectory should be cleaned up
        remaining = list(tmp_path.iterdir())
        # Only the output PDF may remain, no tmp subdirs
        for item in remaining:
            assert not item.is_dir(), f"Leftover directory: {item}"
```

If `failed_dir` defaults under `tmp_path` in a test, this assertion trips — put `failed_dir` on a
separate `tmp_path` subtree.

### `tests/test_web_state_rendering.py` — FALLBACK rendering

Every test here is already `@pytest.mark.parametrize("state", list(JobState))`, so the new member
self-enrols. The two that need arms (`:160-167`) — note the `is` (identity) comparisons that make
the expectation exact rather than "contains":

```python
@pytest.mark.parametrize("state", list(JobState))
def test_history_cell_css_class(client: TestClient, state: JobState) -> None:
    """Only DONE and ERROR history cells carry a status CSS class (CTR-01)."""
    _job_in_state(client, state)
    text = client.get("/api/jobs/history").text
    assert ('<td class="status-done">' in text) is (state is JobState.DONE)
    assert ('<td class="status-error">' in text) is (state is JobState.ERROR)
```

Driver helper (`:133-139`):

```python
def _job_in_state(client: TestClient, state: JobState) -> None:
    """Create a job, drive it to `state`, and make it the worker's current job."""
    job_store: JobStore = _app(client).state.job_store
    job = job_store.create_job(profile="default", title="Render Test")
    job_store.update_state(job.id, state, error="disk on fire")
    _adopt_as_current_job(client, job.id)
```

Literal-markup spot check (`:198-200`) — the FALLBACK glyph/copy assertion goes beside it:

```python
    if state is JobState.DONE:
        assert '<p class="status-done">&#10003; Done: Render Test</p>' in text
```

The module docstring (`:1-17`) states the file's own contract — "Every case is parametrised over
`list(JobState)` rather than a hand-written list of names, so an eighth member cannot be added
without forcing a decision here". Phase 23 *is* that eighth member.

### `tests/test_cli.py` — `saneless jobs` FALLBACK case

Analog `TestJobsCommand` (`:387-440`) — `_populate_store` + `_patch_cli` + `runner.invoke`:

```python
class TestJobsCommand:
    """Jobs command tests."""

    def _populate_store(self, db_path: str, count: int = 2) -> None:
        """Populate a JobStore at db_path with test jobs."""
        store = JobStore(db_path=db_path)
        for i in range(count):
            store.create_job(
                profile="default" if i % 2 == 0 else "photo",
                title=f"Test Document {i + 1}",
            )
        store.close()
```

Note `db_path = str(tmp_path / "saneless.db")` is built in each test from `tmp_dir` — once
`cli.py:227` reads `settings.output.db_path`, **every test in this class must switch its
`OutputConfig` to set `data_dir`**, or the CLI opens a different database than the test populated.
That is a real breakage, not a cosmetic one:

```python
        db_path = str(tmp_path / "saneless.db")
        settings = _make_settings(
            output=OutputConfig(
                tmp_dir=str(tmp_path),
                log_file=str(tmp_path / "saneless.log"),
            ),
        )
```

The table assertion `assert "PENDING" in result.output` (`:439`) is what "distinct in
`saneless jobs` output" is measured against today.

### `tests/test_outcomes_e2e.py` — new file

Worker-driving analog (`tests/test_worker.py:191-218`) — **but note the monkeypatch of
`saneless.worker.run_pipeline`, which OUTC-10 must NOT do**:

```python
    def test_worker_processes_job(
        self,
        mock_scanner: MagicMock,
        mock_paperless: MagicMock,
        default_settings: Settings,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Submit job to worker -> job reaches DONE state."""
        store = JobStore()
        try:
            # Mock run_pipeline to succeed
            monkeypatch.setattr(
                "saneless.worker.run_pipeline",
                lambda *_args, **_kwargs: _success_result(),
            )

            worker = ScanWorker(mock_scanner, mock_paperless, default_settings, store)
            worker.start()

            job = store.create_job("default", "Worker Test")
            worker.submit(job)

            # Wait for processing
            time.sleep(0.5)
            worker.stop()

            fetched = _get(store, job.id)
            assert fetched.state == JobState.DONE
        finally:
            store.close()
```

All 49 `ScanWorker(...)` construction sites in `test_worker.py` follow this shape; **every one of
them monkeypatches `run_pipeline`**, so there is no existing test that runs the real pipeline
through the real worker. OUTC-10 is the first.

The narrowing helper to reuse (`tests/test_worker.py:31-35`):

```python
def _get(store: JobStore, job_id: str) -> Job:
    """Retrieve a job, asserting it exists (narrows Job | None to Job)."""
    fetched = store.get_job(job_id)
    assert fetched is not None
    return fetched
```

The closest thing to a polling wait helper in the repo (`tests/test_browser.py:102-109`) — this is
the loop shape `wait_for_state` should take, including the `for/else` + `raise`:

```python
    # Wait for server startup
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    else:
        msg = "Uvicorn server failed to start"
        raise RuntimeError(msg)
```

A second instance of the same shape lives at `tests/test_worker.py` in the manual-duplex class
(`for _ in range(50): time.sleep(0.05)`), confirming this is the house idiom rather than a one-off.

### `tests/test_browser.py` — the Playwright FALLBACK check

Marker + method shape (`:119-126`) — CLAUDE.md forbids marking browser checks "manual-only", so
the FALLBACK render gets a case here:

```python
@pytest.mark.browser
class TestBrowserRendering:
    """PicoCSS and semantic HTML rendering tests."""

    def test_page_loads_with_title(self, page: Page, browser_server_url: str) -> None:
        """Main page loads and has the saneless title."""
        page.goto(browser_server_url)
        assert "saneless" in page.title().lower()
```

Every existing browser test exercises only the idle page — there is **no precedent for driving a
job into a terminal state in the browser suite**. The `_job_in_state` helper from
`test_web_state_rendering.py:133-139` is the closest model, but it operates on a `TestClient`, not
on the uvicorn-backed `browser_server_url` fixture (`:80-116`). Reaching the live app's job store
from the test process is the new problem the planner must solve for that case.

---

## Shared Patterns

### Total lookups: `match` + `assert_never`, never a dict

**Source:** `vocabulary.py:102-136`, `139-180`, `183-220`; `pipeline.py:48-87`
**Apply to:** `job_state_for` (D-02), `state_label`/`progress_label` FALLBACK arms (D-03),
`ConnectionStatus` message lookup (D-13)

Shape: `match X:` → per-member `case` assigning a local → `case _: assert_never(X)` → single
trailing `return local`. Docstring carries an explicit `Raises: AssertionError` line.

### Raising: `msg = ...` then `raise`

**Source:** `paperless.py:163-164`, `:181-185`, `:207-208`; `pipeline.py:140-144`;
`config.py:222-224`; `pdf.py:58-59`; `job.py` throughout
**Apply to:** every new raise in this phase

ruff `EM` forbids a string literal inside `raise X("...")`. The house form is always:

```python
        msg = f"..."
        raise PaperlessError(msg) from exc
```

`from exc` when chaining, `from last_error` when chaining a stored exception (`paperless.py:208`).

### Logging: lazy `%s` interpolation, never f-strings

**Source:** `paperless.py:165`, `:204`, `:240`, `:247`; `worker.py:260`; `job.py:577`, `:624`
**Apply to:** all new log lines

ruff `G` is enabled. Always `logger.warning("... %s", value)`, never
`logger.warning(f"... {value}")`.

### Enum <-> SQLite TEXT round trip

**Source:** write `job.py:554` (`JobState.PENDING.value`), `:618-620`; read `job.py:506`, `:508-510`, `:515`
**Apply to:** `finish_job`'s `outcome` and `state` binds

Write `.value`; read through the constructor with a `if row[...] else None` guard for nullable
columns.

### Docstrings on everything public (ruff `D`)

**Source:** every function in `src/`
**Apply to:** `finish_job`, `job_state_for`, `ConnectionStatus`, `PaperlessTimeoutError`, the
sanitiser, `OutputConfig.db_path` / `.failed_dir`

Google style with `Args:` / `Returns:` / `Raises:` sections. Single-line docstrings for trivial
members (`exceptions.py`, enum classes). `D203`/`D212` are ignored per `pyproject.toml`.

### Tests: parametrise over `list(EnumType)`, never a name roster

**Source:** `tests/test_vocabulary.py:51`, `:168`, `:196`, `:208`;
`tests/test_web_state_rendering.py:141`, `:160`, `:170`, `:180`
**Apply to:** every new completeness test, and `ConnectionStatus`

This is the mechanism that makes a new member fail the suite automatically. Combined with the
count guard at `tests/test_vocabulary.py:50` and the `is` identity comparisons at
`test_web_state_rendering.py:166-167`, it forces a deliberate decision per member.

### Tests: `store = JobStore()` / `try` / `finally: store.close()`

**Source:** `tests/test_job.py` and `tests/test_worker.py`, universally
**Apply to:** `TestFinishJob` and the OUTC-10 e2e cases

### Zero-sleep paperless stubbing

**Source:** `tests/test_paperless.py:29-33` + `paperless.py:91` (`_transport` kwarg) +
`paperless.py:90` (`max_retries`)
**Apply to:** OUTC-10's five parametrised cases

`max_retries=1` removes both `time.sleep(2**attempt)` calls (`paperless.py:177`, `:194`);
`paperless_task_timeout = 0.05` plus the new deadline clamp bounds the poll. No new seam, no
`time.sleep` monkeypatching.

---

## No Analog Found

The planner should use `23-RESEARCH.md`'s patterns rather than hunting for a codebase precedent
for these four. Each is genuinely the first of its kind in this repository.

| Item | Role | Data Flow | Why no analog |
|------|------|-----------|---------------|
| **Atomic `.part` + `os.replace`** (OUTC-05, `paperless.py`) | file I/O | file-I/O | `grep -rn "os.replace\|\.part\|fsync" src/` returns nothing. The repo has exactly one file-copy site (`paperless.py:203`, `shutil.copy2`) and it is the non-atomic one being replaced. `os` is not even imported in `paperless.py`. Follow RESEARCH § "Pattern 4" (including the A3 recommendation to use a dotfile prefix, `.name.pdf.part`, so paperless-ngx's inotify consumer skips it). |
| **Filename sanitiser** (OUTC-05, new function) | utility | transform | `auto_profiles._slugify` (`:33-44`) is the only slug helper and D-19 forbids reusing it — it is two `.replace` calls and passes `/`, `..`, NUL and control characters through. No allow-list, no length cap, and no path-safety test exists anywhere. Follow RESEARCH § "Pattern 3" and § "Security Domain"'s threat table. |
| **Preservation move out of a doomed `TemporaryDirectory`** (OUTC-04, `pipeline.py`) | orchestration | file-I/O | No `shutil.move` exists in `src/` (only `shutil.copy2` at `paperless.py:203`, `shutil.disk_usage` at `pipeline.py:137`, `shutil.get_terminal_size` at `cli.py:250`). Every current `except` in `pipeline.py` either re-raises or is absent; nothing rescues a file. RESEARCH § "Pattern 2" documents the three `shutil.move` traps (explicit-full-path form, mkdir inside the handler, do not mask the original). |
| **`wait_for_state` polling helper** (`tests/conftest.py`) | test helper | — | `tests/test_worker.py` contains 27 `time.sleep` calls and no wait helper. The closest shape is the `for/else` + `raise` startup loop at `tests/test_browser.py:102-109`, which waits on a uvicorn flag, not on a job row. |

**One more thing with no precedent, flagged rather than filed above:** there is **no computed
`@property` on any pydantic model** in this codebase. D-16's `db_path`/`failed_dir` are the first.
The nearest pattern is `Job.is_active` / `Job.is_busy` (`job.py:404-412`) on a plain dataclass.
Pydantic v2 allows a plain `@property` on a `BaseModel` without extra decoration, but neither `ty`
nor `pyrefly` has been exercised against one here — verify early.

---

## Metadata

**Analog search scope:** `src/saneless/` (17 modules incl. `web/` and `scanner/`),
`src/saneless/web/templates/` + `static/`, `tests/` (19 modules), `docs/` (19 files), `Dockerfile`,
`docker-compose.yml`, `pyproject.toml`, `README.md`.
**Files scanned:** 31 read in full or in targeted ranges; 6 additional files grepped for
`/tmp/saneless`, `saneless.db`, `saneless-data`, `os.replace`, `.part`, `fsync`, `shutil.move`.
**Pattern extraction date:** 2026-09-11

### Corrections to upstream planning docs found while mapping

1. `23-CONTEXT.md` and `23-RESEARCH.md` cite `docs/reference/web-api.md:55-56` as "the documented
   `test_connection` JSON values". The three rows are actually **`:54-56`** — `connected` is at
   `:54`. Two new rows for `not_found` and `server_error` join them.
2. `23-RESEARCH.md` § "Rendering surfaces inventory" says `tests/test_job.py:805-824` for the
   `@_locked` reflective test and `:826+` for the AST test. The actual spans are **`:800-823`** and
   **`:825-841`**, inside `class TestLockDiscipline` at **`:797`**.
3. `23-RESEARCH.md` lists `tests/test_vocabulary.py:50` / `:133` / `:152-166` / `:179-205` for the
   four hand-written lists. Verified actual: **`:50`** (count guard, inside a method spanning
   `:40-50`), **`:131-133`** (`test_terminal_states_membership`), **`:152-176`**
   (`test_state_label_strings` + the parametrised twin), **`:203-205`**
   (`test_terminal_states_have_progress_prose_for_totality`).
4. **Not flagged anywhere upstream:** `tests/conftest.py:133-140`'s `mock_paperless` fixture sets
   `paperless.poll_task.return_value = {"status": "SUCCESS"}`. It encodes the *old* `poll_task`
   contract and is consumed by `tests/test_pipeline.py` and `tests/test_worker.py`. It survives the
   rewrite only because a `MagicMock` return value is simply ignored once nothing reads it — but
   any new test that needs a *raising* poll must override it, and the FAILURE/TIMEOUT e2e cases
   cannot use this fixture at all. Add it to the Wave 0 list.
5. **Not flagged anywhere upstream:** every test in `tests/test_cli.py::TestJobsCommand`
   (`:387-520`, 7 methods) builds `db_path = str(tmp_path / "saneless.db")` by hand while
   configuring only `tmp_dir`. Once `cli.py:227` reads `settings.output.db_path`, all seven open a
   different database than they populated. This is Wave 0 breakage in a file the validation map
   lists only as "needs a FALLBACK case".
6. **Not flagged anywhere upstream:** all six methods of `tests/test_pdf.py::TestAssemblePdf`
   (`:12-85`) call `assemble_pdf([img], tmp_path)` positionally with two arguments. D-09's required
   `filename` argument breaks every one of them.
