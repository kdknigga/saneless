---
phase: 13-review-hardening-cross-ai-review-findings
verified: 2026-03-22T15:10:00Z
status: passed
score: 9/9 must-haves verified
gaps: []
human_verification: []
---

# Phase 13: Review Hardening Verification Report

**Phase Goal:** Address 9 hardening items identified by cross-AI plan review (Gemini CLI) — disk space pre-flight checks, manual duplex data preservation, typed state machine events, exception sanitization, config writability validation, periodic job pruning, empty page detection toggle, threaded Uvicorn signal fix, and Docker Compose documentation
**Verified:** 2026-03-22T15:10:00Z
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| #  | Truth                                                                                                   | Status     | Evidence                                                                                             |
|----|---------------------------------------------------------------------------------------------------------|------------|------------------------------------------------------------------------------------------------------|
| 1  | Multi-page scan pipeline checks disk space before starting and raises a clear error if insufficient     | ✓ VERIFIED | `_check_disk_space()` in `pipeline.py:69-91`; called at `pipeline.py:350`; raises `ScanError`       |
| 2  | Manual duplex mismatch saves both passes as separate PDFs and uploads both to paperless-ngx            | ✓ VERIFIED | `_handle_duplex_mismatch()` at `pipeline.py:99-156`; uploads `(fronts)` and `(backs)` titles        |
| 3  | Worker state transitions use a typed enum callback, not string comparison against log messages          | ✓ VERIFIED | `PipelineEvent` StrEnum at `pipeline.py:37-45`; worker uses `event is PipelineEvent.X` at `worker.py:223-233` |
| 4  | GET /api/paperless/test 502 response contains sanitized error detail, never raw exception strings       | ✓ VERIFIED | `routes.py:134`: `"detail": type(exc).__name__`; test `test_paperless_test_502_sanitizes_exception` passes |
| 5  | Config validation at startup fails fast if tmp_dir or consume_dir paths are not writable               | ✓ VERIFIED | `validate_settings_dirs()` in `config.py:190-224`; called in `app.py:81` and `cli.py:66`            |
| 6  | Job history is pruned periodically during runtime (not only at application startup)                     | ✓ VERIFIED | `worker.py:267-270`: `self._job_store.prune()` in `finally` block after every job completion         |
| 7  | ProfileConfig.enable_empty_page_detection boolean toggle exists and defaults to True                   | ✓ VERIFIED | `config.py:76`: `enable_empty_page_detection: bool = True`; pipeline gated at `pipeline.py:394`     |
| 8  | Uvicorn test server will not raise ValueError in non-main thread                                        | ✓ VERIFIED | uvicorn `capture_signals()` natively skips signal handling when `current_thread() is not main_thread()`; `install_signal_handlers` not available in this uvicorn version |
| 9  | docker-compose.yml includes a comment warning that config.toml must exist on host before first run     | ✓ VERIFIED | `docker-compose.yml:16-18`: WARNING comment above volume mount line                                  |

**Score:** 9/9 truths verified

### Required Artifacts

| Artifact                             | Expected                                     | Status     | Details                                                                                |
|--------------------------------------|----------------------------------------------|------------|----------------------------------------------------------------------------------------|
| `src/saneless/pipeline.py`           | PipelineEvent enum and disk space check      | ✓ VERIFIED | `class PipelineEvent(StrEnum)`, `_check_disk_space()`, `_handle_duplex_mismatch()`    |
| `src/saneless/worker.py`             | Enum-based status callback and post-job pruning | ✓ VERIFIED | Imports `PipelineEvent`; `event is PipelineEvent.X` dispatch; `prune()` in `finally` |
| `src/saneless/config.py`             | Writability validation and empty page toggle | ✓ VERIFIED | `enable_empty_page_detection: bool = True`, `validate_settings_dirs()`, `min_free_space_mb` |
| `src/saneless/web/routes.py`         | Sanitized 502 error response                 | ✓ VERIFIED | `type(exc).__name__` replaces `str(exc)` in paperless_test endpoint                   |
| `tests/test_browser.py`              | Signal handler safety for non-main thread    | ✓ VERIFIED | Comment documents uvicorn's native thread-safe behavior; behavior confirmed by source  |
| `docker-compose.yml`                 | Config existence warning                     | ✓ VERIFIED | "WARNING: config.toml must exist on the host before first run."                        |

### Key Link Verification

