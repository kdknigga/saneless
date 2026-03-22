# Phase 13: Review Hardening -- Cross-AI Review Findings - Research

**Researched:** 2026-03-22
**Domain:** Robustness, safety, and operational hardening (9 discrete improvements)
**Confidence:** HIGH

## Summary

This phase addresses 9 independent hardening items identified by cross-AI review. Each item is a focused, bounded change to an existing module. No new dependencies are needed -- all changes use Python stdlib (`shutil`, `os`, `enum`) and existing project libraries (Pydantic, FastAPI, Pillow, img2pdf). The changes span 8 source files and 2 non-source files (docker-compose.yml, test_browser.py).

The items are highly independent: only RH-02 (duplex mismatch recovery) and RH-03 (typed state events) have a minor interaction point (the worker status callback), but they touch different code paths. All other items can be implemented in any order with no dependency conflicts.

**Primary recommendation:** Group into 2 plans -- one for the 6 simpler items (RH-04 through RH-09), one for the 3 more involved items (RH-01, RH-02, RH-03) -- to keep each plan under 5 tasks.

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions
- **D-01:** Fixed headroom check -- verify minimum free space in `tmp_dir` before starting any scan (flatbed or ADF)
- **D-02:** Hard fail -- raise a clear error if insufficient space, no partial output, user retries after freeing space
- **D-03:** Threshold is configurable via `min_free_space_mb` config field (sensible default, e.g., 500MB)
- **D-04:** Check applies to all scan types (flatbed and ADF), not just multi-page
- **D-05:** On page count mismatch, save both passes as separate PDFs (fronts.pdf and backs.pdf)
- **D-06:** Both partial PDFs are uploaded to paperless-ngx (not just consume directory)
- **D-07:** Job completes with DONE state (not ERROR), with a warning message like "Page count mismatch: 25 fronts, 24 backs. Partial PDFs saved."
- **D-08:** Create a typed enum for worker state transition events (replaces string matching against log messages)
- **D-09:** Pipeline status callback passes enum events, not human-readable strings
- **D-10:** `GET /api/paperless/test` 502 response returns sanitized error class name, never raw exception strings
- **D-11:** Full exception detail logged server-side only
- **D-12:** At startup, validate that `tmp_dir` and `consume_dir` (if set) are writable
- **D-13:** Fail fast with `ConfigError` if permission check fails -- don't wait until mid-scan
- **D-14:** Prune job history after each scan job completes (piggyback, no extra threads)
- **D-15:** No configurable interval -- prune runs on every job completion using existing max_age/max_rows settings
- **D-16:** Add `enable_empty_page_detection: bool = True` to `ProfileConfig`
- **D-17:** When False, skip empty page filtering entirely -- no threshold manipulation needed
- **D-18:** Test server Uvicorn config disables signal handlers when running in non-main thread
- **D-19:** Add comments to docker-compose.yml warning that config.toml must exist on host before first run

### Claude's Discretion
- Exact disk space estimation formula and default threshold value
- Enum naming and member names for typed state events
- Sanitization function implementation details
- Config validation approach (test write vs. `os.access`)
- File naming pattern for partial duplex PDFs (e.g., `{title}_fronts.pdf`)
- Where to place the prune() call in the worker flow

### Deferred Ideas (OUT OF SCOPE)
- SQLite WAL mode (raised in Phase 3 review) -- not in RH requirements, could be separate phase
- Orphaned timeout threads in scanner executor (Phase 2 review) -- complex, not in scope
- Port conflict handling for `saneless serve` (Phase 5 review) -- nice-to-have, not in scope
</user_constraints>

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| RH-01 | Disk space pre-flight check | `shutil.disk_usage()` on `tmp_dir`; new `min_free_space_mb` field in `OutputConfig`; check at top of `run_pipeline()` |
| RH-02 | Manual duplex mismatch saves partial PDFs | Replace `ScanError` raise in `_interleave_duplex` with `assemble_pdf()` for each pass; upload both via `paperless.upload_document()` |
| RH-03 | Typed state machine events | New `PipelineEvent` StrEnum in `pipeline.py`; `status_callback` signature changes from `str` to enum; worker `_status_cb` dispatches on enum |
| RH-04 | Exception sanitization in paperless test | Replace `str(exc)` with `type(exc).__name__` in `paperless_test()` route handler |
| RH-05 | Config writability validation | Pydantic `model_validator` on `Settings` checking `os.access(path, os.W_OK)` for `tmp_dir` and `consume_dir` |
| RH-06 | Periodic job pruning | Add `self._job_store.prune()` call in worker `_process_job()` finally block |
| RH-07 | Empty page detection toggle | Add `enable_empty_page_detection: bool = True` to `ProfileConfig`; gate `filter_empty_pages` call in `run_pipeline()` |
| RH-08 | Uvicorn signal handler fix | Add `install_signal_handlers=False` to `uvicorn.Config()` in `test_browser.py` |
| RH-09 | Docker Compose documentation | Add YAML comments to `docker-compose.yml` volume mount line |
</phase_requirements>

