# Requirements: saneless

**Defined:** 2026-09-09
**Milestone:** v2.0 Prep for release
**Core Value:** A user can walk up to the web UI, click Scan, and have a correctly assembled PDF land in paperless-ngx with metadata -- without touching any other tool.

Every requirement below resolves one or more findings from `.planning/reviews/2026-09-09-code-review.md`. Finding IDs (C-, M-, N-, U-, and "doc row N" for section 8) are given in brackets so a reader can trace back to the evidence and the suggested fix. v1.0 requirements (all validated) are archived at `.planning/milestones/v1.0-REQUIREMENTS.md`.

## v2.0 Requirements

### CI Gate

- [x] **CI-01**: Every push and pull request runs ruff check, ruff format --check, ty, pyrefly, and the non-browser pytest suite in GitHub Actions, and a red run blocks merge [M-25]
- [ ] **CI-02**: CI fails if any shipped file references `kris-knigga/saneless`, `kris-knigga.github.io/saneless`, or `ghcr.io/kris-knigga/saneless` (grep guard, excluding `.planning/` and `site/`) [M-27]

### Contracts and Vocabulary

- [x] **CTR-01**: There is exactly one `JobState` enum, one active-state list, and one state-to-label map, shared by the worker, web templates, and CLI; `job.py` re-exports them so existing imports keep working [M-05]
- [x] **CTR-02**: The pipeline returns a typed `ScanResult` (outcome enum SUCCESS/FALLBACK/~~FAILED~~, pages scanned, pages removed as blank, pages uploaded, warning text) instead of a value nobody reads [C-03, N-38]

  **Closed 2026-09-11 with one member fewer than the wording names.** Phase 21 satisfied the *shape* (D-07) and deferred the `FAILED` question here; Phase 23's **D-01 decided against it**. Failures raise rather than return, so a returned `FAILED` is unreachable, and writing one from the worker's `except` branch would encode the same fact as `JobState.ERROR` in a second column with two chances to disagree. Everything else this requirement asks for shipped: `pipeline.ScanResult` carries `outcome`, `pages_scanned`, `pages_removed`, `pages_uploaded` and `warning`, and `worker.py` reads all five — the "value nobody reads" is gone, which is what C-03 and N-38 were about.
- [x] **CTR-03**: `upload_document` returns a typed `UploadResult`; the `"fallback"` magic string and every reader of it in `src/`, `tests/`, and `docs/` are deleted [C-03, N-38]
- [x] **CTR-04**: A single `classify_source()` in `scanner/base.py` returns a `SourceKind` (FLATBED, FEEDER, FEEDER_DUPLEX, AUTO, UNKNOWN) for any SANE source string, including "Automatic Document Feeder", "ADF Front", "ADF Duplex", and vendor variants, and is the only classification rule in the codebase [C-06, N-09]
- [x] **CTR-05**: `ErrorCategory` lives with `JobState` and is the input to a single user-message map; no template, route, or CLI output classifies errors by string matching [N-14, U-05]

### Job Store

- [x] **STOR-01**: Two threads can call any mix of `JobStore` methods concurrently for 200 rounds without an exception (an `RLock` around every public method; public methods never call other public methods) [C-07]
- [x] **STOR-02**: Schema changes use a `PRAGMA user_version` migration ladder; a v1.0 database file opens and migrates cleanly; the bare `ALTER TABLE ... except: pass` is gone [N-13]
- [x] **STOR-03**: The job table carries `outcome`, `pages_scanned`, `pages_removed`, `pages_uploaded`, `warning`, and `owner_token` columns, added in one migration so later phases never add columns ad hoc [C-03, U-02, U-06]
- [x] **STOR-04**: The row-to-`Job` mapping and column list exist once in `job.py`, and `prune()` reports its count from a single statement [N-17]
- [x] **STOR-05**: `JobStore` exposes `fail_active_jobs()` (marks every non-terminal job FAILED with a "server restarted" reason) and `list_pending()` ordered by creation time [M-03, U-06]

### Honest Outcomes and Never Lose a Scan

