---
phase: 01-core-pipeline
verified: 2026-03-21T14:30:00Z
status: passed
score: 23/23 must-haves verified
re_verification:
  previous_status: passed
  previous_score: 21/21
  gaps_closed:
    - "CLI commands work without root privileges (PermissionError on log mkdir)"
    - "Logging defaults to a user-writable XDG path (~/.local/state/saneless)"
    - "Logging gracefully degrades if directory creation fails (stderr fallback)"
  gaps_remaining: []
  regressions: []
human_verification:
  - test: "Run `saneless devices` on a machine with a physical scanner attached"
    expected: "Table listing discovered SANE devices; no error"
    why_human: "Requires physical scanner hardware — cannot be stubbed"
  - test: "Run `saneless scan --title 'Test' --config /path/to/real.toml` against a real paperless-ngx instance"
    expected: "Output shows Scanning..., Assembling PDF..., Uploading to paperless-ngx..., Done: Test — then document appears in paperless-ngx"
    why_human: "Requires physical scanner and live paperless-ngx instance — cannot be stubbed"
---

# Phase 01: Core Pipeline Verification Report

**Phase Goal:** Build the complete flatbed scan pipeline — config, scanner abstraction, PDF assembly, paperless-ngx client, job persistence, worker, pipeline orchestration, and CLI commands.
**Verified:** 2026-03-21T14:30:00Z
**Status:** passed
**Re-verification:** Yes — after gap closure (plan 01-04: LOG-01, LOG-02 log path fix)

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Settings load from TOML with env var overrides using SANELESS_ prefix | VERIFIED | `config.py`: `SettingsConfigDict(env_prefix="SANELESS_", env_nested_delimiter="__")` + `settings_customise_sources` hook; 14 tests passing |
| 2 | Invalid config fails at startup with clear parse errors | VERIFIED | `load_settings()` raises on malformed TOML; `cli.py` catches and prints "Configuration error: {exc}", exits 2 |
| 3 | Default profile must always be present | VERIFIED | `@field_validator("profiles")` raises `ValueError("A 'default' profile must be defined in config")` if missing |
| 4 | Rotating log file created at configurable path | VERIFIED | `logging_config.py`: `RotatingFileHandler` with `max_bytes`, `backup_count` params; 8 tests passing (2 new) |
| 5 | No hardcoded credentials | VERIFIED | `PaperlessConfig.token` defaults to `""`; token only from TOML or `SANELESS_PAPERLESS__TOKEN` env var |
| 6 | Scanner operations go through abstraction layer | VERIFIED | `scanner/base.py`: `ScannerBackend(ABC)` with `get_devices`, `get_capabilities`, `scan_pages` abstract methods |
| 7 | SaneBackend calls sane.init() exactly once | VERIFIED | `sane_backend.py` line 63: `self._sane_version = sane.init()` in `__init__`; test `test_sane_backend_init_calls_sane_init_exactly_once` passes |
| 8 | Device handles opened in context manager with cancel+close | VERIFIED | `_open_device()` context manager: `finally: dev.cancel(); dev.close()` on all paths |
| 9 | Scanned images assembled into PDF via img2pdf using temp files | VERIFIED | `pdf.py`: saves PIL Images as PNG to `TemporaryDirectory`, calls `img2pdf.convert(image_paths)` |
| 10 | Temp files cleaned up on success and error | VERIFIED | `pdf.py`: `TemporaryDirectory` context manager; `pipeline.py`: outer `TemporaryDirectory` for the whole pipeline run |
| 11 | Paperless client uploads PDF with metadata | VERIFIED | `paperless.py`: `upload_document` builds multipart with title, tags (repeated fields), correspondent, created; POSTs to `/api/documents/post_document/` |
| 12 | Paperless client polls task endpoint with exponential backoff | VERIFIED | `poll_task()`: delay doubles (0.5 → 30.0 cap) until SUCCESS/FAILURE or timeout |
| 13 | Connection test distinguishes unreachable, token_rejected, connected | VERIFIED | `test_connection()` returns one of three strings; 3 dedicated tests passing |
| 14 | Upload retries 3 times on network errors, no retry on 4xx | VERIFIED | Retry loop catches `ConnectError`/`TimeoutException`; raises `PaperlessError` immediately on 4xx |
| 15 | User can run `saneless devices` to see SANE scanner table | VERIFIED | `cli.py` `devices` command: table format + `--json` + `--capabilities`; `uv run saneless --help` shows command |
| 16 | User can run `saneless scan --title 'X'` to trigger full pipeline | VERIFIED | `cli.py` `scan` command calls `run_pipeline`; status callback echoes Scanning/Assembling/Uploading/Done |
| 17 | Background worker thread processes jobs from queue.Queue | VERIFIED | `worker.py`: `threading.Thread(daemon=True)` + `queue.Queue(maxsize=10)`; sentinel None stops loop |
| 18 | CLI prints status lines in correct order | VERIFIED | `pipeline.py` calls `notify("Scanning...")`, `notify("Assembling PDF...")`, `notify("Uploading to paperless-ngx...")`, `notify(f"Done: {title}")`; test `test_run_pipeline_status_callback` passes |
| 19 | CLI exits 0/1/2/3 on success/scan-error/config-error/paperless-error | VERIFIED | `cli.py`: `sys.exit(1)` on ScanError, `sys.exit(2)` on config/validation error, `sys.exit(3)` on PaperlessError; 3 exit-code tests passing |
| 20 | `saneless devices --json` outputs valid JSON | VERIFIED | JSON branch in `devices` command; `test_devices_json_output` passes |
| 21 | `-v` flag enables DEBUG logging to stderr | VERIFIED | `configure_logging(..., verbose=verbose)` in `cli` group callback; `test_verbose_flag` passes |
| 22 | CLI commands work without root privileges (LOG-01/LOG-02 gap) | VERIFIED | `OutputConfig.log_file` defaults to `~/.local/state/saneless/saneless.log` (line 68 of config.py); `configure_logging` wraps `mkdir` + `RotatingFileHandler` in `try/except OSError` with stderr fallback; `test_unwritable_directory_falls_back_to_stderr` passes |
| 23 | Logging gracefully degrades when log directory is not writable (LOG-02 gap) | VERIFIED | `logging_config.py` lines 56-60: `except OSError` attaches `StreamHandler(sys.stderr)` and emits warning; `test_default_log_file_is_xdg_compliant` asserts `.local/state/saneless` in path and no `/var/log` |

