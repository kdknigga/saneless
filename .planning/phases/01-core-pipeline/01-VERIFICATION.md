---
phase: 01-core-pipeline
verified: 2026-03-21T15:30:00Z
status: passed
score: 26/26 must-haves verified
re_verification:
  previous_status: passed
  previous_score: 23/23
  gaps_closed:
    - "User gets a helpful error message when TOML uses [default] instead of [profiles.default]"
    - "User can set title via 'title' field in [profiles.default] (alias for default_title_template)"
    - "Environment variable override works when config.toml has correct structure"
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

**Phase Goal:** Build the core scan-to-PDF-to-Paperless pipeline — config, scanner abstraction, PDF assembly, Paperless client, job persistence, worker, CLI.
**Verified:** 2026-03-21T15:30:00Z
**Status:** passed
**Re-verification:** Yes — after gap closure (plan 01-05: CONF-01, CONF-02, CONF-03 TOML error UX and title alias)

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Settings load from TOML with env var overrides using SANELESS_ prefix | VERIFIED | `config.py`: `SettingsConfigDict(env_prefix="SANELESS_", env_nested_delimiter="__")` + `settings_customise_sources` hook; 22 config tests passing |
| 2 | Invalid config fails at startup with clear parse errors | VERIFIED | `load_settings()` raises on malformed TOML; `cli.py` catches and prints "Configuration error: {exc}", exits 2 |
| 3 | Default profile must always be present | VERIFIED | `@field_validator("profiles")` raises `ValueError("A 'default' profile must be defined in config")` if missing |
| 4 | Rotating log file created at configurable path | VERIFIED | `logging_config.py`: `RotatingFileHandler` with `max_bytes`, `backup_count` params; 8 tests passing |
| 5 | No hardcoded credentials | VERIFIED | `PaperlessConfig.token` defaults to `""`; token only from TOML or `SANELESS_PAPERLESS__TOKEN` env var |
| 6 | Scanner operations go through abstraction layer | VERIFIED | `scanner/base.py`: `ScannerBackend(ABC)` with `get_devices`, `get_capabilities`, `scan_pages` abstract methods |
| 7 | SaneBackend calls sane.init() exactly once | VERIFIED | `sane_backend.py` line 63: `self._sane_version = sane.init()` in `__init__`; test `test_sane_backend_init_calls_sane_init_exactly_once` passes |
| 8 | Device handles opened in context manager with cancel+close | VERIFIED | `_open_device()` context manager: `finally: dev.cancel(); dev.close()` on all paths |
| 9 | Scanned images assembled into PDF via img2pdf using temp files | VERIFIED | `pdf.py`: saves PIL Images as PNG to `TemporaryDirectory`, calls `img2pdf.convert(image_paths)` |
| 10 | Temp files cleaned up on success and error | VERIFIED | `pdf.py`: `TemporaryDirectory` context manager; `pipeline.py`: outer `TemporaryDirectory` for the whole pipeline run |
| 11 | Paperless client uploads PDF with metadata | VERIFIED | `paperless.py`: `upload_document` builds multipart with title, tags (repeated fields), correspondent, created; POSTs to `/api/documents/post_document/` |
| 12 | Paperless client polls task endpoint with exponential backoff | VERIFIED | `poll_task()`: delay doubles (0.5 to 30.0 cap) until SUCCESS/FAILURE or timeout |
| 13 | Connection test distinguishes unreachable, token_rejected, connected | VERIFIED | `test_connection()` returns one of three strings; 3 dedicated tests passing |
| 14 | Upload retries 3 times on network errors, no retry on 4xx | VERIFIED | Retry loop catches `ConnectError`/`TimeoutException`; raises `PaperlessError` immediately on 4xx |
| 15 | User can run `saneless devices` to see SANE scanner table | VERIFIED | `cli.py` `devices` command: table format + `--json` + `--capabilities`; `uv run saneless --help` shows command |
| 16 | User can run `saneless scan --title 'X'` to trigger full pipeline | VERIFIED | `cli.py` `scan` command calls `run_pipeline`; status callback echoes Scanning/Assembling/Uploading/Done |
| 17 | Background worker thread processes jobs from queue.Queue | VERIFIED | `worker.py`: `threading.Thread(daemon=True)` + `queue.Queue(maxsize=10)`; sentinel None stops loop |
| 18 | CLI prints status lines in correct order | VERIFIED | `pipeline.py` calls `notify("Scanning...")`, `notify("Assembling PDF...")`, `notify("Uploading to paperless-ngx...")`, `notify(f"Done: {title}")`; test `test_run_pipeline_status_callback` passes |
| 19 | CLI exits 0/1/2/3 on success/scan-error/config-error/paperless-error | VERIFIED | `cli.py`: `sys.exit(1)` on ScanError, `sys.exit(2)` on config/validation error, `sys.exit(3)` on PaperlessError; 3 exit-code tests passing |
| 20 | `saneless devices --json` outputs valid JSON | VERIFIED | JSON branch in `devices` command; `test_devices_json_output` passes |
| 21 | `-v` flag enables DEBUG logging to stderr | VERIFIED | `configure_logging(..., verbose=verbose)` in `cli` group callback; `test_verbose_flag` passes |
| 22 | CLI commands work without root privileges (LOG-01/LOG-02 gap) | VERIFIED | `OutputConfig.log_file` defaults to `~/.local/state/saneless/saneless.log`; `configure_logging` wraps `mkdir` + `RotatingFileHandler` in `try/except OSError` with stderr fallback |
| 23 | Logging gracefully degrades when log directory is not writable (LOG-02 gap) | VERIFIED | `logging_config.py`: `except OSError` attaches `StreamHandler(sys.stderr)` and emits warning; `test_default_log_file_is_xdg_compliant` asserts `.local/state/saneless` in path |
| 24 | User gets a helpful error message when TOML uses [default] instead of [profiles.default] | VERIFIED | `_build_settings()` catches `ValidationError` with `extra_forbidden` type, builds ConfigError with "Did you mean [profiles.{field}]?" message; `test_wrong_section_name_gives_helpful_error` passes |
| 25 | User can set title via 'title' field in [profiles.default] (alias for default_title_template) | VERIFIED | `ProfileConfig`: `default_title_template = Field(default="", alias="title")` with `ConfigDict(populate_by_name=True)`; `test_title_alias_works` passes |
| 26 | Environment variable override works when config.toml has correct structure | VERIFIED | `SANELESS_PROFILES__DEFAULT__TITLE` sets `default_title_template`; `test_title_env_var_override` passes; all 22 config tests green |