- [x] **OUTC-01**: A Paperless task that ends FAILURE or times out raises a `PaperlessError` (or times out as `PaperlessTimeoutError`); the job is recorded FAILED with the Paperless message, never DONE [C-03]
- [x] **OUTC-02**: When the PDF reaches only the consume directory, the job is recorded in the `FALLBACK` state with a warning, and the status partial, history table, and CLI `jobs` output all render it distinctly from DONE [C-03, doc row 3, doc row 4]
- [x] **OUTC-03**: A manual-duplex front/back count mismatch is recorded with a warning on the job, not a silent DONE [C-03]
- [x] **OUTC-04**: When upload and consume-directory fallback both fail, the assembled PDF is moved to `<data_dir>/failed/` with a unique name, the job's error names that path, and no page images or PDF are deleted on that path; the preservation guard spans both `upload_document` and `poll_task` [C-04]
- [x] **OUTC-05**: Every assembled PDF has a unique file name (timestamp plus job id plus sanitised title), and the consume-directory copy is written to a `.part` file and renamed atomically [C-05]
- [x] **OUTC-06**: Assembled PDFs declare the scan DPI so an A4 page scanned at 300 DPI has an A4 MediaBox (595 x 842 pt), via img2pdf's fixed-DPI layout function [M-06]
- [x] **OUTC-07**: `poll_task` raises on any non-200 poll response and computes its deadline from a monotonic clock that includes request time, so the documented timeout is honoured [M-22]
- [x] **OUTC-08**: `test_connection` reports "connected" only for a 2xx response; 404 and 5xx are reported as distinct failure modes [N-12]
- [x] **OUTC-09**: The job database and the `failed/` directory live under a new `output.data_dir` setting (XDG state by default, `saneless-data` volume in Docker), never under the disposable `tmp_dir` [N-39, U-08]
- [x] **OUTC-10**: A parametrised end-to-end test drives the real worker and pipeline with a stub scanner through SUCCESS, Paperless FAILURE, TIMEOUT, consume-dir fallback, and duplex mismatch, asserting the persisted state, outcome, page counts, and file preservation for each [C-03, M-33]
- [x] **OUTC-11**: `PaperlessClient` pins the paperless-ngx API version with an explicit `Accept` header and parses both the v9 and v10 `/api/tasks/` response shapes — paginated `{"count","results":[…]}` as well as a bare list, lowercase as well as uppercase status values, and the failure message from `result_data["error_message"]` as well as `result` [Phase 23 research, 2026-09-11]

  **Why this exists.** `poll_task` (`src/saneless/paperless.py:210-248`) was written against API v9. Current paperless-ngx serves **v10 by default when no version header is sent**, and `PaperlessClient.__init__` sends only `Authorization` — so `isinstance(tasks, list)` is `False`, no terminal status is ever observed, and every poll burns the full timeout. This is invisible today only because `pipeline.py:521-527` discards `poll_task`'s return value and records `SUCCESS` regardless. OUTC-01 and OUTC-07 remove that cover: once timeouts raise and the PDF is preserved on timeout (D-10), **every successful scan would record as FAILED with a stray file in `failed/`** — the exact inversion of this phase's goal. OUTC-01's "records the job FAILED with the Paperless message" also cannot be implemented without resolving which field carries that message.

### Dark Mode and the Commit Gate

Both defects below were discovered *during* Phase 23 execution, not by the 2026-09-09 review, so they carry no `C-`/`M-`/`N-` finding ID. Evidence lives in `.planning/phases/23-honest-outcomes-and-never-lose-a-scan/deferred-items.md` and `.planning/todos/pending/002-dark-mode-never-engages.md`.

- [x] **DARK-01**: The web UI renders PicoCSS v2's dark palette when the operator's OS requests it. `src/saneless/web/templates/base.html:2` sets `data-theme="auto"`, but Pico v2 scopes its automatic dark rule to `:root:not([data-theme])` — the attribute must be **absent**; `"auto"` was the Pico v1 spelling. The shipped CDN build contains exactly one dark media block, selector `:host(:not([data-theme])), :root:not([data-theme])`. Consequence: every user has seen the light palette regardless of OS preference since Phase 12. Proven by a Playwright test asserting a **computed** background colour under `prefers-color-scheme: dark`, not by reading the attribute out of the HTML [Phase 23 plan 23-05]
- [x] **DARK-02**: `.status-done`, `.status-error` and `.status-fallback` each meet WCAG AA (4.5:1) against the surface in **both** colour schemes. `.status-fallback`'s amber-600 (`#a16207`) measures ~3.6:1 on Pico's dark surface today — below the floor — so it needs a lighter amber under dark. This introduces the first dark-scheme-specific override in `app.css`; the convention is recorded in `.planning/UI-SPEC.md`, which currently records its absence [Phase 23 plan 23-05]
- [x] **DARK-03**: Whether `pico.colors.css` is linked is decided and recorded. `pico.min.css` ships the semantic tokens only; the `--pico-color-*` palette lives in a separate file `base.html` does not link, so `.status-fallback`'s `var(--pico-color-amber-600, #a16207)` falls through to the literal hex today. Coordinate with ROBU-09 (Phase 26), which vendors the Pico files — which Pico files exist should be settled once, not twice [Phase 23 plan 23-05]

- [x] **GATE-01**: A TDD RED commit — a test file referencing a symbol that does not exist yet — can be committed without `--no-verify`, without `# type: ignore`, and without disabling any rule. Today `prek` runs `uv run ty check` and `uv run pyrefly check src tests` on **every** commit, and both reject such a file by construction, so RED and GREEN must land together. All eight Phase 23 executors hit this independently and one enumerated every escape it tried and was denied (`--no-verify` forbidden by contract, `# type: ignore` forbidden by CLAUDE.md, `SKIP=ty-checker` denied by the sandbox). `workflow.tdd_mode` is `true` in `.planning/config.json`, so this degrades every remaining phase [Phase 23, all plans]
- [x] **GATE-02**: Whatever mechanism satisfies GATE-01, a deliberate type error still cannot reach master. If the type checkers move off `pre-commit`, the gate that replaces them (pre-push, CI, or both) is proven to reject one. CI-01's GitHub Actions gate remains the enforcing boundary [Phase 23, all plans]
- [x] **GATE-03**: No verification command in `.planning/`, `.pre-commit-config.yaml`, or CI uses bare `uv run pyrefly check`, and the reason is written down where a future plan author will see it: inside a gitignored directory — such as a `.claude/worktrees/` git worktree — pyrefly's `use-ignore-files` default filters out every source file, and the bare invocation can report success having checked nothing. Phase 23 saw it exit 1 in some worktrees and 0 in others; both are wrong. Always name paths: `uv run pyrefly check src tests` [Phase 23 plans 23-01, 23-03, 23-04]

