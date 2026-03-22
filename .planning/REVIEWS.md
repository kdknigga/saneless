# Cross-AI Plan Reviews

**Generated:** 2026-03-22
**Reviewer:** Gemini CLI (Google Gemini, `@google/gemini-cli` v0.34.0)
**Scope:** All 12 phases, 30 plans

---

## Phase 1: Core Pipeline (Plans 01-01 to 01-05)

**Summary**
Phase 1 successfully establishes the foundational pipeline, integrating configuration, SANE scanner abstraction, PDF assembly, and Paperless-ngx upload. The bottom-up approach is methodically executed, ensuring each component is independently testable before being integrated into the CLI. The focus on early validation of configurations, XDG compliance, and graceful degradation sets a strong baseline for the application.

**Strengths**
- **Robust Abstraction:** The `ScannerBackend` ABC effectively insulates the application from `python-sane` quirks (like fd leaks and segfaults) and enables easy testing via mocks.
- **Fail-Fast Configuration:** `pydantic-settings` ensures malformed TOML or missing default profiles are caught immediately at startup, preventing mid-scan crashes.
- **Network Resilience:** The Paperless-ngx client correctly implements exponential backoff for network issues and includes a fallback to a local consume directory.
- **Resource Cleanup:** Strict use of `tempfile.TemporaryDirectory` within context managers ensures leftover image files are cleaned up on both success and error paths.

**Concerns**
- **HIGH — Disk space exhaustion in `/tmp`:** The pipeline converts scanned PIL Images to PNGs in the temporary directory before `img2pdf` assembly. A 600 DPI A4 page is ~35MB as a lossless PNG. A 50-page scan will consume ~1.75GB of disk space. If `/tmp` is mounted as a small `tmpfs` (common in Linux/Docker environments), this will quickly trigger out-of-space errors.
- **LOW — Silent logging fallback:** Falling back to `stderr` when the log directory is unwritable is good for CLI UX, but in a background daemon or containerized environment, `stderr` might not be permanently captured, obscuring critical failures.

**Suggestions**
- Add a disk space check before starting large multi-page scans, or optionally compress intermediate files using JPEG/TIFF with appropriate quality settings if lossless PNG is not strictly required.
- Validate that `OutputConfig.tmp_dir` and `PaperlessConfig.consume_dir` are writable during configuration loading (fail-fast) rather than discovering permission issues mid-scan.

**Risk Assessment:** **MEDIUM**
The core logic is sound, but the memory and disk footprint of lossless high-DPI image processing poses a significant operational risk in constrained environments like Raspberry Pis or small Docker containers.

---

## Phase 2: ADF and Multi-Page (Plans 02-01 to 02-03)

**Summary**
Phase 2 tackles the most complex hardware interactions: ADF scanning, hardware duplexing, and manual duplex interleaving. The plans demonstrate an excellent grasp of scanner hardware pitfalls, particularly by implementing per-page timeouts and inline image validation to defend against driver lockups. The dual-threshold empty page detection and base64 thumbnail generation are well-integrated into the orchestration pipeline.

**Strengths**
- **Defensive SANE Handling:** Wrapping the unreliable `multi_scan()` iterator in a `ThreadPoolExecutor` with a timeout protects the main pipeline from deadlocking scanner drivers.
- **Inline Validation:** Validating raw page bytes and dimensions inline defends against known hardware bugs (e.g., HP scanners feeding N+1 corrupt empty pages).
- **Graceful Orchestration:** Using `threading.Event` to pause the worker thread for manual duplex flipping is an elegant, low-complexity coordination solution.

**Concerns**
- **HIGH — Catastrophic data loss on manual duplex mismatch:** If a user scans 25 fronts and 24 backs (due to a misfeed), the pipeline raises a `ScanError` and drops the entire job. The user permanently loses the 49 successfully scanned pages and must start completely over.
- **MEDIUM — Orphaned Timeout Threads:** If a scanner C-extension call blocks indefinitely, `future.result(timeout=...)` correctly raises an error, but the thread executing `next(iterator)` remains stuck forever. While `executor.shutdown(wait=False)` prevents immediate pipeline blocking, accumulating deadlocked threads over a long uptime could exhaust OS resources.
- **LOW — No explicit bypass for empty page detection:** Relying purely on adjusting statistical thresholds is clever, but users scanning very faint documents (like pencil receipts) might prefer a simple boolean toggle to disable blank detection entirely.