**Score:** 26/26 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `src/saneless/config.py` | Pydantic Settings with TOML + env var loading; XDG log path; title alias; TOML error UX | VERIFIED | 208 lines; `Field(alias="title")`, `_build_settings` with `ValidationError` interception, `_VALID_SECTIONS` list, XDG log default |
| `src/saneless/exceptions.py` | Custom exception hierarchy | VERIFIED | SanelessError, ConfigError, ScanError, PaperlessError — all inherit correctly |
| `src/saneless/logging_config.py` | configure_logging with RotatingFileHandler + OSError fallback | VERIFIED | `try/except OSError` wraps mkdir + file handler; fallback attaches `StreamHandler(sys.stderr)` |
| `src/saneless/scanner/base.py` | ScannerBackend ABC + dataclasses | VERIFIED | ABC with 3 abstract methods; DeviceInfo, DeviceCapabilities, ScanSettings dataclasses |
| `src/saneless/scanner/sane_backend.py` | SaneBackend wrapping python-sane | VERIFIED | Lazy import, init-once, context-managed device, source validation |
| `src/saneless/scanner/__init__.py` | Re-exports all scanner symbols | VERIFIED | Lazy SaneBackend `__getattr__` + direct base imports |
| `src/saneless/pdf.py` | PDF assembly via img2pdf | VERIFIED | TemporaryDirectory, img2pdf.convert, MAX_IMAGE_PIXELS guard |
| `src/saneless/paperless.py` | PaperlessClient with retry + polling | VERIFIED | upload_document, poll_task, test_connection, close; httpx.Client with Auth header |
| `src/saneless/pipeline.py` | Pipeline orchestration | VERIFIED | scan_pages -> assemble_pdf -> upload_document -> poll_task; TemporaryDirectory wrapper |
| `src/saneless/job.py` | Job model + SQLite JobStore | VERIFIED | JobState enum, Job dataclass, JobStore with CREATE TABLE, check_same_thread=False |
| `src/saneless/worker.py` | Background worker thread | VERIFIED | daemon thread, queue.Queue(maxsize=10), sentinel shutdown, run_pipeline call |
| `src/saneless/cli.py` | Click CLI with scan + devices | VERIFIED | @click.group(), scan and devices subcommands, all exit codes, --json, --capabilities, -v |
| `tests/test_config.py` | Config tests including TOML error UX and title alias | VERIFIED | 22 tests — all pass; TestTomlStructureErrors class with 4 new tests |
| `tests/test_logging.py` | Logging tests including XDG path and OSError fallback | VERIFIED | 8 tests — all pass |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `config.py` | pydantic-settings | `class Settings(BaseSettings)` | WIRED | Line 84; SettingsConfigDict with SANELESS_ prefix |
| `config.py` | TOML config file | `toml_file` in settings_customise_sources | WIRED | Lines 122-128: TomlConfigSettingsSource injected at runtime |
| `config.py` | `_build_settings` | `load_settings` delegates to `_build_settings` | WIRED | Lines 194, 204, 207: all code paths in load_settings call _build_settings |
| `config.py` | ValidationError interception | `except ValidationError` in `_build_settings` | WIRED | Lines 155-174: catches pydantic ValidationError, checks `extra_forbidden`, raises ConfigError |
| `ProfileConfig` | `title` alias | `Field(default="", alias="title")` + `populate_by_name=True` | WIRED | Line 63: alias wired; line 56: ConfigDict enables both names |
| `logging_config.py` | `config.py` | `log_file` from `settings.output.log_file` | WIRED | `cli.py`: `settings.output.log_file` passed to `configure_logging` |
| `logging_config.py` | OSError fallback | `except OSError` block adds stderr handler | WIRED | Wraps mkdir + RotatingFileHandler; fallback attaches StreamHandler |
| `scanner/sane_backend.py` | `scanner/base.py` | `class SaneBackend(ScannerBackend)` | WIRED | Implements all 3 abstract methods |
| `scanner/sane_backend.py` | python-sane | `sane.init()` in `__init__` | WIRED | Lazy import sentinel + `_ensure_sane()`; `sane.init()` in `__init__` |
| `pdf.py` | img2pdf | `img2pdf.convert` | WIRED | `pdf_bytes = img2pdf.convert(image_paths)` |
| `paperless.py` | httpx | `httpx.Client` | WIRED | `self._client = httpx.Client(...)` |
| `paperless.py` | paperless-ngx API | `/api/documents/post_document/` + `/api/tasks/` | WIRED | POST and GET endpoints with correct paths |
| `cli.py` | `config.py` | `load_settings()` in group callback | WIRED | `settings = load_settings(config_path)` |
| `cli.py` | `pipeline.py` | scan command calls `run_pipeline()` | WIRED | `run_pipeline(scanner, paperless, settings, ...)` |
| `pipeline.py` | `scanner/base.py` | calls `scanner_backend.scan_pages()` | WIRED | `images = list(scanner.scan_pages(device_id, scan_settings))` |
| `pipeline.py` | `pdf.py` | calls `assemble_pdf()` | WIRED | `pdf_path = assemble_pdf(images, tmp_path)` |
| `pipeline.py` | `paperless.py` | calls `paperless_client.upload_document()` | WIRED | `task_uuid = paperless.upload_document(...)` |
| `__init__.py` | `cli.py` | `main()` calls `cli()` | WIRED | `from .cli import cli; cli()` |