### Scanner Truthfulness

- [ ] **SCNR-01**: `sane_backend`, `auto_profiles`, the pipeline, and the worker all route feeder decisions through `classify_source()`; a device whose feeder is named "Automatic Document Feeder" scans a full stack, and auto-profiles never collapses two distinct feeder sources into one slug [C-06, N-09]
- [x] **SCNR-02**: A first-page SANE error other than the exact "Document feeder out of documents" message is raised as a `ScanError` carrying the SANE message, never reported as "No paper detected" [M-11]
- [x] **SCNR-03**: The scanner backend never drops blank pages on its own; empty-page detection happens only in the pipeline, only when the profile enables it, and manual-duplex page parity is preserved [M-14, doc row 12]
- [x] **SCNR-04**: Geometry is set only when the device reports `tl_x`/`tl_y`/`br_x`/`br_y` options; otherwise the Pillow crop fallback runs and is proven reachable by a test; geometry units are read from the option descriptor rather than assumed to be millimetres [M-15, N-03, doc row 9]
- [x] **SCNR-05**: The crop fallback and page-size maths use the resolution read back from the device after all options are set, and options are set source-first so the source cannot clamp resolution afterwards [M-16]
- [x] **SCNR-06**: `get_capabilities` honours range constraints (min/max/step) on resolution and reports them in `devices --capabilities` [N-01]
- [x] **SCNR-07**: The SANE test doubles mirror python-sane 2.9.2: unknown option names are stored silently, a bad value for a known option raises `_sane.error`, structurally wrong access raises `AttributeError`, and `multi_scan()` cannot raise [M-32]
- [x] **SCNR-08**: An opt-in integration test module drives the real SANE `test` backend through a `SANE_CONFIG_DIR` scoped to `tmp_path` and proves a ten-page stack comes through a long feeder name [M-32]

### Manual Duplex

- [ ] **DPLX-01**: `ProfileConfig` has a `duplex` field (`none`, `hardware`, `manual`); `source` is passed to SANE verbatim and is never overloaded to mean a scanning strategy [C-01, doc row 2]
- [ ] **DPLX-02**: A legacy config with `source = "Manual Duplex"` still loads, is translated to `duplex = "manual"` at config load, and logs a deprecation warning [C-01]
- [ ] **DPLX-03**: The manual-duplex decision is read in exactly one place; the duplicated detection rule in the worker and the `isinstance` dispatch on the two-outcome result are gone [N-07]
- [ ] **DPLX-04**: A `FlipCoordinator` protocol with a timeout is the only way the pipeline waits for a flip; the CLI provides a stdin prompt ("Flip the stack and press Enter"), the web provides the HTMX Continue button, and running manual duplex without a coordinator is refused up front [C-02, M-07, doc row 1]
- [ ] **DPLX-05**: A flip wait that exceeds the timeout fails the job with a clear message and releases the scanner [M-07]
- [ ] **DPLX-06**: During pass B the job is in a visible `SCANNING_REVERSE` state, Abort at the flip prompt cancels the job, and `wait_transition` is deleted [M-02, doc row 22]
- [ ] **DPLX-07**: Auto-profiles always writes a `default` profile, for flatbed-only, feeder-only, and mixed devices, proven by a write-then-load round-trip test [C-08]

### Worker and Web Robustness

- [ ] **ROBU-01**: The worker loop survives any exception raised by the pipeline, the job store, or prune; each is logged with `exc_info` and the worker keeps serving [C-09]
- [ ] **ROBU-02**: Submitting when the queue is full returns HTTP 429 with a `Retry-After` header and a visible message in the status area; the server never blocks the event loop on a full queue, and shutdown never blocks on the worker [C-09, doc row 23]
- [ ] **ROBU-03**: Worker shutdown uses a stop flag with a bounded join; the worker tests that assumed a draining `stop()` are converted to a `wait_for_state` polling helper in the same change [C-09, N-24]
- [ ] **ROBU-04**: After a web scan finishes (DONE, FALLBACK, or FAILED) the Scan button re-enables without a page reload; the button is server-owned via an out-of-band swap, there is no duplicate `id="scan-btn"` in the DOM, and `app.js` is deleted [C-10]
- [ ] **ROBU-05**: All routes that do blocking I/O are plain `def` handlers; `/health` answers during a scan; the metadata cache is single-flight and profile mutation is locked so threadpool concurrency cannot race [M-01, doc row 26]
- [ ] **ROBU-06**: At startup, jobs left in a non-terminal state are marked FAILED with a "server restarted" reason before the worker starts; at shutdown the worker stops before the store closes [M-03, doc row 33]
- [ ] **ROBU-07**: Profiles are generated at startup (not inside the first job) using the config path that was actually loaded; `is_bare_default` recognises every shape of an untouched default profile [M-04]
- [ ] **ROBU-08**: `POST /api/scan` rejects an unknown profile and an over-long title with 422 before creating a job; `POST /api/cache/invalidate` validates its resource name [N-20]
- [ ] **ROBU-09**: htmx and PicoCSS are served from the package with pinned versions and SHA-384 integrity attributes; the UI works on a LAN with no internet [N-21]
- [ ] **ROBU-10**: State-changing POST endpoints reject cross-site requests using `Sec-Fetch-Site`; the default bind address is documented [N-22]
- [ ] **ROBU-11**: A browser test clicks Scan, waits for the terminal status, and asserts the button is enabled again, running in CI with no CDN egress [C-10, M-33]

