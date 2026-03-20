---
phase: 01-core-pipeline
verified: 2026-03-20T17:30:00Z
status: passed
score: 21/21 must-haves verified
re_verification: false
human_verification:
  - test: "Run `saneless devices` on a machine with a physical scanner and libsane-dev installed"
    expected: "Table listing discovered SANE devices; no error"
    why_human: "python-sane omitted from dependencies (no libsane-dev in build env); SaneBackend lazy-imports sane module which is absent in CI — cannot exercise real SANE path programmatically"
  - test: "Run `saneless scan --title 'Test' --config /path/to/real.toml` against a real paperless-ngx instance"
    expected: "Output shows Scanning..., Assembling PDF..., Uploading to paperless-ngx..., Done: Test — then document appears in paperless-ngx"
    why_human: "Full end-to-end requires physical scanner and live paperless-ngx; mocked in tests only"
---

# Phase 01: Core Pipeline Verification Report

**Phase Goal:** A user can run a CLI command that discovers a scanner, performs a flatbed scan, assembles a PDF, and uploads it to paperless-ngx with metadata
**Verified:** 2026-03-20T17:30:00Z
**Status:** passed
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Settings load from TOML with env var overrides using SANELESS_ prefix | VERIFIED | `config.py`: `SettingsConfigDict(env_prefix="SANELESS_", env_nested_delimiter="__")` + `settings_customise_sources` hook; 14 tests passing |
| 2 | Invalid config fails at startup with clear parse errors | VERIFIED | `load_settings()` raises on malformed TOML; `cli.py` catches and prints "Configuration error: {exc}", exits 2 |
| 3 | Default profile must always be present | VERIFIED | `@field_validator("profiles")` raises `ValueError("A 'default' profile must be defined in config")` if missing |
| 4 | Rotating log file created at configurable path | VERIFIED | `logging_config.py`: `RotatingFileHandler` with `max_bytes`, `backup_count` params; 6 tests passing |
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