| From                           | To                               | Via                                           | Status     | Details                                                                        |
|--------------------------------|----------------------------------|-----------------------------------------------|------------|--------------------------------------------------------------------------------|
| `src/saneless/config.py`       | `src/saneless/exceptions.py`     | ConfigError on unwritable dirs                | ✓ WIRED    | `raise ConfigError(msg)` at `config.py:208, 212, 218, 222`                    |
| `src/saneless/config.py`       | `src/saneless/pipeline.py`       | `enable_empty_page_detection` consumed by pipeline | ✓ WIRED | `if profile.enable_empty_page_detection:` at `pipeline.py:394`               |
| `src/saneless/pipeline.py`     | `src/saneless/worker.py`         | PipelineEvent enum used in status_callback    | ✓ WIRED    | `from .pipeline import PipelineEvent` at `worker.py:24`                       |
| `src/saneless/worker.py`       | `src/saneless/job.py`            | `job_store.prune()` called after job completion | ✓ WIRED  | `self._job_store.prune(...)` at `worker.py:267-270`                            |
| `src/saneless/pipeline.py`     | `src/saneless/pdf.py`            | `assemble_pdf` called for fronts and backs    | ✓ WIRED    | `assemble_pdf(fronts, ...)` and `assemble_pdf(backs, ...)` at `pipeline.py:125-126` |
| `src/saneless/pipeline.py`     | `src/saneless/paperless.py`      | `upload_document` called twice for partial PDFs | ✓ WIRED  | Two `paperless.upload_document()` calls at `pipeline.py:136-149`              |
| `src/saneless/config.py`       | `src/saneless/web/app.py`        | `validate_settings_dirs` called at startup    | ✓ WIRED    | `validate_settings_dirs(settings)` at `app.py:81`                             |
| `src/saneless/config.py`       | `src/saneless/cli.py`            | `validate_settings_dirs` called in CLI        | ✓ WIRED    | `validate_settings_dirs(settings)` at `cli.py:66`                             |

### Requirements Coverage

| Requirement | Source Plan | Description                                                                                   | Status      | Evidence                                                          |
|-------------|------------|-----------------------------------------------------------------------------------------------|-------------|-------------------------------------------------------------------|
| RH-01       | 13-02      | Disk space pre-flight check before scan                                                       | ✓ SATISFIED | `_check_disk_space()` in `pipeline.py:69-91`, called at `pipeline.py:350` |
| RH-02       | 13-03      | Manual duplex mismatch saves partial PDFs instead of discarding                               | ✓ SATISFIED | `_handle_duplex_mismatch()` uploads both fronts/backs to paperless-ngx |
| RH-03       | 13-02      | Typed enum events for worker state transitions                                                | ✓ SATISFIED | `PipelineEvent` StrEnum; `event is PipelineEvent.X` dispatch in worker |
| RH-04       | 13-01      | Sanitized 502 error responses from paperless test endpoint                                    | ✓ SATISFIED | `type(exc).__name__` at `routes.py:134`; test verifies no IP/token leakage |
| RH-05       | 13-01      | Config writability validation at startup with ConfigError                                     | ✓ SATISFIED | `validate_settings_dirs()` raises `ConfigError`; called in app lifespan and CLI |
| RH-06       | 13-02      | Periodic job pruning during runtime (per-job, not only startup)                               | ✓ SATISFIED | `self._job_store.prune()` in worker `finally` block at `worker.py:267-270` |
| RH-07       | 13-01      | `enable_empty_page_detection` boolean toggle on `ProfileConfig`                               | ✓ SATISFIED | `config.py:76`; pipeline gated at `pipeline.py:394`              |
| RH-08       | 13-01      | Uvicorn test server safe from ValueError in non-main thread                                   | ✓ SATISFIED | `uvicorn.Server.capture_signals()` natively skips signal handling in non-main threads; `install_signal_handlers` not present in installed uvicorn version |
| RH-09       | 13-01      | Docker Compose warns config.toml must exist before first run                                  | ✓ SATISFIED | Warning comment at `docker-compose.yml:16-18`                    |

### Anti-Patterns Found

None detected. No TODO/FIXME/placeholder patterns, empty implementations, or hardcoded stub data found in modified files.

### Human Verification Required

None. All RH requirements are verifiable from code and tests. Browser/hardware checks are not required for this hardening phase.

### Gaps Summary

No gaps. All 9 hardening requirements (RH-01 through RH-09) are implemented, wired, and covered by passing tests. 124 tests pass across `test_config.py`, `test_pipeline.py`, `test_web.py`, and `test_worker.py`. Ruff and ty both report zero errors.

One notable implementation deviation from the plan: RH-08 prescribed `install_signal_handlers=False` in the uvicorn config, but the installed uvicorn version does not expose that parameter. The goal (no ValueError in non-main thread) is satisfied by uvicorn's `capture_signals()` method, which explicitly skips signal installation when `current_thread() is not main_thread()`. This is the correct and idiomatic solution for the installed version.

---

_Verified: 2026-03-22T15:10:00Z_
_Verifier: Claude (gsd-verifier)_