### Configuration Strictness

- [ ] **CFG-01**: Unknown keys inside any config section (top level, `[paperless]`, `[scanner]`, `[output]`, `[profiles.<name>]`) are rejected at load with a message naming the section, the key, and the valid keys [M-18]
- [ ] **CFG-02**: A `--config` path that does not exist exits with code 2 and a message naming the path [M-19, doc row 14]
- [ ] **CFG-03**: `~` is expanded in every path setting and `$XDG_CONFIG_HOME` / `$XDG_STATE_HOME` are honoured for the default config and data locations [M-20, doc row 27]
- [ ] **CFG-04**: `log_level` accepts only valid level names, and `-v` sets the effective level to DEBUG [M-21, doc row 11, doc row 13]
- [ ] **CFG-05**: The Paperless token is a `SecretStr`; it never appears in `repr(settings)`, logs, or error messages [N-15]
- [ ] **CFG-06**: The documented `title` profile key sets the default job title when the user leaves the title blank [M-24, doc row 6]
- [ ] **CFG-07**: `auto-profiles --force` merges: it replaces only the keys it generates, preserves user-added keys such as `default_tags`, and never touches a profile it did not create [M-09, doc row 8]
- [ ] **CFG-08**: Config rewrites are atomic (temp file, fsync, rename), UTF-8 regardless of locale, and preserve file mode and comments [M-10]
- [ ] **CFG-09**: The recommended Docker Compose and docs mount the config directory rather than the file, so atomic rewrites succeed under the documented deployment [M-30, doc row 20]
- [ ] **CFG-10**: `saneless <subcommand> --help` works without a valid configuration file [N-25]
- [ ] **CFG-11**: The loaded config path and which keys came from the environment are logged at INFO at startup [U-01]

### Exception Translation

- [ ] **EXC-01**: Every SANE, httpx, img2pdf (all seven error classes), and tomllib exception is caught at its call site and re-raised as a saneless exception type with the original message; no third-party exception type escapes a module boundary [M-17]
- [ ] **EXC-02**: The CLI catches `ConfigError`, `ScanError`, `PaperlessError`, and `PdfError` and prints a one-line message with a non-zero exit code instead of a traceback; a missing `python-sane` import produces a clear install hint [M-17]
- [ ] **EXC-03**: A scan that produces zero pages reports "No pages were scanned" (and "All pages were blank" only when detection removed them) rather than a misleading message or a bare `ValueError` [N-06]
- [ ] **EXC-04**: A user-initiated abort at the flip prompt is recorded as a cancelled job, not a scanner failure [N-08]
- [ ] **EXC-05**: The worker logs every job failure with `exc_info` so the operator can find the cause [M-17]

### Geometry, Memory, and Timeouts

- [ ] **HARD-01**: Scanned pages are spooled to disk as they arrive with an explicit ordered page record; a 12-page scan comes out in order 1..12, duplex interleave reorders the record list, and peak memory stays bounded by one page [M-08]
- [ ] **HARD-02**: A mid-batch scanner error after N pages keeps the N pages already acquired and reports the error with the count [N-02]
- [ ] **HARD-03**: A scan timeout waits for the cancelled read to return before closing the device; a stuck read on a daemon thread never blocks process exit [M-12]
- [ ] **HARD-04**: The flatbed path shares the same timeout and image validation as the ADF path [M-13]
- [ ] **HARD-05**: `sane.init()` is called once per process with a re-entry guard and `sane.exit()` is called at shutdown; neither is called from a request path [N-04]

### Appliance Layer