**Score:** 23/23 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/saneless/config.py` | Pydantic Settings with TOML + env var loading; XDG log path default | VERIFIED | 171 lines; `log_file` default uses `Path.home() / ".local" / "state" / "saneless" / "saneless.log"` |
| `src/saneless/exceptions.py` | Custom exception hierarchy | VERIFIED | SanelessError, ConfigError, ScanError, PaperlessError — all inherit correctly |
| `src/saneless/logging_config.py` | configure_logging with RotatingFileHandler + OSError fallback | VERIFIED | `try/except OSError` wraps mkdir + file handler; fallback attaches `StreamHandler(sys.stderr)` |
| `src/saneless/scanner/base.py` | ScannerBackend ABC + dataclasses | VERIFIED | ABC with 3 abstract methods; DeviceInfo, DeviceCapabilities, ScanSettings dataclasses |
| `src/saneless/scanner/sane_backend.py` | SaneBackend wrapping python-sane | VERIFIED | Lazy import, init-once, context-managed device, source validation |
| `src/saneless/scanner/__init__.py` | Re-exports all scanner symbols | VERIFIED | Lazy SaneBackend __getattr__ + direct base imports |
| `src/saneless/pdf.py` | PDF assembly via img2pdf | VERIFIED | TemporaryDirectory, img2pdf.convert, MAX_IMAGE_PIXELS guard |
| `src/saneless/paperless.py` | PaperlessClient with retry + polling | VERIFIED | upload_document, poll_task, test_connection, close; httpx.Client with Auth header |
| `src/saneless/pipeline.py` | Pipeline orchestration | VERIFIED | scan_pages → assemble_pdf → upload_document → poll_task; TemporaryDirectory wrapper |
| `src/saneless/job.py` | Job model + SQLite JobStore | VERIFIED | JobState enum, Job dataclass, JobStore with CREATE TABLE, check_same_thread=False |
| `src/saneless/worker.py` | Background worker thread | VERIFIED | daemon thread, queue.Queue(maxsize=10), sentinel shutdown, run_pipeline call |
| `src/saneless/cli.py` | Click CLI with scan + devices | VERIFIED | @click.group(), scan and devices subcommands, all exit codes, --json, --capabilities, -v |
| `tests/test_logging.py` | Logging tests including XDG path and OSError fallback | VERIFIED | 8 tests — all pass (2 new: `test_unwritable_directory_falls_back_to_stderr`, `test_default_log_file_is_xdg_compliant`) |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `config.py` | pydantic-settings | `class Settings(BaseSettings)` | WIRED | Line 80; SettingsConfigDict with SANELESS_ prefix |
| `config.py` | TOML config file | `toml_file` in settings_customise_sources | WIRED | Lines 118-123: TomlConfigSettingsSource injected at runtime |
| `logging_config.py` | `config.py` | `log_file` from `settings.output.log_file` | WIRED | `cli.py` line 57: `settings.output.log_file` passed to `configure_logging` |
| `logging_config.py` | OSError fallback | `except OSError` block adds stderr handler | WIRED | Lines 56-60: catches OSError, attaches StreamHandler, emits warning |
| `scanner/sane_backend.py` | `scanner/base.py` | `class SaneBackend(ScannerBackend)` | WIRED | Implements all 3 abstract methods |
| `scanner/sane_backend.py` | python-sane | `sane.init()` in __init__ | WIRED | Lazy import sentinel + _ensure_sane(); sane.init() in __init__ |
| `pdf.py` | img2pdf | `img2pdf.convert` | WIRED | Line 53: `pdf_bytes = img2pdf.convert(image_paths)` |
| `paperless.py` | httpx | `httpx.Client` | WIRED | `self._client = httpx.Client(...)` |
| `paperless.py` | paperless-ngx API | `/api/documents/post_document/` + `/api/tasks/` | WIRED | POST and GET endpoints with correct paths |
| `cli.py` | `config.py` | `load_settings()` in group callback | WIRED | Line 46: `settings = load_settings(config_path)` |
| `cli.py` | `pipeline.py` | scan command calls `run_pipeline()` | WIRED | `run_pipeline(scanner, paperless, settings, ...)` |
| `pipeline.py` | `scanner/base.py` | calls `scanner_backend.scan_pages()` | WIRED | `images = list(scanner.scan_pages(device_id, scan_settings))` |
| `pipeline.py` | `pdf.py` | calls `assemble_pdf()` | WIRED | `pdf_path = assemble_pdf(images, tmp_path)` |
| `pipeline.py` | `paperless.py` | calls `paperless_client.upload_document()` | WIRED | `task_uuid = paperless.upload_document(...)` |
| `__init__.py` | `cli.py` | `main()` calls `cli()` | WIRED | `from .cli import cli; cli()` |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|-------------|--------|---------|
| CONF-01 | 01-01 | Config via pydantic-settings with TOML + env var overrides | SATISFIED | Settings(BaseSettings) with SANELESS_ prefix; TomlConfigSettingsSource; all config tests pass |
| CONF-02 | 01-01 | Scanner host, device, paperless URL/token, tmp_dir, log settings, profiles all configurable | SATISFIED | ScannerConfig, PaperlessConfig, OutputConfig, ProfileConfig all defined with correct fields |
| CONF-03 | 01-01 | No hardcoded credentials | SATISFIED | token: str = "" default; must come from config or SANELESS_PAPERLESS__TOKEN |
| PROF-01 | 01-01 | Scan profiles in TOML with source, resolution, mode, optional metadata | SATISFIED | ProfileConfig with source, resolution, mode, default_tags, default_correspondent, default_title_template |
| PROF-02 | 01-01 | Default profile must always be present | SATISFIED | field_validator raises ValueError if "default" not in profiles dict |
| LOG-01 | 01-01, 01-04 | Rotating log file at configurable path | SATISFIED | RotatingFileHandler; path from `settings.output.log_file`; XDG default (~/.local/state/saneless); OSError fallback to stderr; 8 logging tests all pass |
| LOG-02 | 01-01, 01-04 | Log level configurable | SATISFIED | `log_level: str = "INFO"` in OutputConfig; passed to configure_logging; test_log_level_from_config passes |
| SCAN-01 | 01-02 | User can discover available SANE devices | SATISFIED | SaneBackend.get_devices() + CLI devices command |
| SCAN-02 | 01-02 | User can pin target device by name in config | SATISFIED | settings.scanner.device used by pipeline; ScannerConfig.device field |
| SCAN-03 | 01-02 | Flatbed single-page scan | SATISFIED | scan_pages() with source="Flatbed" from ProfileConfig default; yields one image from dev.snap() |
| ARCH-01 | 01-02 | Scanner behind abstraction layer | SATISFIED | ScannerBackend ABC; SaneBackend implements it |
| ARCH-03 | 01-02 | SANE initialized once at startup | SATISFIED | sane.init() in SaneBackend.__init__ exactly once |
| PDF-01 | 01-02 | PDF assembly via img2pdf (lossless) | SATISFIED | img2pdf.convert with PNG temp files (lossless format) |
| PDF-02 | 01-02 | Temp files in configurable tmp_dir, cleaned up | SATISFIED | TemporaryDirectory in assemble_pdf and run_pipeline |
| LOG-04 | 01-02 | Temp files cleaned up on error | SATISFIED | TemporaryDirectory context manager guarantees cleanup on all paths including exceptions |
| PLSS-01 | 01-02 | Upload to paperless-ngx with metadata (title, created, correspondent, tags) | SATISFIED | upload_document multipart form with all metadata fields |
| PLSS-02 | 01-02 | Poll task endpoint until terminal state | SATISFIED | poll_task() with exponential backoff; returns SUCCESS/FAILURE/TIMEOUT |
| PLSS-03 | 01-02 | Test connection distinguishing 3 failure modes | SATISFIED | test_connection() returns "connected", "token_rejected", "unreachable" |
| ARCH-02 | 01-03 | Background worker thread with queue.Queue | SATISFIED | ScanWorker: daemon thread + Queue(maxsize=10) + sentinel shutdown |
| CLI-01 | 01-03 | `saneless scan [--profile] [--title]` triggers scan job | SATISFIED | scan command with --profile (default="default"), --title (required) |
| CLI-02 | 01-03 | `saneless devices` lists available SANE devices | SATISFIED | devices command with table, --json, and --capabilities output modes |

**All 21 requirement IDs from plans accounted for. LOG-01 and LOG-02 re-verified after gap closure in plan 01-04. No orphaned requirements for Phase 1 in REQUIREMENTS.md.**

### Anti-Patterns Found

No anti-patterns detected. Scan of modified files (config.py, logging_config.py, test_logging.py) found:
- No TODO/FIXME/XXX/HACK/PLACEHOLDER comments
- No stub return patterns
- No empty handlers
- No hardcoded credentials
- No suppressed errors (no `# type: ignore`, `# noqa`)