**Suggestions**
- For manual duplex count mismatches, save the "fronts" to the fallback consume directory or assemble them into a partial PDF rather than destroying the data entirely.
- Add an `enable_empty_page_detection` boolean to `ProfileConfig` to allow explicit bypassing without tweaking float thresholds.
- Set the `ThreadPoolExecutor` to run as daemon threads to ensure deadlocked C-extensions don't prevent application teardown.

**Risk Assessment:** **HIGH**
Hardware edge cases and driver deadlocks are notoriously difficult to handle. The manual duplex data-loss scenario is a severe usability risk if a misfeed occurs.

---

## Phase 3: Web UI (Plans 03-00 to 03-03)

**Summary**
Phase 3 translates the core backend into a functional, single-page web application using FastAPI, HTMX, and PicoCSS. The architectural decision to use server-side rendering with HTMX is perfectly suited for this lightweight appliance. The plans show excellent attention to UX, including conditional 1-second polling, graceful degradation when Paperless is offline, and clear visual feedback for the manual duplex flip.

**Strengths**
- **Zero-Build Frontend:** Relying on HTMX and PicoCSS via CDNs eliminates JavaScript toolchains and keeps the stack exceptionally lean.
- **Graceful Degradation:** The web UI loads successfully even if Paperless-ngx is unreachable, returning empty lists for tags/correspondents rather than throwing 500 errors.
- **Efficient Polling:** Conditionally rendering the `hx-trigger` only during active job states prevents unnecessary network spam when the system is idle.
- **Caching Layer:** The TTL-based `MetadataCache` prevents hammering the Paperless API on every page load while still allowing granular manual invalidation.

**Concerns**
- **HIGH — SQLite Concurrency without WAL mode:** The worker thread writes to the database while the web thread reads from it (polling every 1 second). Unless SQLite is explicitly configured with `PRAGMA journal_mode=WAL;`, the frequent concurrent reads and writes will inevitably collide, causing `database is locked` operational errors.
- **MEDIUM — Job History Pruning is Startup-Only:** The `job_store.prune()` method is only called within the FastAPI `lifespan` startup block. If the application is run continuously as a daemon, the job history table will grow indefinitely beyond the `max_rows` limit until the next restart.
- **LOW — Brittle Manual Duplex Detection:** The `_is_manual_duplex` logic checks if the user-defined `source` string contains "manual" and "duplex". This is tied to English string matching and could easily break if a user names their profile differently.

**Suggestions**
- Explicitly execute `PRAGMA journal_mode=WAL;` and `PRAGMA synchronous=NORMAL;` upon opening the SQLite connection in `JobStore` to safely support high-concurrency read/write access.
- Piggyback on the `POST /api/scan` endpoint to periodically trigger `job_store.prune()` during runtime, or use an `asyncio.create_task` loop inside the lifespan.
- Add a dedicated boolean in `ProfileConfig` (e.g., `manual_duplex: bool = False`) to control two-pass routing rather than performing substring matching on the hardware `source` field.

**Risk Assessment:** **MEDIUM**
The web architecture is solid and highly efficient, but the SQLite concurrency issue and the lack of runtime database pruning need addressing to ensure long-term stability in an always-on deployment.

---

## Phase 4: Packaging and Deployment

### Plan 04-01: CLI `jobs` command and consume directory fallback

**Summary:** Implements the `saneless jobs` CLI command for viewing local scan history and hardens the paperless-ngx upload fallback by automatically creating the consume directory if it does not exist.

**Strengths**
- **Strong TDD Approach:** Requires comprehensive test coverage of the CLI command and DB state before implementation.
- **Safe Filesystem Operations:** Using `mkdir(parents=True, exist_ok=True)` gracefully handles nested directories and race conditions.
- **Informational CLI Design:** The command correctly returns a `0` exit code, as it is a read-only informational command.