## Standard Stack

No new dependencies needed. All changes use existing project libraries:

### Core (already installed)
| Library | Purpose | Used For |
|---------|---------|----------|
| Python `shutil` | Disk usage checking | `shutil.disk_usage()` for RH-01 |
| Python `os` | Path writability checks | `os.access(path, os.W_OK)` for RH-05 |
| Python `enum.StrEnum` | Typed enumerations | `PipelineEvent` enum for RH-03 |
| Pydantic | Config validation | `model_validator` for RH-05 writability checks |
| img2pdf / Pillow | PDF assembly | Reusing `assemble_pdf()` for partial PDFs in RH-02 |

## Architecture Patterns

### Pattern 1: StrEnum for All Enums
**What:** Project uses `StrEnum` (Python 3.14 stdlib) for all enum types.
**Evidence:** `JobState(StrEnum)` and `ErrorCategory(StrEnum)` in `job.py`.
**Apply to RH-03:** New `PipelineEvent` must follow the same pattern.

```python
class PipelineEvent(StrEnum):
    """Events emitted by the scan pipeline to report progress."""
    SCANNING = "SCANNING"
    AWAITING_FLIP = "AWAITING_FLIP"
    SCANNING_REVERSE = "SCANNING_REVERSE"
    ASSEMBLING = "ASSEMBLING"
    UPLOADING = "UPLOADING"
    DONE = "DONE"
```

### Pattern 2: Pydantic Field Validators for Config
**What:** `@field_validator` and `@model_validator` on Settings/ProfileConfig classes.
**Evidence:** `validate_default_profile` in `config.py`.
**Apply to RH-05:** Use `@model_validator(mode='after')` on `Settings` to check writability after all fields are resolved.

```python
@model_validator(mode="after")
def validate_writable_dirs(self) -> Settings:
    """Fail fast if tmp_dir or consume_dir are not writable."""
    tmp = Path(self.output.tmp_dir)
    if tmp.exists() and not os.access(tmp, os.W_OK):
        msg = f"tmp_dir is not writable: {tmp}"
        raise ValueError(msg)
    consume = self.paperless.consume_dir
    if consume:
        consume_path = Path(consume)
        if consume_path.exists() and not os.access(consume_path, os.W_OK):
            msg = f"consume_dir is not writable: {consume_path}"
            raise ValueError(msg)
    return self
```

**Important nuance:** Only check writability if the directory already exists. Directories that don't exist yet are created lazily (e.g., `run_pipeline` creates `tmp_dir` with `mkdir(parents=True, exist_ok=True)`). For non-existent paths, check the parent directory's writability instead.

### Pattern 3: Callback-Driven Pipeline Status
**What:** `run_pipeline()` accepts a `status_callback` that the worker maps to state transitions.
**Current problem (RH-03):** The callback passes human-readable strings like `"Awaiting flip..."` and the worker does exact string matching against them.
**Fix:** Change callback signature to accept `PipelineEvent` enum values. The pipeline emits `PipelineEvent.AWAITING_FLIP`, the worker dispatches on enum members.

### Pattern 4: Worker Post-Job Hooks
**What:** The worker `_process_job()` method has a `finally` block for cleanup.
**Apply to RH-06:** Add `self._job_store.prune()` call in the finally block, after `self._current_job_id = None`. This piggybacks on existing cleanup flow.