### Human Verification Required

#### 1. Physical scanner discovery

**Test:** On a machine with a USB/network scanner attached, run `saneless devices`
**Expected:** Table showing discovered device name, vendor, model, type; no error
**Why human:** Requires physical scanner hardware — cannot be stubbed

#### 2. Full end-to-end scan pipeline

**Test:** Configure `saneless.toml` with a real scanner device and paperless-ngx URL/token, then run `saneless scan --title "Test Document"`
**Expected:** Terminal prints "Scanning...", "Assembling PDF...", "Uploading to paperless-ngx...", "Done: Test Document" and the document appears in paperless-ngx inbox
**Why human:** Requires physical scanner and live paperless-ngx instance — cannot be stubbed

### Gaps Summary

No gaps found. All 23 truths verified (21 original + 2 new from gap closure plan 01-04).

**Gap closure confirmed:** Plan 01-04 successfully addressed the LOG-01/LOG-02 blocker:
- `OutputConfig.log_file` now defaults to `~/.local/state/saneless/saneless.log` (XDG Base Directory compliant)
- `configure_logging` now wraps `mkdir` and `RotatingFileHandler` construction in `try/except OSError` with automatic fallback to a `StreamHandler(sys.stderr)`
- Two new tests added and passing: `test_unwritable_directory_falls_back_to_stderr` and `test_default_log_file_is_xdg_compliant`
- Both commits verified to exist: `6b4b0e0` (test/RED) and `69c775b` (feat/GREEN)
- Ruff lint + format: clean
- Full test suite: 221 passed in 24.27s (no regressions)

---

_Verified: 2026-03-21T14:30:00Z_
_Verifier: Claude (gsd-verifier)_