**Concerns**
- **MEDIUM — Hardcoded Display Widths:** The plan dictates hardcoded widths for the table columns. This will break visually on narrower terminals or waste space on wider ones.
- **LOW — Missing DB Error Handling:** The plan does not specify error handling for SQLite connection/permission errors.

**Suggestions**
- Implement terminal-aware dynamic width calculation using `shutil.get_terminal_size()` instead of hardcoded string formatting.
- Add a `try/except` block around the `JobStore` initialization to catch `sqlite3.OperationalError` and exit gracefully.

**Risk Assessment:** **LOW.** The scope is contained, and the fallback logic is standard and safe.

### Plan 04-02: PyPI metadata, Dockerfile, Docker Compose, GitHub Actions

**Summary:** Establishes the project's distribution infrastructure, including complete PyPI metadata, a multi-stage Dockerfile, a docker-compose.yml example, and a GitHub Actions CI/CD pipeline.

**Strengths**
- **Lean Containerization:** The two-stage Docker build ensures the final OCI image is small and only contains runtime dependencies.
- **Secure Publishing:** Utilizing OIDC trusted publishing for PyPI and the native `GITHUB_TOKEN` for GHCR avoids long-lived static secrets.
- **Config Alignment:** Correctly identifies and binds the application's actual default port (`8080`).

**Concerns**
- **HIGH — Docker Compose Volume Trap:** Mounting `./config.toml:/etc/saneless/config.toml:ro` is dangerous. If `./config.toml` does not exist on the host before running `docker compose up`, Docker daemon will automatically create it as an empty *directory*.
- **LOW — CI Python Version:** The test job does not explicitly request a Python 3.14 environment.

**Suggestions**
- Mount the containing directory instead of the file, or add documentation warning users to create `config.toml` before starting the container.
- Add an explicit `python-version: "3.14"` step to the GitHub Actions test job.

**Risk Assessment:** **MEDIUM.** The volume mounting issue is a common pitfall that ruins the first-time user experience.

---

## Phase 5: Web Server Launch Command

### Plan 05-01: `saneless serve` CLI command

**Summary:** Connects the existing FastAPI application factory to the CLI via a new `serve` command and updates the Dockerfile to use this command as the default container entrypoint.

**Strengths**
- **Elegant Logging Integration:** Passing `log_config=None` to `uvicorn.run()` correctly prevents Uvicorn from overriding the application's pre-configured root logger.
- **DRY Execution:** Correctly recognizes that `configure_logging()` is already executed by the CLI group callback.

**Concerns**
- **LOW — Port Conflict Handling:** The plan does not explicitly outline handling `OSError` if the requested port is already in use.

**Suggestions**
- Wrap `uvicorn.run()` in a `try/except` targeting `OSError` to catch "Address already in use" errors with a clean CLI error.

**Risk Assessment:** **LOW.** The integration is straightforward and relies on previously validated components.

---

## Phase 6: Gap Closure Fixes

### Plan 06-01: Paperless test route and worker state transitions

**Summary:** Closes final V1 gaps by adding a web route to expose the paperless-ngx connection test and updating the background worker to emit intermediate `ASSEMBLING` and `UPLOADING` states.

**Strengths**
- **Code Reuse:** Directly utilizes the existing `test_connection()` method rather than duplicating network logic.
- **Graceful API Failures:** Catching all unexpected exceptions in the route and returning a structured 502 JSON response prevents unhandled 500 HTML pages.

**Concerns**
- **MEDIUM — Fragile State Coupling:** The worker's state transitions rely on exact string matching against pipeline log messages (e.g., `elif msg == "Assembling PDF..."`). Modifying a log message for grammar or localization will silently break UI state tracking.
- **LOW — Exception Leakage:** Returning `str(exc)` in the 502 response could potentially leak sensitive information (tokens, internal IPs).