- [ ] **APPL-01**: `saneless doctor` runs a shared check list (scanner reachable and named, Paperless URL reachable and token accepted, profiles configured, consume-dir fallback configured, data dir writable) and exits non-zero on any failing check [U-03]
- [ ] **APPL-02**: The index page shows a status strip with the same checks, refreshed on load and by a button, cached with a TTL, and skipped while a scan is active so it never probes the busy scanner [U-03]
- [ ] **APPL-03**: The status area and history table show pages scanned, pages removed as blank, and pages uploaded for every terminal job; manual duplex shows front and back counts during pass B [U-02]
- [ ] **APPL-04**: Every user-facing error shows a plain-language message with a suggested next step derived from `ErrorCategory`, with the raw technical detail inside a collapsed "Technical details" disclosure [U-05]
- [ ] **APPL-05**: Profiles have `label` and `description` fields; auto-generation fills them ("Feeder, single-sided", "Glass (flatbed)"), the dropdown shows labels with descriptions as help text, and feeder profiles sort first on sheet-fed scanners [U-04]
- [ ] **APPL-06**: When the config location is read-only, generated profiles are kept in memory and the status strip says so instead of failing silently [U-04, M-30]
- [ ] **APPL-07**: A placeholder or empty Paperless token is detected at startup, shown red on the status strip, and refuses scans (the server still starts so the operator can see why); the compose template has one place for the secret with the environment block commented out [U-01]
- [ ] **APPL-08**: A queued job shows "Waiting for '<title>' to finish (N ahead of you)" using `list_pending()` position [U-06]
- [ ] **APPL-09**: The flip prompt with its Continue and Abort buttons is rendered only for the browser that submitted the job (owner token in an HttpOnly, SameSite=Lax cookie set at submit); other viewers see "Waiting for the stack to be flipped"; Abort asks for confirmation [U-06]
- [ ] **APPL-10**: Each form control has one line of help text in plain words ("Who sent this document? Optional."), tags use a checkbox list that works with a thumb, and the operator can hide Tags and Correspondent for a simpler form [U-07]
- [ ] **APPL-11**: The recommended compose example includes the consume-directory mount with a two-line explanation, and the status strip reports "Fallback: not configured; scans cannot be kept if Paperless is down" when it is absent [U-08]
- [ ] **APPL-12**: All user-facing timestamps in the web UI and CLI display in the server's local timezone with the zone named [M-23]

### Delivery and Identity

- [ ] **DLVR-01**: Every reference to `kris-knigga/saneless`, `kris-knigga.github.io/saneless`, and `ghcr.io/kris-knigga/saneless` in shipped files (24 lines in nine files) is updated to the `kdknigga/saneless` forms; the PyPI distribution name stays `saneless` [M-27, doc row 19]
- [ ] **DLVR-02**: The release workflow succeeds end to end: PyPI trusted publisher and GHCR package configured for `kdknigga/saneless`, the unresolvable `pypa/gh-action-pypi-publish@v1.12` ref fixed, and a pre-release tag rehearsed against TestPyPI verified by a `pip install` and `docker pull` from a clean machine [M-26]
- [ ] **DLVR-03**: All GitHub Actions are pinned to commit SHAs with a Dependabot config, every job has a `permissions:` block, and a workflow audit (zizmor) runs in CI [N-31]
- [ ] **DLVR-04**: Container logs appear in `docker logs` (stderr handler enabled in the container command) [M-28]
- [ ] **DLVR-05**: The example config, `EXPOSE`, and `HEALTHCHECK` agree on one port [M-29, doc row 21]
- [ ] **DLVR-06**: A `.dockerignore` allow-list keeps config files, secrets, `.planning/`, and tests out of the build context [M-31]
- [ ] **DLVR-07**: The container runs as a non-root user, base images are pinned by digest, and a `WORKDIR` is set so relative writes do not land in `/` [N-26]
- [ ] **DLVR-08**: The built wheel contains the LICENSE file via PEP 639 `license` / `license-files` keys, with the legacy license classifier removed in the same change [N-30]
- [ ] **DLVR-09**: `.gitignore` no longer blanket-ignores `*.png`; only specific screenshot directories are excluded [N-32]
- [ ] **DLVR-10**: `saneless --version` prints the installed version [doc row 15]

### Documentation Accuracy

- [ ] **DOCS-01**: Every false claim in review section 8 (rows 1 through 34) is corrected or the behaviour is implemented, and each behaviour change in this milestone updates the sentence that described the old behaviour in the same phase [section 8]
- [ ] **DOCS-02**: README examples run as written: `saneless scan` shows the required `--title`, `source` values match real SANE spelling, and the tutorial link resolves [N-42, doc row 16, 17, 18]
- [ ] **DOCS-03**: The internal `docs/PRD.md` is removed from the published site (or updated to match the shipped code), and the README's tutorial link points at the getting-started page [N-29, doc row 34]
- [ ] **DOCS-04**: A "Which setup do I have?" page gives the exact compose lines for USB-on-host, network scanner, and `saned` elsewhere, linked from the quick-start prerequisites; the contradictory USB statements are reconciled [U-10, doc row 29]
- [ ] **DOCS-05**: The getting-started and Docker pages state in one sentence that the UI has no login and binds all interfaces by default, and point at the reverse-proxy option [U-09]
- [ ] **DOCS-06**: The compose example's Paperless service is either complete (Redis broker included) or replaced by a pointer to the official Paperless compose file, and `docker run` examples use `$(pwd)` [doc row 31, 32]

### Test Suite Hygiene