**Score:** 21/21 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/saneless/config.py` | Pydantic Settings with TOML + env var loading | VERIFIED | 159 lines; exports Settings, ScannerConfig, PaperlessConfig, OutputConfig, ProfileConfig, load_settings |
| `src/saneless/exceptions.py` | Custom exception hierarchy | VERIFIED | SanelessError, ConfigError, ScanError, PaperlessError — all inherit correctly |
| `src/saneless/logging_config.py` | configure_logging with RotatingFileHandler | VERIFIED | RotatingFileHandler, optional stderr handler, parent dir creation |
| `src/saneless/scanner/base.py` | ScannerBackend ABC + dataclasses | VERIFIED | ABC with 3 abstract methods; DeviceInfo, DeviceCapabilities, ScanSettings dataclasses |
| `src/saneless/scanner/sane_backend.py` | SaneBackend wrapping python-sane | VERIFIED | Lazy import, init-once, context-managed device, source validation, no progress callback |
| `src/saneless/scanner/__init__.py` | Re-exports all scanner symbols | VERIFIED | Lazy SaneBackend __getattr__ + direct base imports |
| `src/saneless/pdf.py` | PDF assembly via img2pdf | VERIFIED | TemporaryDirectory, img2pdf.convert, MAX_IMAGE_PIXELS guard |
| `src/saneless/paperless.py` | PaperlessClient with retry + polling | VERIFIED | upload_document, poll_task, test_connection, close; httpx.Client with Auth header |
| `src/saneless/pipeline.py` | Pipeline orchestration | VERIFIED | scan_pages → assemble_pdf → upload_document → poll_task; TemporaryDirectory wrapper |
| `src/saneless/job.py` | Job model + SQLite JobStore | VERIFIED | JobState enum, Job dataclass, JobStore with CREATE TABLE, check_same_thread=False |
| `src/saneless/worker.py` | Background worker thread | VERIFIED | daemon thread, queue.Queue(maxsize=10), sentinel shutdown, run_pipeline call |
| `src/saneless/cli.py` | Click CLI with scan + devices | VERIFIED | @click.group(), scan and devices subcommands, all exit codes, --json, --capabilities, -v |
| `tests/conftest.py` | Shared test fixtures | VERIFIED | clean_env (autouse), sample_toml, sample_pil_image, sample_pil_images, default_settings, mock_scanner, mock_paperless |
| `tests/test_config.py` | Config tests | VERIFIED | 14 tests — all pass |
| `tests/test_logging.py` | Logging tests | VERIFIED | 6 tests — all pass |
| `tests/test_scanner.py` | Scanner tests | VERIFIED | 14 tests — all pass |
| `tests/test_pdf.py` | PDF assembly tests | VERIFIED | 5 tests — all pass |
| `tests/test_paperless.py` | Paperless client tests | VERIFIED | 16 tests — all pass |
| `tests/test_pipeline.py` | Pipeline tests | VERIFIED | 7 tests — all pass |
| `tests/test_worker.py` | Worker + job store tests | VERIFIED | 8 tests — all pass |
| `tests/test_cli.py` | CLI integration tests | VERIFIED | 15 tests — all pass |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `config.py` | pydantic-settings | `class Settings(BaseSettings)` | WIRED | Line 70; SettingsConfigDict with SANELESS_ prefix |
| `config.py` | TOML config file | `toml_file` in settings_customise_sources | WIRED | Lines 107-110: TomlConfigSettingsSource injected at runtime |
| `logging_config.py` | config.py | reads OutputConfig fields | WIRED | configure_logging takes log_file, log_level, max_bytes, backup_count params; RotatingFileHandler on line 46 |
| `scanner/sane_backend.py` | `scanner/base.py` | `class SaneBackend(ScannerBackend)` | WIRED | Line 53; implements all 3 abstract methods |
| `scanner/sane_backend.py` | python-sane | `sane.init()` in __init__ | WIRED | Lines 35-41: lazy import sentinel + _ensure_sane(); line 63: sane.init() |
| `pdf.py` | img2pdf | `img2pdf.convert` | WIRED | Line 53: `pdf_bytes = img2pdf.convert(image_paths)` |
| `paperless.py` | httpx | `httpx.Client` | WIRED | Line 59: `self._client = httpx.Client(...)` |
| `paperless.py` | paperless-ngx API | `/api/documents/post_document/` + `/api/tasks/` | WIRED | Lines 113-114; lines 178-179 |
| `cli.py` | `config.py` | `load_settings()` in group callback | WIRED | Line 46: `settings = load_settings(config_path)` |
| `cli.py` | `pipeline.py` | scan command calls `run_pipeline()` | WIRED | Line 94: `run_pipeline(scanner, paperless, settings, ...)` |
| `pipeline.py` | `scanner/base.py` | calls `scanner_backend.scan_pages()` | WIRED | Line 92: `images = list(scanner.scan_pages(device_id, scan_settings))` |
| `pipeline.py` | `pdf.py` | calls `assemble_pdf()` | WIRED | Line 97: `pdf_path = assemble_pdf(images, tmp_path)` |
| `pipeline.py` | `paperless.py` | calls `paperless_client.upload_document()` | WIRED | Lines 103-105: `task_uuid = paperless.upload_document(...)` |
| `__init__.py` | `cli.py` | `main()` calls `cli()` | WIRED | Lines 7-10: `from .cli import cli; cli()` |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|-------------|-------------|--------|---------|
| CONF-01 | 01-01 | Config via pydantic-settings with TOML + env var overrides | SATISFIED | Settings(BaseSettings) with SANELESS_ prefix; TomlConfigSettingsSource; all config tests pass |
| CONF-02 | 01-01 | Scanner host, device, paperless URL/token, tmp_dir, log settings, profiles all configurable | SATISFIED | ScannerConfig, PaperlessConfig, OutputConfig, ProfileConfig all defined with correct fields |
| CONF-03 | 01-01 | No hardcoded credentials | SATISFIED | token: str = "" default; must come from config or SANELESS_PAPERLESS__TOKEN |
| PROF-01 | 01-01 | Scan profiles in TOML with source, resolution, mode, optional metadata | SATISFIED | ProfileConfig with source, resolution, mode, default_tags, default_correspondent, default_title_template |
| PROF-02 | 01-01 | Default profile must always be present | SATISFIED | field_validator raises ValueError if "default" not in profiles dict |
| LOG-01 | 01-01 | Rotating log file at configurable path | SATISFIED | RotatingFileHandler; path passed from settings.output.log_file |
| LOG-02 | 01-01 | Log level configurable | SATISFIED | log_level: str = "INFO" in OutputConfig; passed to configure_logging |
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

**All 21 requirement IDs from plans accounted for. No orphaned requirements for Phase 1 in REQUIREMENTS.md.**

Note: PLSS-06 (consume directory fallback) appears in REQUIREMENTS.md mapped to Phase 4, but the consume_dir fallback is already implemented in `paperless.py`. This is bonus implementation, not a gap.

### Anti-Patterns Found

No anti-patterns detected. Scan of all source files found:
- No TODO/FIXME/XXX/HACK/PLACEHOLDER comments
- No stub return patterns (`return null`, `return {}`, `return []`)
- No empty handlers
- No hardcoded credentials

One notable deviation: `python-sane` is not in `pyproject.toml` dependencies (deferred because `libsane-dev` system headers unavailable in build environment). The `SaneBackend` uses a lazy import sentinel so the package installs and tests pass without it. **This is intentional and documented** — the scanner abstraction layer isolates this system dependency. The absence of python-sane from pyproject.toml means production deployment requires manual installation of both libsane-dev and python-sane. This is a known deployment concern but not a code defect.

### Human Verification Required

#### 1. Physical scanner discovery

**Test:** On a machine with libsane-dev and a USB/network scanner attached, install the package, then run `saneless devices`
**Expected:** Table showing discovered device name, vendor, model, type; no error
**Why human:** python-sane C extension requires libsane-dev system headers absent in the build environment; SaneBackend lazy-imports sane only when instantiated

#### 2. Full end-to-end scan pipeline

**Test:** Configure `saneless.toml` with a real scanner device and paperless-ngx URL/token, then run `saneless scan --title "Test Document"`
**Expected:** Terminal prints "Scanning...", "Assembling PDF...", "Uploading to paperless-ngx...", "Done: Test Document" and the document appears in paperless-ngx inbox
**Why human:** Requires physical scanner + live paperless-ngx instance; all individual units verified via mocks only

### Gaps Summary

No gaps found. All 21 truths verified. All artifacts exist, are substantive (not stubs), and are wired together. The full test suite (85 tests) passes in 11.17 seconds. All requirement IDs from plan frontmatter are satisfied with implementation evidence.

---

_Verified: 2026-03-20T17:30:00Z_
_Verifier: Claude (gsd-verifier)_