### Requirements Coverage

Plan 01-05 declares requirements: CONF-01, CONF-02, CONF-03.

ROADMAP.md Phase 1 requires: SCAN-01, SCAN-02, SCAN-03, PROF-01, PROF-02, PDF-01, PDF-02, PLSS-01, PLSS-02, PLSS-03, CONF-01, CONF-02, CONF-03, ARCH-01, ARCH-02, ARCH-03, LOG-01, LOG-02, LOG-04, CLI-01, CLI-02.

REQUIREMENTS.md traceability maps CONF-01, CONF-02, CONF-03 to Phase 1 with status Complete.

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|----------|
| CONF-01 | 01-01, 01-05 | Config via pydantic-settings with TOML + env var overrides | SATISFIED | Settings(BaseSettings) with SANELESS_ prefix; TomlConfigSettingsSource; title alias via Field(alias="title"); all 22 config tests pass |
| CONF-02 | 01-01, 01-05 | Scanner host, device, paperless URL/token, tmp_dir, log settings, profiles all configurable | SATISFIED | ScannerConfig, PaperlessConfig, OutputConfig, ProfileConfig all defined with correct fields; title alias enables shorter TOML syntax |
| CONF-03 | 01-01, 01-05 | No hardcoded credentials | SATISFIED | token: str = "" default; must come from config or SANELESS_PAPERLESS__TOKEN |
| PROF-01 | 01-01 | Scan profiles in TOML with source, resolution, mode, optional metadata | SATISFIED | ProfileConfig with source, resolution, mode, default_tags, default_correspondent, default_title_template (alias: title) |
| PROF-02 | 01-01 | Default profile must always be present | SATISFIED | field_validator raises ValueError if "default" not in profiles dict |
| LOG-01 | 01-01, 01-04 | Rotating log file at configurable path | SATISFIED | RotatingFileHandler; XDG default; OSError fallback to stderr; 8 logging tests pass |
| LOG-02 | 01-01, 01-04 | Log level configurable | SATISFIED | `log_level: str = "INFO"` in OutputConfig; passed to configure_logging |
| LOG-04 | 01-02 | Temp files cleaned up on error | SATISFIED | TemporaryDirectory context manager guarantees cleanup on all paths |
| SCAN-01 | 01-02 | User can discover available SANE devices | SATISFIED | SaneBackend.get_devices() + CLI devices command |
| SCAN-02 | 01-02 | User can pin target device by name in config | SATISFIED | settings.scanner.device used by pipeline |
| SCAN-03 | 01-02 | Flatbed single-page scan | SATISFIED | scan_pages() with source="Flatbed" from ProfileConfig default |
| ARCH-01 | 01-02 | Scanner behind abstraction layer | SATISFIED | ScannerBackend ABC; SaneBackend implements it |
| ARCH-03 | 01-02 | SANE initialized once at startup | SATISFIED | sane.init() in SaneBackend.__init__ exactly once |
| PDF-01 | 01-02 | PDF assembly via img2pdf (lossless) | SATISFIED | img2pdf.convert with PNG temp files |
| PDF-02 | 01-02 | Temp files in configurable tmp_dir, cleaned up | SATISFIED | TemporaryDirectory in assemble_pdf and run_pipeline |
| PLSS-01 | 01-02 | Upload to paperless-ngx with metadata | SATISFIED | upload_document multipart form with all metadata fields |
| PLSS-02 | 01-02 | Poll task endpoint until terminal state | SATISFIED | poll_task() with exponential backoff |
| PLSS-03 | 01-02 | Test connection distinguishing 3 failure modes | SATISFIED | test_connection() returns "connected", "token_rejected", "unreachable" |
| ARCH-02 | 01-03 | Background worker thread with queue.Queue | SATISFIED | ScanWorker: daemon thread + Queue(maxsize=10) + sentinel shutdown |
| CLI-01 | 01-03 | `saneless scan [--profile] [--title]` triggers scan job | SATISFIED | scan command with --profile (default="default"), --title (required) |
| CLI-02 | 01-03 | `saneless devices` lists available SANE devices | SATISFIED | devices command with table, --json, and --capabilities output modes |