- [ ] **TEST-01**: The suite is hermetic: an autouse fixture isolates `HOME`, `XDG_*`, and the working directory, and the suite passes with `HOME` pointed at an empty directory [M-34]
- [ ] **TEST-02**: No `time.sleep` remains in `tests/`; retry tests use a patched clock and worker tests use the `wait_for_state` helper [N-18, N-24]
- [ ] **TEST-03**: Tests with no assertion, tests that only pin literal constants, and duplicate tests are removed after a `pytest --cov` line diff proves no coverage is lost [N-40]
- [ ] **TEST-04**: Scanner tests assert what their names claim (init-once, read-back resolution, geometry presence) [N-05]
- [ ] **TEST-05**: CLI tests read back files they claim to verify, docstrings match behaviour, and `--force` is verified by file content [N-33]
- [ ] **TEST-06**: Data-loss and negative paths have tests: upload failure preserves the PDF, Paperless FAILURE maps to FAILED, worker survives a raising prune, two-thread store access, and the flip timeout [M-33]
- [x] **TEST-07**: `pytest-timeout` guards the suite so a hung thread test cannot block CI forever [M-33]

### Minor Sweep

- [ ] **SWP-01**: `PIL.Image.MAX_IMAGE_PIXELS` is set in exactly one place, not at import time in three modules [N-10]
- [ ] **SWP-02**: `configure_logging` is idempotent; calling it twice attaches one stderr handler [N-16]
- [ ] **SWP-03**: `_transport` becomes a documented public constructor parameter and the other API-shape nits in N-19 are resolved [N-19]
- [ ] **SWP-04**: Metadata fetch failures are logged with their cause, and the cache serves stale data on error instead of an empty list [N-23]
- [ ] **SWP-05**: The `serve` pre-bind probe supports IPv6 hosts, and `--port 0` is honoured rather than falling back to config [N-27]
- [ ] **SWP-06**: `devices --json --capabilities` writes only JSON to stdout so it pipes to `jq` [N-28]
- [ ] **SWP-07**: The nineteen comments in `src/` that cite planning artefacts are rewritten to explain the reason in plain words [N-34]
- [ ] **SWP-08**: The six `# noqa` / `# type: ignore` in `src/` and four in `tests/` are removed by fixing the underlying issue, in line with the project rule [N-35]
- [ ] **SWP-09**: The duplicated-logic inventory (job database path, slug rules, active-state lists, label maps) is reduced to one implementation each [N-36]
- [ ] **SWP-10**: Dead code identified in N-37 is deleted [N-37]
- [ ] **SWP-11**: Logging is consistent across modules: one logger per module, consistent levels for the same event class [N-41]
- [ ] **SWP-12**: Path settings are `Path` on the settings model and function signatures take `Path`, removing the nine `Path()` re-wrap sites [N-43]
- [ ] **SWP-13**: Naming and template nits in N-44 are resolved, and the stale debug notes in N-45 are updated or removed [N-44, N-45]
- [ ] **SWP-14**: The small `auto_profiles` cleanups in N-11 are applied [N-11]

## Future Requirements

Deferred from the v1.0 backlog. Tracked but not in this milestone.

### Notifications

- **NOTF-01**: System sends desktop/push notification on scan completion or error

### Advanced Scanning

- **ADVS-01**: Support for driverless scanning via sane-airscan (eSCL/WSD)
- **ADVS-02**: scanbd hardware button integration to trigger scans

### Targets

- **TARG-01**: Additional ingestion targets beyond paperless-ngx (cloud storage, email, local folder)

### UI Enhancements

- **UIX-01**: Mobile-optimized responsive layout beyond the tag picker and help text delivered in APPL-10
- **UIX-02**: Multi-language support / internationalization

## Out of Scope

| Feature | Reason |
|---------|--------|
| Renaming the PyPI distribution from `saneless` | Review M-27 decision: only the repo, GHCR, and docs-site names change; the package import and CLI name stay `saneless` |
| Multi-user authentication or per-user job ownership | Trusted-LAN decision stands; the APPL-09 owner token is a footgun guard for the flip prompt, not an auth mechanism |
| Refusing to boot on a placeholder token | The web UI is the operator's only diagnostic surface; start red and refuse scans instead (research FEATURES.md) |
| `platformdirs` or any new runtime dependency | Research confirmed every capability is satisfiable with stdlib and already-pinned packages |
| Upgrading to htmx 4 | Would invalidate the C-10 fix; stay on htmx 2.x |
| Container `HEALTHCHECK` calling `saneless doctor` | Doctor does network I/O and would flap the container during routine Paperless restarts; keep `/health` |
| A configurable Paperless retry count (doc row 28) | Fix the documentation to say the count is fixed; configurability is not requested |
| Built-in OCR, image editing, cloud targets, saned management, film scanning, custom pipelines, page reordering, live preview, multiple simultaneous scanners | Carried over from v1.0 out-of-scope list; reasons unchanged |

## Traceability