**Suggestions**
- Decouple the state machine from logging. Modify the pipeline's `status_callback` to accept a structured event enum alongside the human-readable string.
- Sanitize exception strings in the 502 response.

**Risk Assessment:** **MEDIUM.** The string-matching architecture for state transitions is technical debt that will cause regressions during refactoring.

### Plan 06-02: Fix test_connection auth validation

**Summary:** Corrects a logic flaw by targeting an authenticated endpoint (`/api/tags/`) instead of the unauthenticated API root.

**Strengths**
- **Root Cause Accuracy:** Correctly identifies that the DRF browsable API root does not enforce authentication.
- **Performance Optimization:** Enforcing `page_size=1` ensures minimal payload.

**Concerns**
- **LOW — Endpoint Scope:** Highly restricted API tokens lacking tag read access will fail despite being valid for documents.

**Risk Assessment:** **LOW.** Highly surgical, well-targeted bug fix.

---

## Phase 7: Tech Debt Cleanup

### Plan 07-01: Backend Hardening

**Summary:** Hardens the backend by formalizing `python-sane` as an optional dependency, introducing an `ErrorCategory` enum for typed exception handling, and fixing a flip-timing race condition using `threading.Event`.

**Strengths**
- **Robust Exception Handling:** Explicitly catches subclass exceptions before parent classes, ensuring correct error categorization.
- **Proper Synchronization:** Using `threading.Event` instead of a polling loop for flip timing is the correct pattern.
- **Safe Database Migration:** Catching `sqlite3.OperationalError` during `ALTER TABLE` handles schema evolution pragmatically.

**Concerns**
- **MEDIUM:** The `wait_transition` timeout is hardcoded to `2.0` seconds. Slow network scanners may exceed this, causing the UI to receive stale state.

**Suggestions**
- Move the `2.0` second timeout to configuration or a named constant.

**Risk Assessment:** **LOW.** Surgically targeted changes backed by solid test plans.

### Plan 07-02: Playwright Browser Tests

**Summary:** Introduces Playwright browser tests using a session-scoped Uvicorn server to verify PicoCSS rendering, HTMX polling, and UI interactions.

**Strengths**
- **Performance Optimization:** Session-scoped server with function-scoped pages drastically reduces test execution time.
- **Lifespan Safety:** Implements concrete `_BrowserTestScanner` stub rather than `MagicMock`, correctly satisfying FastAPI lifespan events.

**Concerns**
- **HIGH:** Running Uvicorn via `threading.Thread(target=server.run)` is dangerous — Uvicorn attempts to install signal handlers that can only run in the main thread, causing crashes or hangs during teardown.
- **LOW:** Relying on `time.sleep(0.05)` for server startup can cause flaky tests in slow CI.

**Suggestions**
- Configure Uvicorn to disable signal handling, or use `server.serve()` wrapped in an asyncio task.
- Use Playwright's built-in `expect()` auto-retrying assertions instead of synchronous `assert` statements.

**Risk Assessment:** **MEDIUM.** Threaded Uvicorn servers in test suites are prone to port-binding race conditions and teardown hangs.

---

## Phase 8: Audit Lint & Type Checker Ignores

### Plan 08-01: Audit Suppressions

**Summary:** Systematically removes or documents all `# noqa` and `# type: ignore` comments, fixes a specific `httpx` multipart type error, and tightens `per-file-ignores` in `pyproject.toml`.

**Strengths**
- **Zero-Tolerance Quality Bar:** Enforces strict justifications for remaining suppressions.
- **Targeted Exemption Removal:** Auditing `per-file-ignores` individually based on actual violation counts is data-driven.

**Concerns**
- **LOW:** Relying on `httpx._types.FileTypes` uses a private API that could change in future releases.

**Suggestions**
- Ensure private API import is wrapped in `if TYPE_CHECKING:` block to limit breakage to static analysis only.

**Risk Assessment:** **LOW.** Changes are strictly confined to static analysis configuration.

---

## Phase 9: Enable Pytest Strict Mode

### Plan 09-01: Fix Type Checker Errors in Tests