### Anti-Patterns to Avoid
- **String-based dispatch:** Current `_status_cb` matches `msg == "Awaiting flip..."` -- this is the exact anti-pattern RH-03 fixes.
- **Leaking internal details in API responses:** Current `str(exc)` in the paperless test route can include tokens, IPs, or stack traces.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Disk space check | Custom statvfs call | `shutil.disk_usage()` | Cross-platform, returns named tuple with `free` field |
| PDF assembly for partial duplex | Custom byte concatenation | Existing `assemble_pdf()` | Already handles temp files, img2pdf encoding, cleanup |
| Path writability check | Try-write-delete pattern | `os.access(path, os.W_OK)` | Atomic check, no side effects, no cleanup needed |

## Common Pitfalls

### Pitfall 1: Config Validator Ordering with Non-Existent Dirs
**What goes wrong:** Checking writability on `tmp_dir` that hasn't been created yet causes false failures on first run.
**Why it happens:** `tmp_dir` defaults to `/tmp/saneless` which may not exist at settings load time.
**How to avoid:** Only validate writability if the path (or its parent) exists. For non-existent paths, check parent writability instead.
**Warning signs:** `ConfigError` on clean installs.

### Pitfall 2: Duplex Mismatch Recovery Changes Return Type
**What goes wrong:** `_interleave_duplex` currently raises `ScanError` -- callers expect either a page list or an exception. Changing to partial PDF upload means the caller must handle a new code path.
**Why it happens:** Recovery requires assembling and uploading PDFs from within the scan step, before the normal PDF assembly step.
**How to avoid:** Handle mismatch recovery inside `_scan_manual_duplex`, not in `_interleave_duplex`. The mismatch handler assembles partial PDFs, uploads them, and returns an empty list or a special sentinel to skip the normal PDF path. The pipeline caller then completes with DONE state and a warning message.
**Warning signs:** Double PDF assembly, or the pipeline trying to upload an empty page list.

### Pitfall 3: PipelineEvent Enum Changes Callback Signature
**What goes wrong:** Changing `status_callback` from `Callable[[str], None]` to `Callable[[PipelineEvent], None]` breaks all existing callers (worker, tests, CLI).
**Why it happens:** The callback type is used across modules.
**How to avoid:** Update all callers in the same changeset. The `PipelineRequest.status_callback` type annotation, `_noop_callback` signature, worker `_status_cb`, and any test mocks must all change together.
**Warning signs:** ty/pyrefly type errors about str vs PipelineEvent.

### Pitfall 4: Uvicorn Signal Handler ValueError
**What goes wrong:** `ValueError: set_wakeup_fd only works in main thread` when uvicorn runs in a daemon thread.
**Why it happens:** Uvicorn's default config calls `signal.set_wakeup_fd()` which only works in the main thread.
**How to avoid:** Pass `install_signal_handlers=False` to `uvicorn.Config()` in `test_browser.py`. This is the documented uvicorn parameter for embedded use.

### Pitfall 5: os.access vs Real Writability
**What goes wrong:** `os.access()` can give incorrect results on network filesystems, ACLs, or when running as root.
**Why it happens:** `os.access()` checks POSIX permission bits, not ACLs or filesystem quirks.
**How to avoid:** For this project (LAN scanner appliance, typically Docker), `os.access()` is sufficient. Document the limitation but don't over-engineer. The alternative (try-write-delete) has its own issues (creates temp files, cleanup race conditions).

## Code Examples

### RH-01: Disk Space Check
```python
# In pipeline.py, at top of run_pipeline()
import shutil

def _check_disk_space(tmp_dir: str, min_free_mb: int) -> None:
    """Raise ScanError if insufficient disk space in tmp_dir."""
    path = Path(tmp_dir)
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(path)
    free_mb = usage.free // (1024 * 1024)
    if free_mb < min_free_mb:
        msg = (
            f"Insufficient disk space: {free_mb} MB free, "
            f"{min_free_mb} MB required"
        )
        raise ScanError(msg)
```

### RH-02: Partial Duplex PDF Upload
```python
# In pipeline.py, replacing raise in _interleave_duplex or _scan_manual_duplex
def _handle_duplex_mismatch(
    fronts: list[Image.Image],
    backs: list[Image.Image],
    tmp_path: Path,
    paperless: PaperlessClient,
    title: str,
    tags: list[int] | None,
    correspondent: int | None,
    notify: Callable[[PipelineEvent], None],
) -> None:
    """Save and upload partial PDFs when duplex page counts mismatch."""
    fronts_pdf = assemble_pdf(fronts, tmp_path / "fronts")
    backs_pdf = assemble_pdf(backs, tmp_path / "backs")
    created = datetime.now(tz=UTC).isoformat()
    notify(PipelineEvent.UPLOADING)
    paperless.upload_document(fronts_pdf, f"{title} (fronts)", tags, correspondent, created)
    paperless.upload_document(backs_pdf, f"{title} (backs)", tags, correspondent, created)
```