Which phases cover which requirements. Updated during roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|
| CI-01 | Phase 20 — CI Gate | Complete |
| CI-02 | Phase 31 — Delivery, Identity, and Documentation Accuracy | Pending |
| CTR-01 | Phase 21 — Vocabulary and Contracts | Complete |
| CTR-02 | Phase 21 — Vocabulary and Contracts | Partial — typed `ScanResult` shipped; `ScanOutcome.FAILED` deferred to Phase 23 with the code path that produces it (D-07) |
| CTR-03 | Phase 21 — Vocabulary and Contracts | Complete |
| CTR-04 | Phase 21 — Vocabulary and Contracts | Complete |
| CTR-05 | Phase 21 — Vocabulary and Contracts | Complete |
| STOR-01 | Phase 22 — Job Store Hardening | Complete |
| STOR-02 | Phase 22 — Job Store Hardening | Complete |
| STOR-03 | Phase 22 — Job Store Hardening | Complete |
| STOR-04 | Phase 22 — Job Store Hardening | Complete |
| STOR-05 | Phase 22 — Job Store Hardening | Complete |
| OUTC-01 | Phase 23 — Honest Outcomes and Never Lose a Scan | Complete |
| OUTC-02 | Phase 23 — Honest Outcomes and Never Lose a Scan | Complete |
| OUTC-03 | Phase 23 — Honest Outcomes and Never Lose a Scan | Complete |
| OUTC-04 | Phase 23 — Honest Outcomes and Never Lose a Scan | Complete |
| OUTC-05 | Phase 23 — Honest Outcomes and Never Lose a Scan | Complete |
| OUTC-06 | Phase 23 — Honest Outcomes and Never Lose a Scan | Complete |
| OUTC-07 | Phase 23 — Honest Outcomes and Never Lose a Scan | Complete |
| OUTC-08 | Phase 23 — Honest Outcomes and Never Lose a Scan | Complete |
| OUTC-09 | Phase 23 — Honest Outcomes and Never Lose a Scan | Complete |
| OUTC-10 | Phase 23 — Honest Outcomes and Never Lose a Scan | Complete |
| OUTC-11 | Phase 23 — Honest Outcomes and Never Lose a Scan | Complete |
| DARK-01 | Phase 23.1 — Dark Mode and the Commit Gate | Complete |
| DARK-02 | Phase 23.1 — Dark Mode and the Commit Gate | Complete |
| DARK-03 | Phase 23.1 — Dark Mode and the Commit Gate | Complete |
| GATE-01 | Phase 23.1 — Dark Mode and the Commit Gate | Complete |
| GATE-02 | Phase 23.1 — Dark Mode and the Commit Gate | Complete |
| GATE-03 | Phase 23.1 — Dark Mode and the Commit Gate | Complete |
| SCNR-01 | Phase 24 — Scanner Truthfulness | Partial — feeder routing done; strategy reads deferred to Phase 25 (DPLX-03) |
| SCNR-02 | Phase 24 — Scanner Truthfulness | Complete |
| SCNR-03 | Phase 24 — Scanner Truthfulness | Complete |
| SCNR-04 | Phase 24 — Scanner Truthfulness | Complete |
| SCNR-05 | Phase 24 — Scanner Truthfulness | Complete |
| SCNR-06 | Phase 24 — Scanner Truthfulness | Complete |
| SCNR-07 | Phase 24 — Scanner Truthfulness | Complete |
| SCNR-08 | Phase 24 — Scanner Truthfulness | Complete |
| DPLX-01 | Phase 25 — Manual Duplex | Pending |
| DPLX-02 | Phase 25 — Manual Duplex | Pending |
| DPLX-03 | Phase 25 — Manual Duplex | Pending |
| DPLX-04 | Phase 25 — Manual Duplex | Pending |
| DPLX-05 | Phase 25 — Manual Duplex | Pending |
| DPLX-06 | Phase 25 — Manual Duplex | Pending |
| DPLX-07 | Phase 25 — Manual Duplex | Pending |
| ROBU-01 | Phase 26 — Worker and Web Robustness | Pending |
| ROBU-02 | Phase 26 — Worker and Web Robustness | Pending |
| ROBU-03 | Phase 26 — Worker and Web Robustness | Pending |
| ROBU-04 | Phase 26 — Worker and Web Robustness | Pending |
| ROBU-05 | Phase 26 — Worker and Web Robustness | Pending |
| ROBU-06 | Phase 26 — Worker and Web Robustness | Pending |
| ROBU-07 | Phase 26 — Worker and Web Robustness | Pending |
| ROBU-08 | Phase 26 — Worker and Web Robustness | Pending |
| ROBU-09 | Phase 26 — Worker and Web Robustness | Pending |
| ROBU-10 | Phase 26 — Worker and Web Robustness | Pending |
| ROBU-11 | Phase 26 — Worker and Web Robustness | Pending |
| CFG-01 | Phase 27 — Configuration Strictness | Pending |
| CFG-02 | Phase 27 — Configuration Strictness | Pending |
| CFG-03 | Phase 27 — Configuration Strictness | Pending |
| CFG-04 | Phase 27 — Configuration Strictness | Pending |
| CFG-05 | Phase 27 — Configuration Strictness | Pending |
| CFG-06 | Phase 27 — Configuration Strictness | Pending |
| CFG-07 | Phase 27 — Configuration Strictness | Pending |
| CFG-08 | Phase 27 — Configuration Strictness | Pending |
| CFG-09 | Phase 27 — Configuration Strictness | Pending |
| CFG-10 | Phase 27 — Configuration Strictness | Pending |
| CFG-11 | Phase 27 — Configuration Strictness | Pending |
| EXC-01 | Phase 28 — Exception Translation | Pending |
| EXC-02 | Phase 28 — Exception Translation | Pending |
| EXC-03 | Phase 28 — Exception Translation | Pending |
| EXC-04 | Phase 28 — Exception Translation | Pending |
| EXC-05 | Phase 28 — Exception Translation | Pending |
| HARD-01 | Phase 29 — Geometry, Memory, and Timeouts | Pending |
| HARD-02 | Phase 29 — Geometry, Memory, and Timeouts | Pending |
| HARD-03 | Phase 29 — Geometry, Memory, and Timeouts | Pending |
| HARD-04 | Phase 29 — Geometry, Memory, and Timeouts | Pending |
| HARD-05 | Phase 29 — Geometry, Memory, and Timeouts | Pending |
| APPL-01 | Phase 30 — Appliance Layer | Pending |
| APPL-02 | Phase 30 — Appliance Layer | Pending |
| APPL-03 | Phase 30 — Appliance Layer | Pending |
| APPL-04 | Phase 30 — Appliance Layer | Pending |
| APPL-05 | Phase 30 — Appliance Layer | Pending |
| APPL-06 | Phase 30 — Appliance Layer | Pending |
| APPL-07 | Phase 30 — Appliance Layer | Pending |
| APPL-08 | Phase 30 — Appliance Layer | Pending |
| APPL-09 | Phase 30 — Appliance Layer | Pending |
| APPL-10 | Phase 30 — Appliance Layer | Pending |
| APPL-11 | Phase 30 — Appliance Layer | Pending |
| APPL-12 | Phase 30 — Appliance Layer | Pending |
| DLVR-01 | Phase 31 — Delivery, Identity, and Documentation Accuracy | Pending |
| DLVR-02 | Phase 31 — Delivery, Identity, and Documentation Accuracy | Pending |
| DLVR-03 | Phase 31 — Delivery, Identity, and Documentation Accuracy | Pending |
| DLVR-04 | Phase 31 — Delivery, Identity, and Documentation Accuracy | Pending |
| DLVR-05 | Phase 31 — Delivery, Identity, and Documentation Accuracy | Pending |
| DLVR-06 | Phase 31 — Delivery, Identity, and Documentation Accuracy | Pending |
| DLVR-07 | Phase 31 — Delivery, Identity, and Documentation Accuracy | Pending |
| DLVR-08 | Phase 31 — Delivery, Identity, and Documentation Accuracy | Pending |
| DLVR-09 | Phase 31 — Delivery, Identity, and Documentation Accuracy | Pending |
| DLVR-10 | Phase 31 — Delivery, Identity, and Documentation Accuracy | Pending |
| DOCS-01 | Phase 31 — Delivery, Identity, and Documentation Accuracy | Pending |
| DOCS-02 | Phase 31 — Delivery, Identity, and Documentation Accuracy | Pending |
| DOCS-03 | Phase 31 — Delivery, Identity, and Documentation Accuracy | Pending |
| DOCS-04 | Phase 31 — Delivery, Identity, and Documentation Accuracy | Pending |
| DOCS-05 | Phase 31 — Delivery, Identity, and Documentation Accuracy | Pending |
| DOCS-06 | Phase 31 — Delivery, Identity, and Documentation Accuracy | Pending |
| TEST-01 | Phase 32 — Suite Hygiene and Minor Sweep | Pending |
| TEST-02 | Phase 32 — Suite Hygiene and Minor Sweep | Pending |
| TEST-03 | Phase 32 — Suite Hygiene and Minor Sweep | Pending |
| TEST-04 | Phase 32 — Suite Hygiene and Minor Sweep | Pending |
| TEST-05 | Phase 32 — Suite Hygiene and Minor Sweep | Pending |
| TEST-06 | Phase 32 — Suite Hygiene and Minor Sweep | Pending |
| TEST-07 | Phase 20 — CI Gate | Complete |
| SWP-01 | Phase 32 — Suite Hygiene and Minor Sweep | Pending |
| SWP-02 | Phase 32 — Suite Hygiene and Minor Sweep | Pending |
| SWP-03 | Phase 32 — Suite Hygiene and Minor Sweep | Pending |
| SWP-04 | Phase 32 — Suite Hygiene and Minor Sweep | Pending |
| SWP-05 | Phase 32 — Suite Hygiene and Minor Sweep | Pending |
| SWP-06 | Phase 32 — Suite Hygiene and Minor Sweep | Pending |
| SWP-07 | Phase 32 — Suite Hygiene and Minor Sweep | Pending |
| SWP-08 | Phase 32 — Suite Hygiene and Minor Sweep | Pending |
| SWP-09 | Phase 32 — Suite Hygiene and Minor Sweep | Pending |
| SWP-10 | Phase 32 — Suite Hygiene and Minor Sweep | Pending |
| SWP-11 | Phase 32 — Suite Hygiene and Minor Sweep | Pending |
| SWP-12 | Phase 32 — Suite Hygiene and Minor Sweep | Pending |
| SWP-13 | Phase 32 — Suite Hygiene and Minor Sweep | Pending |
| SWP-14 | Phase 32 — Suite Hygiene and Minor Sweep | Pending |

**Coverage:**
- v2.0 requirements: 118 total
- Mapped to phases: 118
- Unmapped: 0
- Phases: 13 (Phase 20 through Phase 32)

---
*Requirements defined: 2026-09-09*
*Last updated: 2026-09-09 after milestone v2.0 roadmap creation*