**Summary:** Eliminates type checking exclusions for `tests/` by replacing untyped `MagicMock` instances with concrete stubs.

**Strengths**
- **Type-Safe Testing:** Protocol-compliant fake classes improve test suite reliability.
- **Idiomatic Type Narrowing:** Using `assert fetched is not None` is the Pythonic approach.

**Concerns**
- **LOW:** Using `cls: type = ScannerBackend` indirection slightly obfuscates test intent.

**Risk Assessment:** **LOW.** Safe refactoring of test utilities.

### Plan 09-02: Annotations, Docstrings, and Lazy Imports

**Summary:** Massive mechanical sweep applying type annotations, docstrings, and moving 77 lazy imports to top-level across 14 test files.

**Strengths**
- **Runtime Optimization:** Uses `from __future__ import annotations` and `TYPE_CHECKING` blocks to avoid slowing test collection.
- **Clean Escape Hatch:** `object.__setattr__` bypasses strict type checkers for dynamic mock patching.

**Concerns**
- **HIGH — Import-order risk:** Moving 77 lazy imports to top-level simultaneously is high-risk. If any were intentionally lazy to prevent C-extension loading or side-effects before `monkeypatch`, this will break test isolation.
- **MEDIUM — Merge conflict risk:** Modifying ~211 functions in a single sweep is highly prone to errors.

**Suggestions**
- Scrutinize imports in `tests/test_scanner.py` and `tests/test_cli.py` — ensure moving `from saneless import cli` doesn't initialize Click/SANE globally.

**Risk Assessment:** **HIGH.** Global mechanical refactoring combined with import-order manipulation often introduces subtle side-effects.

### Plan 09-03: Pytest Strict Config & SQLite ResourceWarnings

**Summary:** Enables pytest strict mode (`filterwarnings=["error"]`) and fixes `ResourceWarning` leaks from unclosed SQLite connections.

**Strengths**
- **Resource Leak Prevention:** Catches unclosed DB connections that cause file-locking issues.
- **Strict Quality Gates:** Enforcing `xfail_strict` and strict markers prevents silent degradation.

**Concerns**
- **MEDIUM:** Global `filterwarnings=["error"]` without allowlisting is aggressive. Third-party deprecation warnings will break the test suite.

**Suggestions**
- Use `yield` fixtures for `JobStore` cleanup.
- Be prepared to add targeted `filterwarnings` exceptions for third-party libraries.

**Risk Assessment:** **MEDIUM.** Strict warning enforcement creates pristine code but brittle CI when dependencies update.

---

## Phase 10: Automatic Scanner Profile Creation

### Plan 10-01: Core auto_profiles module

**Summary:** Creates pure functions mapping scanner capabilities to configuration profiles, using `tomlkit` for comment-preserving TOML persistence.

**Strengths**
- **Pure Functions & TDD:** Decoupling generation logic from I/O makes core logic highly testable.
- **User-Centric Persistence:** `tomlkit` preserves user comments in config files.
- **Robust Mapping:** Case-insensitive, keyword-based mapping for `source_to_slug` accounts for vendor discrepancies.

**Concerns**
- **LOW:** No file locking for concurrent writes.

**Suggestions**
- Consider atomic write pattern (write to temp file, then `os.replace`) to prevent TOML corruption on interruption.

**Risk Assessment:** **LOW.** Isolated, well-tested pure functions.

### Plan 10-02: CLI command and worker lazy trigger

**Summary:** Integrates profile generation via `saneless auto-profiles` CLI and lazy worker trigger.

**Strengths**
- **Graceful Degradation:** Falls back to bare default profile if scanner is unreachable.
- **Idempotency:** `_auto_generated` flag prevents infinite loops on subsequent scans.
- **Non-Destructive CLI:** `--force` flag protects against accidental profile overwrites.

**Concerns**
- **MEDIUM:** Broad `Exception` catch in `_maybe_auto_generate` will mislead on `PermissionError` vs scanner unreachable.

**Suggestions**
- Split `try/except`: catch `Exception` for scanner checks, handle `OSError`/`PermissionError` separately for file writes.