**All 21 requirement IDs from plans accounted for. CONF-01, CONF-02, CONF-03 re-verified after gap closure in plan 01-05. No orphaned requirements for Phase 1 in REQUIREMENTS.md.**

### Anti-Patterns Found

Scan of files modified by plan 01-05 (config.py, tests/test_config.py):

- No TODO/FIXME/XXX/HACK/PLACEHOLDER comments
- No stub return patterns (return null, empty dict, empty list)
- No empty handlers
- No hardcoded credentials

Two pre-existing `# noqa: ARG003` comments in config.py at lines 109-110 are legitimate: pydantic-settings requires those parameters in the `settings_customise_sources` signature even though they are unused. Not introduced by plan 01-05.

One pre-existing `# type: ignore[call-arg]` at line 153 is necessary: `Settings(_toml_file=...)` passes an internal init kwarg that the type checker cannot see in the public signature. This was present before plan 01-05 and cannot be removed without breaking functionality or suppressing a correct type error in a worse way.

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `config.py` | 109-110 | `# noqa: ARG003` | Info | Pre-existing; required by pydantic-settings API signature |
| `config.py` | 153 | `# type: ignore[call-arg]` | Info | Pre-existing; required by pydantic-settings internal kwarg API |

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

No gaps found. All 26 truths verified (23 from previous + 3 new from gap closure plan 01-05).

**Gap closure confirmed:** Plan 01-05 successfully addressed the two UAT issues (tests 4 and 5):

1. **TOML structure error UX (UAT test 4):** `[default]` section now raises `ConfigError("Unknown config section 'default'. Valid top-level sections: scanner, paperless, output, profiles. Did you mean [profiles.default]?")` — implemented via `_build_settings` helper that intercepts `ValidationError` with `extra_forbidden` type errors.

2. **Title alias (UAT test 5 prerequisite):** `title` accepted as alias for `default_title_template` on `ProfileConfig` via `Field(default="", alias="title")` with `ConfigDict(populate_by_name=True)`. Both `title` and `default_title_template` work in TOML and Python code.

3. **Env var override with correct TOML (UAT test 5):** `SANELESS_PROFILES__DEFAULT__TITLE` sets `default_title_template` to the env var value. Verified by `test_title_env_var_override`.

Commits verified to exist: `feb98e4` (test/RED) and `9e1e6e6` (feat/GREEN).

Full test suite: 225 passed in 24.38s — no regressions.

---

_Verified: 2026-03-21T15:30:00Z_
_Verifier: Claude (gsd-verifier)_