### RH-03: Typed Pipeline Events
```python
# In pipeline.py
class PipelineEvent(StrEnum):
    SCANNING = "SCANNING"
    AWAITING_FLIP = "AWAITING_FLIP"
    SCANNING_REVERSE = "SCANNING_REVERSE"
    ASSEMBLING = "ASSEMBLING"
    UPLOADING = "UPLOADING"
    DONE = "DONE"
```

### RH-04: Exception Sanitization
```python
# In routes.py, paperless_test()
except Exception as exc:
    logger.warning("Paperless connection test failed: %s", exc)
    return JSONResponse(
        status_code=502,
        content={"status": "error", "detail": type(exc).__name__},
    )
```

### RH-08: Uvicorn Signal Fix
```python
# In test_browser.py
config = uvicorn.Config(
    app, host="127.0.0.1", port=0,
    log_level="warning",
    install_signal_handlers=False,  # RH-08: prevent ValueError in non-main thread
)
```

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.0.2 |
| Config file | `pyproject.toml` [tool.pytest.ini_options] |
| Quick run command | `uv run pytest tests/ -x --ignore=tests/test_browser.py` |
| Full suite command | `uv run pytest tests/ --ignore=tests/test_browser.py` |

### Phase Requirements -> Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| RH-01 | Disk space check raises ScanError when low | unit | `uv run pytest tests/test_pipeline.py -x -k disk_space` | Wave 0 |
| RH-02 | Duplex mismatch saves and uploads partial PDFs | unit | `uv run pytest tests/test_pipeline.py -x -k duplex_mismatch` | Wave 0 |
| RH-03 | Worker dispatches on PipelineEvent enum, not strings | unit | `uv run pytest tests/test_worker.py -x -k pipeline_event` | Wave 0 |
| RH-04 | Paperless test 502 returns class name, not raw exception | unit | `uv run pytest tests/test_web.py -x -k sanitize` | Wave 0 |
| RH-05 | Config validation fails on unwritable tmp_dir | unit | `uv run pytest tests/test_config.py -x -k writable` | Wave 0 |
| RH-06 | Job pruning runs after job completion | unit | `uv run pytest tests/test_worker.py -x -k prune` | Wave 0 |
| RH-07 | Empty page detection skipped when toggle is False | unit | `uv run pytest tests/test_pipeline.py -x -k empty_page_toggle` | Wave 0 |
| RH-08 | Uvicorn test server uses install_signal_handlers=False | integration | `uv run pytest tests/test_browser.py -x -k page_loads` | Existing (fixture change) |
| RH-09 | docker-compose.yml has config.toml warning comment | manual-only | Visual inspection of YAML comments | N/A |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/ -x --ignore=tests/test_browser.py`
- **Per wave merge:** `uv run pytest tests/ --ignore=tests/test_browser.py && uv run ruff check . && uv run ty check`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- New test functions needed in `tests/test_pipeline.py` for RH-01, RH-02, RH-07
- New test functions needed in `tests/test_worker.py` for RH-03, RH-06
- New test functions needed in `tests/test_web.py` for RH-04
- New test functions needed in `tests/test_config.py` for RH-05
- No new test files needed -- all fit in existing modules

## Sources

### Primary (HIGH confidence)
- Direct codebase inspection of all 8 affected source files
- Python stdlib docs: `shutil.disk_usage()`, `os.access()`, `enum.StrEnum`
- Uvicorn docs: `install_signal_handlers` parameter for embedded server usage

### Secondary (MEDIUM confidence)
- Pydantic `model_validator` usage verified against existing `field_validator` patterns in codebase

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - no new dependencies, all stdlib/existing
- Architecture: HIGH - all patterns directly observed in codebase
- Pitfalls: HIGH - identified from concrete code inspection

**Research date:** 2026-03-22
**Valid until:** 2026-04-22 (stable -- all stdlib patterns)