**Risk Assessment:** **MEDIUM.** Background config modification can introduce hard-to-debug edge cases.

---

## Phase 11: Review and Adjust Default DPI Setting

### Plan 11-01: Consolidate DEFAULT_RESOLUTION constant

**Summary:** Consolidates default DPI into a single `DEFAULT_RESOLUTION` constant, validated at 300 DPI for Tesseract OCR.

**Strengths**
- **Research-Backed:** 300 DPI validated against OCR requirements and archival standards.
- **Clean Architecture:** Centralized constant prevents drift.
- **Backwards Compatible:** Does not modify existing user configurations.

**Concerns**
- **LOW:** None.

**Suggestions**
- Add a comment in `saneless.toml.example` mentioning 300 DPI is recommended minimum for OCR.

**Risk Assessment:** **LOW.** Focused, minimal-risk refactoring.

---

## Phase 12: UI Polish

### Plan 12-01: Web UI polish (humanize, ARIA, external JS, CSS spacing)

**Summary:** Humanizes enum labels, adds ARIA labels, replaces inline scripts with external `app.js`, normalizes CSS to PicoCSS design tokens.

**Strengths**
- **Accessibility:** `aria-label` and `sr-only` spans greatly improve screen reader compatibility.
- **Security:** Removing inline `<script>` blocks enables strict CSP.
- **Testing Rigor:** Includes static analysis test for CSS `!important` regression.

**Concerns**
- **LOW:** `document.getElementById("scan-btn")` will fail if multiple scan buttons exist.
- **LOW:** No cache-busting strategy for `/static/app.js`.

**Suggestions**
- Consider appending a version query parameter to the script tag (e.g., `app.js?v=1.0`).

**Risk Assessment:** **LOW.** Cosmetic and structural changes with no backend impact.

### Plan 12-02: CLI table truncation

**Summary:** Introduces `_truncate` helper with terminal-aware column widths via `shutil.get_terminal_size()`.

**Strengths**
- **Dynamic Sizing:** Adapts to terminal width correctly.
- **Clean Fallbacks:** `max(15, cols - 50)` prevents column collapse.
- **Isolated Helper:** Simple, testable, reusable.

**Concerns**
- **LOW:** `_truncate` with `width=0` would produce incorrect output (`value[:-1] + "..."`).

**Suggestions**
- Add edge case handling for `width <= 0` and `width == 1`.

**Risk Assessment:** **LOW.** Clean formatting fix with appropriate unit tests.

---

## Summary Table

| Phase | Risk | HIGH Concerns | Key Issue |
|-------|------|---------------|-----------|
| 1. Core Pipeline | MEDIUM | 1 | Disk space exhaustion on `/tmp` with high-DPI scans |
| 2. ADF and Multi-Page | **HIGH** | 1 | Data loss on manual duplex page count mismatch |
| 3. Web UI | MEDIUM | 1 | SQLite concurrency without WAL mode |
| 4. Packaging | MEDIUM | 1 | Docker Compose volume trap (file vs directory) |
| 5. Web Server Launch | LOW | 0 | — |
| 6. Gap Closure | MEDIUM | 0 | Fragile string-matching state machine |
| 7. Tech Debt | MEDIUM | 1 | Uvicorn signal handler crash in threaded tests |
| 8. Audit Suppressions | LOW | 0 | — |
| 9. Pytest Strict | **HIGH** | 1 | Mass import reordering risk in test files |
| 10. Auto Profiles | MEDIUM | 0 | Misleading error messages in background worker |
| 11. Default DPI | LOW | 0 | — |
| 12. UI Polish | LOW | 0 | — |

**Overall Assessment:** The project demonstrates exceptionally thorough planning with strong TDD practices and defensive error handling throughout. The two HIGH-risk areas (Phase 2 manual duplex data loss, Phase 9 mass import reordering) are the most actionable items for follow-up. The MEDIUM-risk items around SQLite WAL mode and Docker Compose volumes are common operational pitfalls worth addressing proactively.
