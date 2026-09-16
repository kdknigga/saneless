# Roadmap: saneless

**Milestone:** v2.0 "Prep for release"
**Requirements:** `.planning/REQUIREMENTS.md` (125 v2.0 requirements)
**Research:** `.planning/research/SUMMARY.md`
**Review under remediation:** `.planning/reviews/2026-09-09-code-review.md`
**Previous milestone:** v1.0, archived at `.planning/milestones/v1.0-ROADMAP.md` (ended at Phase 19)

## Overview

v2.0 is a hardening milestone on a working appliance. Every requirement resolves a finding from the 2026-09-09 comprehensive code review. The phase order is not the review's own section-10 order: it is the reconciled dependency spine from research, which fixes five places where following the review literally would reintroduce a bug it is trying to fix.

The spine is built on one discipline — **every phase leaves the suite green, and its own tests could not have passed before it.** That is why CI lands first (Phase 20) with zero source changes, why the vocabulary and the job-store migration ladder land before anything that needs a new column or a new state (Phases 21–22), why scanner ground truth is fixed before duplex correctness is asserted against it (Phase 24 before 25), and why the user-visible appliance layer comes only after honest outcomes, truthful scanner errors, and exception translation already exist to feed it (Phase 30).

Five orderings are load-bearing and must not be rearranged during planning:

1. **CI first.** Without it, "green" means "green on one developer's machine."
2. **Job-store lock and `PRAGMA user_version` migration ladder (Phase 22) before typed results (Phase 23).** All result columns are added in one migration so no later phase reaches for another bare `ALTER TABLE ... except: pass`.
3. **Scanner truthfulness (Phase 24) before manual duplex (Phase 25).** The backend's own blank-page removal changes duplex page parity, and duplex tests written against untruthful SANE fakes assert a system that will not exist after Phase 24.
4. **`output.data_dir` (OUTC-09, M-30's sibling finding N-39) in the same phase as `failed/` preservation (Phase 23), and the config-*directory* mount (CFG-09 / M-30) in the same phase as the atomic config write (CFG-08 / M-10, Phase 27).** `os.replace` returns `EBUSY` over a bind-mounted file, so a "durable write" shipped without the mount fix is broken on day one for the deployment the docs recommend.
5. **htmx/PicoCSS vendoring (ROBU-09) in the same phase as the Scan-button fix (ROBU-04), Phase 26.** The C-10 browser regression test runs in a CI sandbox with no egress; CDN-loaded assets make it impossible.

**Documentation is cross-cutting.** Each phase corrects the sentences in `docs/` and `README.md` that described the behaviour it changed, in that same phase. DOCS-01 sits in Phase 31 only as the final audit that all 34 rows of review section 8 are now true.

## Phases

**Phase Numbering:**

- Integer phases (20, 21, 22...): Planned milestone work, continuing from v1.0's Phase 19
- Decimal phases (22.1, 22.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [x] **Phase 20: CI Gate** - GitHub Actions runs lint, format, both type checkers, and the non-browser suite on every push; `pytest-timeout` guards against hangs (completed 2026-09-10)
- [x] **Phase 21: Vocabulary and Contracts** - One `JobState`, one label map, one `ErrorCategory`, one `classify_source()`, and typed `ScanResult`/`UploadResult`, with zero behaviour change (completed 2026-09-10)
- [x] **Phase 22: Job Store Hardening** - `RLock` on every method, `PRAGMA user_version` migration ladder, and every result column added in one migration (completed 2026-09-10)
- [x] **Phase 23: Honest Outcomes and Never Lose a Scan** - Typed outcomes end to end, `FALLBACK` state, PDFs preserved under a durable `data_dir`, unique names, correct DPI (completed 2026-09-11)
- [x] **Phase 23.1: Dark Mode and the Commit Gate** (INSERTED) - The dark palette actually engages, every status colour passes AA in both schemes, and a TDD RED commit is possible without suppressing a gate (completed 2026-09-11)
- [x] **Phase 24: Scanner Truthfulness** - One source classifier wired everywhere, real SANE error messages, correct geometry and read-back DPI, fakes that model real python-sane (completed 2026-09-13)
- [x] **Phase 25: Manual Duplex** - A `duplex` profile field, a required `FlipCoordinator` with timeout, a CLI flip prompt, and a visible reverse pass (completed 2026-09-14)
- [x] **Phase 26: Worker and Web Robustness** - Unkillable worker, 429 backpressure, sync routes, crash recovery, server-owned Scan button, vendored front-end assets (completed 2026-09-14)
- [x] **Phase 27: Configuration Strictness** - Unknown keys rejected with the right section named, atomic UTF-8 rewrites, XDG/`~` expansion, `SecretStr`, and the config-directory mount that makes atomic rewrite possible (completed 2026-09-15)
- [x] **Phase 28: Exception Translation** - No third-party exception type escapes a module boundary; the CLI prints one line, not a traceback (completed 2026-09-15)
- [x] **Phase 29: Geometry, Memory, and Timeouts** - Pages spooled to disk in explicit order, safe cancel, shared flatbed/ADF timeout, guarded `sane.init()` (completed 2026-09-16)
- [ ] **Phase 30: Appliance Layer** - Status strip and `saneless doctor` from one check list, page counts, plain-language errors, human profile labels, queue position, owner-only flip prompt
- [ ] **Phase 31: Delivery, Identity, and Documentation Accuracy** - `kdknigga/saneless` everywhere with a CI grep guard, a release workflow proven end to end, container fixes, and every false doc claim corrected
- [ ] **Phase 32: Suite Hygiene and Minor Sweep** - Hermetic tests, no `time.sleep`, no low-value tests, and the remaining N-01..N-45 sweep
- [ ] **Phase 33: Disable the OpenAPI Schema and Docs Endpoints** - `/openapi.json`, `/docs` and `/redoc` are gone rather than broken; nothing claims saneless serves an API schema

## Phase Details

### Phase 20: CI Gate

**Goal**: Every push and pull request is provably green — ruff, ruff format, ty, pyrefly, and the non-browser pytest suite run in GitHub Actions and a red run blocks merge — so every phase that follows can be trusted, with the contributing docs updated in-phase to describe the gate
**Depends on**: Nothing (first phase of v2.0)
**Requirements**: CI-01, TEST-07
**Success Criteria** (what must be TRUE):

  1. A push with a ruff violation, a `ty` error, a `pyrefly` error, or a failing test produces a red GitHub Actions run that blocks merge
  2. A clean push produces a green run that exercises all five checks, and the run is visible on the pull request
  3. A test that hangs is killed by `pytest-timeout` with a per-test traceback instead of consuming the CI job's full time budget

**Plans**: 5 plans

Plans:

- [x] 20-01-PLAN.md — pytest-timeout hang guard, ci.yml + dependabot.yml, CONTRIBUTING.md
- [x] 20-02-PLAN.md — repo to private, .planning-stripped branch built locally, publication checkpoint
- [x] 20-03-PLAN.md — push master + filtered branch, open PR (never merged), green run, read check contexts
- [x] 20-04-PLAN.md — master branch ruleset via gh api + read-back, one seeded break proving red
- [x] 20-05-PLAN.md — ty/pyrefly bump, three suppressions removed with real fixes, green run on the PR

Note: the earlier "zero source changes in this phase" note is SUPERSEDED by CONTEXT.md D-16 — bumping `ty` and `pyrefly` and fixing the resulting type errors is planned work in Phase 20 (Plan 05), landing as a separate, later commit than `ci.yml` per D-17. The naming grep guard (CI-02) is deliberately deferred to Phase 31, where the rename it guards actually lands — adding it here would make CI red from its first run.

### Phase 21: Vocabulary and Contracts

**Goal**: The words the system uses about itself exist exactly once and are enforceable by the type checkers — one `JobState` enum, one active-state list, one state-to-label map, one `ErrorCategory`, one `classify_source()`, and typed pipeline results — with no behaviour change and the docs that named the old `"fallback"` string updated in-phase
**Depends on**: Phase 20
**Requirements**: CTR-01, CTR-02, CTR-03, CTR-04, CTR-05
**Success Criteria** (what must be TRUE):

  1. A parametrised test proves every `JobState` member has a label and appears in exactly one active-state list, and the worker, web templates, and CLI all read them from the same module
  2. `classify_source()` returns the correct `SourceKind` for "Automatic Document Feeder", "ADF Front", "ADF Duplex", "Flatbed", "Auto", and vendor variants, and it is the only classification rule in the codebase
  3. The pipeline returns a typed `ScanResult` and `upload_document` returns a typed `UploadResult`; the `"fallback"` magic string is absent from `src/`, `tests/`, and `docs/`
  4. Existing imports from `job.py` still resolve (re-exports), and the whole suite passes unchanged

**Plans**: 5 plans

Plans:
**Wave 1**

- [x] 21-01-PLAN.md — `vocabulary.py` (enums, three state classifications, four total lookups behind `assert_never`) + `job.py` re-exports and `Job.is_active`/`is_busy`
- [x] 21-02-PLAN.md — `SourceKind` + `classify_source()` in `scanner/base.py`, both existing rules delegated; carries the phase's one authorised behaviour change (D-11 / C-06)

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 21-03-PLAN.md — web rewiring: `_STATE_LABELS` and `humanize_state` deleted, filters re-backed, all state literals removed from the three templates
- [x] 21-04-PLAN.md — `PipelineEvent.job_state`, worker `_status_cb` collapse, CLI `_event_labels` deleted, `classify_error` moved off `ScanWorker`

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 21-05-PLAN.md — typed `UploadResult` (atomic across five stub sites) and `ScanResult`; sentinel deleted; the false `FALLBACK` doc claim corrected

Waves: 1 = {21-01, 21-02} · 2 = {21-03, 21-04} · 3 = {21-05}

### Phase 22: Job Store Hardening

**Goal**: The job store is safe under concurrency and can evolve its schema honestly — an `RLock` around every public method, a `PRAGMA user_version` migration ladder that opens a v1.0 database cleanly, and every result column this milestone will ever need added in one migration — with the storage docs updated in-phase
**Depends on**: Phase 21
**Requirements**: STOR-01, STOR-02, STOR-03, STOR-04, STOR-05
**Success Criteria** (what must be TRUE):

  1. Two threads calling any mix of `JobStore` methods for 200 rounds complete with zero exceptions and no interleaved-transaction corruption
  2. A database file written by v1.0 opens, migrates through the ladder, and reports the current `user_version`; the bare `ALTER TABLE ... except: pass` is gone
  3. The job table carries `outcome`, `pages_scanned`, `pages_removed`, `pages_uploaded`, `warning`, and `owner_token` after a single migration step, even though most stay unused until later phases
  4. `fail_active_jobs()` marks every non-terminal job FAILED with a "server restarted" reason, and `list_pending()` returns queued jobs in creation order
  5. The row-to-`Job` mapping and its column list appear exactly once, and `prune()` reports its count from one statement

**Plans**: 6 plans

Plans:

**Wave 1**

- [x] 22-01-PLAN.md — `StorageError`, the `PRAGMA user_version` migration ladder, and the WAL-before-autocommit open sequence

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 22-02-PLAN.md — one `_COLUMNS` tuple, six new `Job` fields, one `_row_to_job`

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 22-03-PLAN.md — the `@_locked` decorator, per-body transactions, and the three structural/concurrency tests

**Wave 4** *(blocked on Wave 3 completion)*

- [x] 22-04-PLAN.md — `prune()` as one `DELETE` reporting `cursor.rowcount`

**Wave 5** *(blocked on Wave 4 completion)*

- [x] 22-05-PLAN.md — `fail_active_jobs()` and `list_pending()`, derived from `ACTIVE_STATES`

**Wave 6** *(blocked on Wave 5 completion)*

- [x] 22-06-PLAN.md — storage docs corrected, phase-level green gate and traceability

Note: every plan touches `src/saneless/job.py` and `tests/test_job.py`, so the waves are strictly sequential (1 through 6) -- this phase offers no same-wave parallelism.

### Phase 23: Honest Outcomes and Never Lose a Scan

**Goal**: A job's recorded state is always the truth and a scanned document is never destroyed by a downstream failure — Paperless failures and timeouts raise, consume-directory delivery is recorded as `FALLBACK`, unrecoverable uploads preserve the PDF under a durable `data_dir/failed/` with a unique name and correct DPI — with every doc sentence that promised the old silent-DONE behaviour rewritten in-phase
**Depends on**: Phase 22
**Requirements**: OUTC-01, OUTC-02, OUTC-03, OUTC-04, OUTC-05, OUTC-06, OUTC-07, OUTC-08, OUTC-09, OUTC-10, OUTC-11
**Success Criteria** (what must be TRUE):

  1. A Paperless task ending FAILURE, or a poll that exceeds its monotonic deadline, records the job FAILED with the Paperless message — never DONE — and a PDF still exists on disk whose path is named in the job error
  2. A PDF that reaches only the consume directory records the job as `FALLBACK` with a warning, rendered distinctly from DONE in the status area, the history table, and `saneless jobs`
  3. When upload and consume-directory fallback both fail after N pages, the assembled PDF is in `<data_dir>/failed/` under a unique name and no page image or PDF was deleted on that path
  4. An A4 page scanned at 300 DPI produces a PDF with a 595 x 842 pt MediaBox, and two jobs with the same title produce two distinct PDF file names
  5. A parametrised end-to-end test drives the real worker and pipeline with a stub scanner through SUCCESS, Paperless FAILURE, TIMEOUT, consume-dir fallback, and duplex mismatch, asserting persisted state, outcome, page counts, and file preservation for each
  6. `poll_task` reaches a terminal status against BOTH a v9-shaped (bare list, uppercase status, `result`) and a v10-shaped (paginated `{"count","results"}`, lowercase status, `result_data.error_message`) `/api/tasks/` response, and `PaperlessClient` sends an explicit API-version `Accept` header

**Plans**: 9 plans in 5 waves

Plans:

- [x] 23-01-PLAN.md — Wave 0 test scaffolding, JobState.FALLBACK, job_state_for, PaperlessTimeoutError, ConnectionStatus (wave 1)
- [x] 23-02-PLAN.md — output.data_dir, db_path/failed_dir properties, entry points, Dockerfile and compose (wave 1)
- [x] 23-03-PLAN.md — filename sanitiser, unique PDF naming, fixed-DPI layout, PipelineRequest.job_id (wave 1)
- [x] 23-04-PLAN.md — paperless API-version pin, v9/v10 parsing, raising poll_task, ConnectionStatus, atomic consume-dir rename (wave 2)
- [x] 23-05-PLAN.md — FALLBACK rendering in status partial, history table, CSS, app.js, CLI, and UI-SPEC (wave 2)
- [x] 23-06-PLAN.md — the preservation guard spanning upload_document and poll_task, plus duplex parity (wave 3)
- [x] 23-07-PLAN.md — JobStore.finish_job and the worker consuming the ScanResult (wave 4)
- [x] 23-08-PLAN.md — the parametrised five-outcome end-to-end test (wave 5)
- [x] 23-09-PLAN.md — documentation sweep: fallback status, connection statuses, data_dir, upgrade note (wave 5)

Wave order is load-bearing, not cosmetic. **23-04 Task 1 bundles the v9/v10 shape tolerance with the raise semantics in one non-splittable commit** — a commit where timeouts raise against a still-misparsed response would record every successful scan as FAILED with a stray preserved PDF. The preservation guard (23-06) is strictly after it. Do not reorder or split these during execution.

Note: the preservation `try/except` must span both `upload_document` and `poll_task`. Wrapping only the upload call means a correct FAILURE raise unwinds the `TemporaryDirectory` and deletes the document this phase exists to protect.

Note (added 2026-09-11 from Phase 23 research): criterion 6 / OUTC-11 was not in the original scope. `poll_task` assumes API v9 while paperless-ngx now serves v10 by default to a client that sends no version header. The bug is masked today because `pipeline.py:521-527` discards the poll result; criteria 1 and 3 remove that mask, at which point every successful scan would record FAILED with a preserved stray PDF. The fix is a prerequisite for criterion 1, not an extension of it.

### Phase 23.1: Dark Mode and the Commit Gate (INSERTED)

**Goal**: Two defects that Phase 23 execution exposed are closed — the web UI actually renders Pico v2's dark palette when the operator's OS asks for it, with every status colour passing WCAG AA in both schemes; and a TDD RED commit is possible without `--no-verify`, `# type: ignore`, or disabling a rule, while type errors still cannot reach master
**Depends on**: Phase 23
**Requirements**: DARK-01, DARK-02, DARK-03, GATE-01, GATE-02, GATE-03
**Success Criteria** (what must be TRUE):

  1. With `prefers-color-scheme: dark`, a Playwright test asserts the root element's (`document.documentElement`) **computed** background is Pico's dark surface rather than the light one, and that test fails if `data-theme="auto"` is restored. *(Amended in 23.1: `body` is transparent in both schemes because Pico paints the page surface on `:root`, so reading `body` could never pass.)*
  2. `.status-done`, `.status-error` and `.status-fallback` each measure at least 4.5:1 against the surface in **both** schemes, asserted by computed-colour tests rather than by eye
  3. A test file referencing a symbol that does not exist yet can be committed with hooks enabled and no suppression of any kind
  4. A deliberate type error is still rejected before it can reach master — proven at whichever gate now owns that job
  5. No verification command in `.planning/`, the hooks, or CI uses bare `uv run pyrefly check`

**Plans**: 4 plans in 2 waves

Plans:

- [x] 23.1-01-PLAN.md — dark palette engaged, app-owned fallback amber, T1/T2/T3 computed-colour tests with mutation checks, master UI-SPEC convention (wave 1, TDD)
- [x] 23.1-02-PLAN.md — type checks split: src-only at commit, full at pre-merge-commit and pre-push; CI pyrefly paths; CONTRIBUTING and CLAUDE.md (wave 1)
- [x] 23.1-03-PLAN.md — every pathless pyrefly command under .planning/ rewritten to name src tests, residue enumerated and justified (wave 1)
- [x] 23.1-04-PLAN.md — three prek shims installed from the main checkout, D-07 RED/commit/merge/push demonstrations, CI and ruleset read-backs, close-out (wave 2)

**Why this is an insertion rather than end-of-milestone work.** GATE-01/02 affect every phase from 24 to 32: all eight Phase 23 executors independently hit the same wall, and each spent real effort rediscovering it. Fixing it once here compounds across the nine phases that follow. DARK-01/02 are not urgent in the same way and are here only because the user asked for one phase covering both.

**Coupling to Phase 26.** DARK-03 settles whether `pico.colors.css` ships. Phase 26's ROBU-09 vendors htmx and PicoCSS for the no-egress CI sandbox, so whatever this phase decides about which Pico files exist must survive that vendoring — and Phase 26 must not silently reintroduce `data-theme="auto"` or drop the dark-scheme overrides. Check this explicitly when planning 26.

**Evidence base.** `.planning/phases/23-honest-outcomes-and-never-lose-a-scan/deferred-items.md` (written by plan 23-05, with the measurement DARK-02 needs) and `.planning/todos/pending/002-dark-mode-never-engages.md`. Both predate this phase and should be read before planning it.

### Phase 24: Scanner Truthfulness

**Goal**: The scanner layer reports what actually happened — the single `classify_source()` drives every feeder decision, real SANE errors carry their real messages, geometry and DPI are read from the device rather than assumed, and the test doubles behave like python-sane 2.9.2 — with the scanner-discovery and ADF docs corrected in-phase
**Depends on**: Phase 23
**Requirements**: SCNR-01, SCNR-02, SCNR-03, SCNR-04, SCNR-05, SCNR-06, SCNR-07, SCNR-08
**Success Criteria** (what must be TRUE):

  1. A device whose feeder is named "Automatic Document Feeder" scans a full stack, and auto-profiles never collapses two distinct feeder sources into one slug
  2. A first-page SANE error other than the exact "Document feeder out of documents" message surfaces as a `ScanError` carrying the SANE text, never as "No paper detected"
  3. The backend drops no pages on its own; blank-page removal happens only in the pipeline, only when the profile enables it, and manual-duplex page parity survives
  4. Geometry is written only when the device reports `tl_x`/`tl_y`/`br_x`/`br_y` with units read from the option descriptor; a test proves the Pillow crop fallback is reachable and uses the resolution read back after all options are set
  5. The rewritten fakes match real python-sane semantics (unknown option stored silently, bad value for a known option raises `_sane.error`, structurally wrong access raises `AttributeError`), and an opt-in integration test drives the real SANE `test` backend through a `SANE_CONFIG_DIR` scoped to `tmp_path` to pull ten pages from a long feeder name

**Plans**: 8 plans in 7 waves

Plans:

**Wave 1** *(no file overlap — 24-01 owns the test doubles and CI config, 24-02 owns auto-profiles)*

- [x] 24-01-PLAN.md — shared `tests/fake_sane.py` faithful to python-sane 2.9.2, `sane_hardware` marker, session-scoped `SANE_CONFIG_DIR` fixture, SCNR-08's ten-page assertion committed RED (D-17, D-18)
- [x] 24-02-PLAN.md — `classify_source` as the only flatbed/Auto rule, every source slugged from its own name over `[a-z0-9-]`, collision tie-break, orphan prune, four doc corrections (D-02, D-14, D-15, D-16)

**Wave 2** *(blocked on 24-01)*

- [x] 24-03-PLAN.md — `_scan_adf_pages` split for branch headroom, first-page special case and unreachable `multi_scan` guard deleted, `_MAX_ADF_PAGES`, W-01 rationale corrected (D-03, D-04)

**Wave 3** *(blocked on 24-03)*

- [x] 24-04-PLAN.md — pure-white/black checks deleted, integrity skip-and-count with all-rejected raise, SCNR-08 turns green, CI runs the marker (D-05, D-06, D-08)

**Wave 4** *(blocked on 24-04)*

- [x] 24-05-PLAN.md — `scan_pages` split, options set source-first, resolution read back, `SaneDevice.resolution` typed `float`, Auto override via `classify_source`, D-01 recorded as settled (D-11, D-01)

**Wave 5** *(blocked on 24-05)*

- [x] 24-06-PLAN.md — geometry presence check with a reachable crop fallback, `GeometryUnit` total enum over all seven SANE codes, clamped-area read-back (D-09, D-10, D-19)

**Wave 6** *(blocked on 24-06)*

- [x] 24-07-PLAN.md — `ScanBatch` carries actual DPI and the integrity-skip count out of the backend; the measured 89-reference sweep; both `dpi=profile.resolution` sites redirected (D-12, D-07)

**Wave 7** *(blocked on 24-02 and 24-07)*

- [x] 24-08-PLAN.md — one `_constraint()` helper, `resolution_range` honoured by auto-profiles and the CLI, the three legacy SANE doubles deleted (D-13, D-17 completion)

Waves: 1 = {24-01, 24-02} · 2 = {24-03} · 3 = {24-04} · 4 = {24-05} · 5 = {24-06} · 6 = {24-07} · 7 = {24-08}

Three measured ordering constraints make waves 2-7 strictly sequential, and none may be collapsed:

1. **SCNR-08 is downstream of D-05.** The SANE `test` backend's default picture is solid black, so today's `_validate_page_image` discards all ten ADF pages and `scan_pages` returns `[]`. The ten-page assertion cannot pass until the pure-black check is removed. It is written RED in 24-01 and turned green by 24-04 — not scheduled in parallel.
2. **D-04's page cap is downstream of a refactor.** `_scan_adf_pages` already sits at 12 of ruff's 12 `PLR0912` branches, and `scan_pages` at 11. Adding to either breaks the build, and this project forbids suppressing the rule — so 24-03 and 24-05 each split before they add.
3. **D-12 touches the ABC.** `scan_pages` is a generator and `list()` discards `StopIteration.value`, so the read-back DPI and reject count cannot leave the backend without changing `ScannerBackend` — 89 references across 8 test files plus 3 production sites, two of which duck-type and draw no diagnostic from `ty` or `pyrefly`. 24-07 therefore lands after the facts it carries exist (24-04, 24-05, 24-06).

Note (carried from Phase 21's security audit, finding W-01 in `.planning/phases/21-vocabulary-and-contracts/21-SECURITY.md`): **`_scan_adf_pages` has no page cap.** `python-sane`'s `_SaneIterator.__next__` stops only on the exact string `"Document feeder out of documents"`, so on hardware that is not a feeder `start()`/`snap()` keep succeeding and the loop does not terminate. The per-page timeout does not bound it — a succeeding scan satisfies it every iteration — and `pipeline.py` calls `list(scanner.scan_pages(...))`, so unbounded pages means unbounded memory. This is reachable today because Phase 21 routes any `"duplex"`-named source to `multi_scan()` (D-11 AMENDED) and `scan_pages` validates the source against the device only when `has_source_option` is true. Phase 21 accepted the risk with a documentation-only control; this phase should add the iteration guard. Criterion 2 above already covers the other half — the bare `except Exception` that reports every first-page failure as "No paper detected in feeder".

### Phase 25: Manual Duplex

**Goal**: Manual duplex actually works and is honest about where it is — `duplex` is its own profile field, `source` is passed to SANE verbatim, exactly one place decides the strategy, a required `FlipCoordinator` with a timeout serves both CLI and web, and pass B is visible — with the ADF duplex how-to rewritten in-phase to stop documenting `source = "Manual Duplex"` as current
**Depends on**: Phase 24
**Requirements**: DPLX-01, DPLX-02, DPLX-03, DPLX-04, DPLX-05, DPLX-06, DPLX-07
**Success Criteria** (what must be TRUE):

  1. A legacy config with `source = "Manual Duplex"` still loads and scans, is translated to `duplex = "manual"` at config load with a deprecation warning, and `source` is never inspected for strategy anywhere in the codebase
  2. `saneless scan` with a manual-duplex profile prompts the operator on stdin and blocks until answered, then completes a two-pass scan; manual duplex with no coordinator, or with no interactive terminal, is refused before the scanner is opened
  3. A flip wait that exceeds the timeout fails the job with a clear message and releases the scanner for the next job
  4. During pass B the job reports `SCANNING_REVERSE`, Abort at the flip prompt cancels the job, and `wait_transition` no longer exists
  5. A write-then-load round trip proves auto-profiles always emits a `default` profile for flatbed-only, feeder-only, and mixed devices

**Plans**: 15 plans (9 executed in 7 waves, plus 6 gap-closure plans in 4 waves)

Plans:
**Wave 1**

- [x] 25-01-PLAN.md — Config: the `duplex` field, the legacy translation, the flip timeout (wave 1)

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 25-02-PLAN.md — Vocabulary: the ninth `JobState` and the filled projection seam (wave 2)
- [x] 25-08-PLAN.md — auto-profiles: `duplex` emission and the DPLX-07 round trip (wave 2)

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 25-03-PLAN.md — The `FlipCoordinator` contract and the bounded flip wait (wave 3)

**Wave 4** *(blocked on Wave 3 completion)*

- [x] 25-04-PLAN.md — One strategy reader, the refusal guard, the total dispatch (wave 4)

**Wave 5** *(blocked on Wave 4 completion)*

- [x] 25-05-PLAN.md — Worker and web: delete the transition-event protocol, shared job lookup (wave 5)
- [x] 25-06-PLAN.md — Scanner: feeder resolution and the no-feeder refusal (wave 5)

**Wave 6** *(blocked on Wave 5 completion)*

- [x] 25-07-PLAN.md — CLI: the flip prompt and the non-TTY refusal (wave 6)

**Wave 7** *(blocked on Wave 6 completion)*

- [x] 25-09-PLAN.md — Documentation and UI-SPEC (wave 7)

**Gap closure** (from 25-VERIFICATION.md and 25-REVIEW.md: CR-01, WR-01..WR-08, IN-01..IN-04)

**Gap Wave 1**

- [x] 25-10-PLAN.md — CR-01: flip coordinator armed at AWAITING_FLIP, signals scoped by job_id, race regressions (gap wave 1)
- [x] 25-13-PLAN.md — WR-02/WR-03/WR-04: single-sided feeder preference, no-source-option devices, whole-profile is_bare_default (gap wave 1)

**Gap Wave 2** *(blocked on Gap Wave 1 completion)*

- [x] 25-11-PLAN.md — CR-01: flip routes acknowledge a claimed answer instead of re-rendering the prompt, Playwright check (gap wave 2)
- [x] 25-12-PLAN.md — WR-01/WR-05/IN-04: bounded flip timeout, deprecation warning after logging setup (gap wave 2)

**Gap Wave 3** *(blocked on Gap Wave 2 completion)*

- [x] 25-14-PLAN.md — IN-02/WR-08/IN-01: one claim-once FlipAnswerSlot, CLI prompt failure aborts at once (gap wave 3)

**Gap Wave 4** *(blocked on Gap Wave 3 completion)*

- [x] 25-15-PLAN.md — WR-06/WR-07 and deferred-items correction: docs and UI-SPEC match the fixes (gap wave 4)

### Phase 26: Worker and Web Robustness

**Goal**: The server survives everything the pipeline can throw at it and the browser always reflects reality — a guarded worker loop, 429 backpressure that is actually visible, blocking routes declared `def`, crash recovery at startup, and a server-owned Scan button served from vendored assets that work on an offline LAN — with the deployment and API docs updated in-phase
**Depends on**: Phase 25
**Requirements**: ROBU-01, ROBU-02, ROBU-03, ROBU-04, ROBU-05, ROBU-06, ROBU-07, ROBU-08, ROBU-09, ROBU-10, ROBU-11
**Success Criteria** (what must be TRUE):

  1. A pipeline, job-store, or `prune` exception is logged with `exc_info` and the worker keeps serving the next job
  2. Submitting past a full queue returns 429 with `Retry-After` and a message the user can actually see in the status area, the event loop never blocks, and shutdown never blocks on the worker
  3. `/health` answers while a scan is running, and concurrent threadpool requests neither stampede the metadata cache nor mutate profiles mid-iteration
  4. Jobs left non-terminal by a crash are FAILED with a "server restarted" reason before the worker starts, and profiles are generated at startup from the config path that was actually loaded
  5. A browser test in CI with no CDN egress clicks Scan, waits for the terminal status, and asserts `#scan-btn` is enabled again with no duplicate `id="scan-btn"` in the DOM and `app.js` deleted

**Plans**: 19 plans (14 executed in 7 waves, plus 5 gap-closure plans in 4 waves)

**Wave 1**

- [x] 26-01-PLAN.md — Vocabulary contracts (S3 copy, WorkerHealth, SubmitResult, RequestRejection, ErrorCategory.REJECTED) and JobStore.latest_run_job / probe (wave 1)
- [x] 26-02-PLAN.md — Settings.config_path, one config search list, is_bare_default shapes (wave 1)
- [x] 26-03-PLAN.md — Vendored htmx 2.0.8 / Pico 2.1.1 with SRI, hook exclusion, pinning test (wave 1)

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 26-04-PLAN.md — Worker stop flag, bounded join, non-blocking submit, converted tests, profile lock (wave 2)
- [x] 26-05-PLAN.md — One error renderer, exception handlers, htmx-config meta, #status-message slot (wave 2)

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 26-06-PLAN.md — Guarded worker loop, degraded health, idle store probe, prune out of the job path (wave 3)
- [x] 26-07-PLAN.md — Cross-origin guard middleware (Sec-Fetch-Site + Origin fallback) (wave 3)

**Wave 4** *(blocked on Wave 3 completion)*

- [x] 26-08-PLAN.md — Startup profile generation from the loaded config path (wave 4)
- [x] 26-09-PLAN.md — Lifespan crash recovery before the worker, guarded close (wave 4)
- [x] 26-10-PLAN.md — def routes, honest /health, single-flight cache, 422/429/503 and the D-06 lookup (wave 4)

**Wave 5** *(blocked on Wave 4 completion)*

- [x] 26-11-PLAN.md — Server-owned Scan button via OOB swap, app.js deleted, title cap (wave 5)

**Wave 6** *(blocked on Wave 5 completion)*

- [x] 26-12-PLAN.md — Offline browser gate, Scan button browser proof (ROBU-11), CI browser job (wave 6)
- [x] 26-14-PLAN.md — Docs and master UI-SPEC corrected for Phase 26 (wave 6)

**Wave 7** *(blocked on Wave 6 completion)*

- [x] 26-13-PLAN.md — Request error slot browser proof (ROBU-02 visibility) and plain-HTTP LAN origin (wave 7)

**Gap closure** (from 26-VERIFICATION.md and 26-REVIEW.md: CR-01, WR-01)

**Gap Wave 1**

- [x] 26-15-PLAN.md — CR-01: owed job-store writes retried on every idle tick, corrected locked-in test, Scan button re-enables (gap wave 1)
- [x] 26-16-PLAN.md — WR-01: JobStore.create_rejected_job single statement for refused-before-row submits (gap wave 1)

**Gap Wave 2** *(blocked on Gap Wave 1 completion)*

- [x] 26-17-PLAN.md — WR-01: failed post-submit rejection write owed to the worker under a lock (gap wave 2)

**Gap closure 2** (from re-verification 26-VERIFICATION.md and re-review 26-REVIEW.md: WR-10, WR-11, IN-08)

**Gap Wave 3**

- [x] 26-18-PLAN.md — WR-10: a streak of failed owed-write retries degrades the worker so a persistent store fault reaches /health; WR-11 test race fixed (gap wave 3)

**Gap Wave 4** *(blocked on Gap Wave 3 completion)*

- [x] 26-19-PLAN.md — IN-08: an owed refused attempt is skipped by the status lookup (latest_run_job exclude_ids + owed_rejection_ids) (gap wave 4)

Note: htmx 2's default `responseHandling` does not swap 4xx bodies, so the 429 must be paired with an explicit `htmx-config` override or it is invisible — reintroducing the exact C-10 symptom this phase fixes. Worker tests that assumed a draining `stop()` are converted to a `wait_for_state` polling helper here, not in Phase 32.

Note (added in 23.1, DARK-03 coupling): when ROBU-09 vendors Pico, follow `23.1-UI-SPEC.md` § "Coupling contract for Phase 26 (ROBU-09)": vendor `pico.min.css` 2.1.1 only; do not vendor `pico.colors.css`; reintroduce no `data-theme` on `<html>`; keep the `--saneless-status-fallback` block; and move the DARK-01/DARK-02 browser tests into CI once the assets are local.

**UI hint**: yes

### Phase 27: Configuration Strictness

**Goal**: A wrong config is caught at load with a message that names the right place, and a config rewrite is durable on the deployment the docs recommend — nested `extra="forbid"` with full-`loc` error rendering, atomic UTF-8 comment-preserving writes, `~`/XDG expansion, validated log level, `SecretStr` token — with the configuration reference and compose example updated in-phase
**Depends on**: Phase 26
**Requirements**: CFG-01, CFG-02, CFG-03, CFG-04, CFG-05, CFG-06, CFG-07, CFG-08, CFG-09, CFG-10, CFG-11
**Success Criteria** (what must be TRUE):

  1. A typo'd key under `[paperless]` is rejected at load with a message naming `paperless`, the bad key, and the valid keys; a `--config` path that does not exist exits 2 naming the path
  2. `~` and `$XDG_CONFIG_HOME`/`$XDG_STATE_HOME` are honoured for config and data locations, an invalid `log_level` is rejected, and `-v` sets the effective level to DEBUG
  3. `auto-profiles --force` succeeds against the documented Docker Compose mount, replaces only the keys it generates, and leaves `default_tags` and hand-written profiles untouched
  4. The Paperless token never appears in `repr(settings)`, logs, or error messages, while the loaded config path and the env-sourced keys are logged at INFO on startup
  5. `saneless <subcommand> --help` works with no valid configuration file, and a blank title falls back to the profile's documented `title` key

**Plans**: 8 plans

Plans:

**Wave 1**

- [x] 27-01-PLAN.md — (wave 1) SecretStr token, validated `log_level`, literal `default_title` + shared `resolve_job_title` wired into the web route
- [x] 27-02-PLAN.md — (wave 1) `atomic_write.replace_file_atomically`: same-dir mkstemp, fsync, rename, mode/owner copy, symlink write-through, EBUSY -> ConfigError

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 27-03-PLAN.md — (wave 2) nested `extra="forbid"`, loc/msg error renderer with did-you-mean, env attribution, unknown `SANELESS_*` rejection, CFG-02, CFG-11 functions
- [x] 27-04-PLAN.md — (wave 2) `auto-profiles --force` merge with `ProfileWriteResult`, UTF-8/CRLF/tomllib-guarded atomic rewrite, EBUSY exit 2 and worker fallback
- [x] 27-05-PLAN.md — (wave 2) `./config:/etc/saneless` directory mount in compose and Docker docs, profile how-to merge/title text, static deployment test

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 27-06-PLAN.md — (wave 3) lazy memoised CLI settings (`--help` without config), `-v` = saneless DEBUG, CFG-11 startup line, optional `--title`
- [x] 27-07-PLAN.md — (wave 3) `$XDG_CONFIG_HOME`/`$XDG_STATE_HOME` at call time, `~` expansion, nearest-ancestor writability, conftest XDG hygiene
- [x] 27-08-PLAN.md — (wave 3) configuration/env/CLI references, scripting how-to, empty-page tip, search-path lists, TOML example, doc-truth tests

Note: CFG-08 (atomic write) and CFG-09 (mount the config directory) must ship together. `os.replace` over a bind-mounted *file* returns `EBUSY`, so shipping the atomic write alone delivers a durable-write feature that is broken for the documented deployment.

### Phase 28: Exception Translation

**Goal**: No third-party exception type escapes a module boundary and no user ever sees a traceback — SANE, httpx, all seven img2pdf error classes, and tomllib errors are wrapped at their call sites with their original messages, and the CLI prints one line with a non-zero exit code — with the troubleshooting docs updated in-phase
**Depends on**: Phase 27
**Requirements**: EXC-01, EXC-02, EXC-03, EXC-04, EXC-05
**Success Criteria** (what must be TRUE):

  1. A parametrised test per third-party library proves each boundary raises the saneless exception type carrying the original message, never the third-party type
  2. `saneless scan` against a bad config, a broken scanner, an unreachable Paperless, and an unassemblable PDF each print one line and exit non-zero; a missing `python-sane` import prints an install hint
  3. A scan that produces zero pages says "No pages were scanned", and says "All pages were blank" only when detection actually removed them — never a bare `ValueError`
  4. A user abort at the flip prompt is recorded as a cancelled job, not a scanner failure, and every job failure is logged with `exc_info`

**Plans**: 14 plans

Plans:

**Wave 1**

- [x] 28-01-PLAN.md — PdfError, ScanCancelledError, describe(), ErrorCategory.ASSEMBLY, ExitCode, JobState.CANCELLED + status/history branches
- [x] 28-02-PLAN.md — TOML syntax / unreadable / non-UTF-8 config and tomlkit parse failures become ConfigError under the header
- [x] 28-03-PLAN.md — "No pages were scanned" precondition and "All pages were blank"

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 28-04-PLAN.md — assemble_pdf boundary: every img2pdf/Pillow failure becomes PdfError
- [x] 28-05-PLAN.md — SANE boundary: require_sane() and every python-sane call site wrapped as ScanError
- [x] 28-06-PLAN.md — Paperless upload boundary: ctor InvalidURL, widened retry set, fast-fail bad URL, one body renderer
- [x] 28-07-PLAN.md — Worker's three job endings (shutdown, CANCELLED, failure with exc_info)
- [x] 28-08-PLAN.md — CANCELLED muted styling proven with Playwright; UI-SPEC rows
- [x] 28-09-PLAN.md — CLI guarded group: one line + D-07 exit code for every failure, exit 5 last resort, Ctrl-C 130

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 28-10-PLAN.md — Paperless poll survives transport errors to its deadline; duplicate hint; wrapped tag/correspondent fetches
- [x] 28-11-PLAN.md — Flip-prompt abort classified as cancel end to end (abort_cause, 130, web CANCELLED)

**Wave 4** *(blocked on Wave 3 completion)*

- [x] 28-12-PLAN.md — require_sane() in the four SANE commands; serve bind/startup failures exit 2
- [x] 28-14-PLAN.md — CANCELLED, abort, retry/fallback and zero-page wording corrected across explanation docs

**Wave 5** *(blocked on Wave 4 completion)*

- [x] 28-13-PLAN.md — Exit-code tables + doc-truth tests, troubleshoot-a-failed-scan how-to, nav and cross-links

**Cross-cutting constraints:**

- `uv run mkdocs build --strict` succeeds

### Phase 29: Geometry, Memory, and Timeouts

**Goal**: A long scan is ordered, bounded in memory, and cancellable without wedging the process — pages spooled to disk with explicit ordered records, a shared ADF/flatbed timeout that waits for the cancelled read, and `sane.init()`/`sane.exit()` guarded as process-global — with the architecture explanation page updated in-phase
**Depends on**: Phase 28
**Requirements**: HARD-01, HARD-02, HARD-03, HARD-04, HARD-05
**Success Criteria** (what must be TRUE):

  1. A 12-page scan with distinct per-page content comes out in order 1..12, duplex interleave reorders the page records rather than the filesystem, and peak memory stays bounded by roughly one page
  2. A mid-batch scanner error after N pages keeps those N pages and reports the error with the count
  3. A fake with a blocking read proves `close()` is never called while the read is blocked, and the process still exits — a stuck read never blocks `docker stop` or `pytest`
  4. The flatbed path enforces the same timeout and image validation as the ADF path
  5. `sane.init()` runs once per process behind a re-entry guard, `sane.exit()` runs at shutdown, and neither is reachable from a request path

**Plans**: 11 plans in 8 waves

Plans:
**Wave 1**

- [x] 29-01-PLAN.md — Page spool and the PageRecord/PageSink contract (additive; tree stays green)

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 29-02-PLAN.md — Contract switch: ScanBatch records, scan_pages(sink), close(), conftest seam

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 29-03-PLAN.md — Records through pages.py, pdf.py and pipeline.py; spool lifetime (src green)

**Wave 4** *(blocked on Wave 3 completion)*

- [x] 29-04-PLAN.md — Migrate tests/test_scanner.py + the weakref memory-bound proof
- [x] 29-05-PLAN.md — Migrate pipeline/pdf/pages/e2e suites + the twelve-page and interleave proofs

**Wave 5** *(blocked on Wave 4 completion)*

- [x] 29-06-PLAN.md — Migrate the eight remaining stub scanners; close the red interval

**Wave 6** *(blocked on Wave 5 completion)*

- [x] 29-07-PLAN.md — Daemon-thread timeout, cancel-then-wait, wedge, flatbed, Ctrl-C, exit proof
- [x] 29-08-PLAN.md — Bounded PDF assembly: per-page convert plus a qpdf merge
- [x] 29-09-PLAN.md — Keep the pages a mid-batch failure leaves behind (HARD-02)

**Wave 7** *(blocked on Wave 6 completion)*

- [x] 29-10-PLAN.md — SANE init guard, shutdown at every entry point, the request-path proof

**Wave 8** *(blocked on Wave 7 completion)*

- [x] 29-11-PLAN.md — Documentation (D-20), the doc-truth test, and the phase gate

### Phase 30: Appliance Layer

**Goal**: A non-technical household member can tell at a glance whether the appliance is healthy and what a failure means — one shared check list behind both `saneless doctor` and a cached status strip, page counts on every terminal job, plain-language errors with a next step, human profile labels, queue position, and an owner-only flip prompt — with help text and the docs for each new surface written in-phase
**Depends on**: Phase 29
**Requirements**: APPL-01, APPL-02, APPL-03, APPL-04, APPL-05, APPL-06, APPL-07, APPL-08, APPL-09, APPL-10, APPL-11, APPL-12
**Success Criteria** (what must be TRUE):

  1. `saneless doctor` runs the shared checks (scanner, Paperless, profiles, fallback, data dir) and exits non-zero on a placeholder token or any other red check; the index page shows the same checks, refreshed on load and by a button
  2. The status strip stays fast with the scanner host unplugged and is skipped entirely while a scan is active, so it never contends with the exclusive scanner
  3. Every terminal job shows pages scanned, pages removed as blank, and pages uploaded; manual duplex shows front and back counts during pass B; a queued job says "Waiting for '<title>' to finish (N ahead of you)"
  4. Every user-facing error shows a plain-language message and a suggested next step, with the raw technical detail inside a collapsed disclosure
  5. Two browser contexts show the owner the Continue/Abort flip prompt (with confirmation on Abort) and the non-owner "Waiting for the stack to be flipped"; profile dropdowns show human labels with descriptions, feeder-first on sheet-fed scanners, and say so on the strip when the config mount is read-only

**Plans**: 19 plans

Plans:
**Wave 1**

- [ ] 30-01-PLAN.md — Vocabulary: error advice with next steps, the shared local-time formatter, page counts, the busy line, the token rejection member
- [ ] 30-02-PLAN.md — Config: the placeholder-token predicate, profile label/description fields, the `[web]` section
- [ ] 30-03-PLAN.md — Job store: the owner_token writer and queue_position

**Wave 2** *(blocked on Wave 1 completion)*

- [ ] 30-04-PLAN.md — Pipeline pass-count channel; worker scanner gate, profile storage and front pages
- [ ] 30-05-PLAN.md — Auto-profiles: generated label and description as tool-owned keys

**Wave 3** *(blocked on Wave 2 completion)*

- [ ] 30-06-PLAN.md — checks.py: the shared three-state registry and the bounded Paperless probe

**Wave 4** *(blocked on Wave 3 completion)*

- [ ] 30-07-PLAN.md — Check cache with an injected clock, and the lazy refresher thread
- [ ] 30-08-PLAN.md — `saneless doctor` and its CLI reference section

**Wave 5** *(blocked on Wave 4 completion)*

- [ ] 30-09-PLAN.md — App composition: Jinja filters, app.state, dual-thread lifespan shutdown
- [ ] 30-10-PLAN.md — CLI: the `Try:` advice line, `scan`'s token refusal, the local-time jobs table

**Wave 6** *(blocked on Wave 5 completion)*

- [ ] 30-11-PLAN.md — Status strip: routes, partials, cold-start poll, paused during scan

**Wave 7** *(blocked on Wave 6 completion)*

- [ ] 30-12-PLAN.md — Plain-language errors, page counts and the local Time cell in the templates

**Wave 8** *(blocked on Wave 7 completion)*

- [ ] 30-13-PLAN.md — Owner cookie, owner-gated flip prompt, queue position, Abort confirmation

**Wave 9** *(blocked on Wave 8 completion)*

- [ ] 30-14-PLAN.md — Placeholder-token refusal in the web layer and the disabled Scan button

**Wave 10** *(blocked on Wave 9 completion)*

- [ ] 30-15-PLAN.md — Profile select with a live description and feeder-first ordering

**Wave 11** *(blocked on Wave 10 completion)*

- [ ] 30-16-PLAN.md — Form help text, thumb-friendly tag picker with filter, the simpler form

**Wave 12** *(blocked on Wave 11 completion)*

- [ ] 30-17-PLAN.md — Browser verification part 1: egress gate, strip, errors, counts, timestamps
- [ ] 30-18-PLAN.md — Compose template, reference docs and master UI-SPEC accuracy

**Wave 13** *(blocked on Wave 12 completion)*

- [ ] 30-19-PLAN.md — Browser verification part 2: two contexts, tag picker, blocked Scan button

**UI hint**: yes

### Phase 31: Delivery, Identity, and Documentation Accuracy

**Goal**: The project ships under its real name with a release path proven end to end and documentation that does not lie — `kdknigga/saneless` everywhere behind a CI grep guard, SHA-pinned actions with scoped permissions, container logging/port/user/`.dockerignore` fixes, and every one of review section 8's 34 false claims corrected as this milestone's final documentation audit
**Depends on**: Phase 30
**Requirements**: CI-02, DLVR-01, DLVR-02, DLVR-03, DLVR-04, DLVR-05, DLVR-06, DLVR-07, DLVR-08, DLVR-09, DLVR-10, DOCS-01, DOCS-02, DOCS-03, DOCS-04, DOCS-05, DOCS-06
**Success Criteria** (what must be TRUE):

  1. No shipped file references `kris-knigga/saneless`, `kris-knigga.github.io/saneless`, or `ghcr.io/kris-knigga/saneless`, and CI fails if one reappears (excluding `.planning/` and `site/`); the PyPI distribution name stays `saneless`
  2. A pre-release tag runs the entire release workflow green end to end, verified by an actual `pip install` and `docker pull` from a clean machine — not by reading the workflow file
  3. Container logs appear in `docker logs`, the example config / `EXPOSE` / `HEALTHCHECK` agree on one port, the container runs non-root from digest-pinned bases with a `WORKDIR`, and a `.dockerignore` allow-list keeps secrets, `.planning/`, and tests out of the build context
  4. All actions are SHA-pinned with Dependabot, every job has a `permissions:` block, a zizmor audit runs in CI, the wheel carries the LICENSE via PEP 639, and `saneless --version` prints the installed version
  5. A reader following README and the docs site hits no false claim: every row of review section 8 is either corrected or the behaviour now matches, `saneless scan` examples run as written, and the new "Which setup do I have?" and trust-model pages resolve from the quick-start prerequisites

**Plans**: TBD

### Phase 32: Suite Hygiene and Minor Sweep

**Goal**: The test suite is hermetic, fast, and meaningful, and the last correctness nits are gone — isolated `HOME`/`XDG`/cwd, no `time.sleep`, no assertion-free or duplicate tests, and the remaining N-01..N-45 sweep including the final one-implementation-each audit — with any doc sentence touched by a sweep item updated in-phase
**Depends on**: Phase 31
**Requirements**: TEST-01, TEST-02, TEST-03, TEST-04, TEST-05, TEST-06, SWP-01, SWP-02, SWP-03, SWP-04, SWP-05, SWP-06, SWP-07, SWP-08, SWP-09, SWP-10, SWP-11, SWP-12, SWP-13, SWP-14
**Success Criteria** (what must be TRUE):

  1. The suite passes with `HOME` pointed at an empty directory and the working directory isolated, and no `time.sleep` remains anywhere in `tests/`
  2. A `pytest --cov` line diff proves no coverage was lost by the tests removed, and scanner and CLI tests assert what their names and docstrings claim
  3. The data-loss and negative-path tests all exist and pass: upload failure preserves the PDF, Paperless FAILURE maps to FAILED, the worker survives a raising `prune`, two-thread store access is clean, and the flip timeout fails the job
  4. No `# noqa` or `# type: ignore` remains in `src/` or `tests/`, `MAX_IMAGE_PIXELS` is set in one place, `configure_logging` is idempotent, and the job-db-path / slug-rule / active-state / label-map duplication inventory has one implementation each
  5. `devices --json --capabilities` pipes cleanly to `jq`, `serve` handles IPv6 hosts and `--port 0`, the metadata cache serves stale data on error with the cause logged, and no comment in `src/` cites a planning artefact

**Plans**: TBD

### Phase 33: Disable the OpenAPI Schema and Docs Endpoints

**Goal**: A LAN-exposed saneless stops advertising an API surface it does not have -- `GET /openapi.json`, `/docs` and `/redoc` are removed rather than returning 500, the route-reachability proof stops skipping the schema path, and the decision is written down where a contributor will meet it
**Depends on**: Nothing -- independent of Phases 30-32; sequenced last only by convention
**Requirements**: API-01
**Success Criteria** (what must be TRUE):

  1. `GET /openapi.json`, `GET /docs` and `GET /redoc` each answer 404 on a running app -- no 500, no partial schema, no interactive shell -- proven by a test that drives all three
  2. `_ROUTE_SKIPS` in `tests/test_app_lifespan.py` no longer carries an `/openapi.json` entry, and the every-route reachability assertion (`uncovered == set()`) still passes, so the skip is removed rather than widened
  3. No shipped file -- docs, README, or test -- claims saneless serves an OpenAPI schema or interactive API documentation
  4. The reason is recorded in `docs/reference/web-api.md`: this is an unauthenticated LAN appliance serving an HTMX UI, the generated schema never worked, and nothing consumes it
  5. Phase 29's `deferred-items.md` entry is closed with a pointer to this phase, so the finding is not rediscovered a third time

**Plans**: TBD

## Progress

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 20. CI Gate | 5/5 | Complete    | 2026-09-10 |
| 21. Vocabulary and Contracts | 5/5 | Complete   | 2026-09-10 |
| 22. Job Store Hardening | 6/6 | Complete   | 2026-09-10 |
| 23. Honest Outcomes and Never Lose a Scan | 9/9 | Complete   | 2026-09-11 |
| 24. Scanner Truthfulness | 8/8 | Complete   | 2026-09-13 |
| 25. Manual Duplex | 15/15 | Complete    | 2026-09-14 |
| 26. Worker and Web Robustness | 19/19 | Complete    | 2026-09-15 |
| 27. Configuration Strictness | 8/8 | Complete    | 2026-09-15 |
| 28. Exception Translation | 14/14 | Complete    | 2026-09-15 |
| 29. Geometry, Memory, and Timeouts | 11/11 | Complete   | 2026-09-16 |
| 30. Appliance Layer | 0/? | Not started | - |
| 31. Delivery, Identity, and Documentation Accuracy | 0/? | Not started | - |
| 32. Suite Hygiene and Minor Sweep | 0/? | Not started | - |
| 33. Disable the OpenAPI Schema and Docs Endpoints | 0/? | Not started | - |

## Research Flags

Phases worth a `--research-phase` pass during planning (from `.planning/research/SUMMARY.md`):

| Phase | Why |
|-------|-----|
| 26 | The single-flight cache lock and the exact `htmx-config` `responseHandling` JSON shape are narrow but easy to get subtly wrong — verify against the installed htmx 2.0.10 before writing the browser test |
| 27 | The "unknown top-level env var ignored, unknown nested env var rejected" asymmetry is MEDIUM confidence; the bind-mount `EBUSY` behaviour needs a throwaway `docker run` to confirm |
| 30 | The owner-token cookie mechanism (lifetime, reload behaviour, fail-open escape hatch) is genuinely novel here and should be prototyped small |

Safe to skip research: Phase 20 (mechanical), Phase 21 (pure refactor, fully specified), Phase 28 (mechanical wrapping at identified call sites).

## Coverage

All 126 v2.0 requirements are mapped to exactly one phase. See the Traceability table in `.planning/REQUIREMENTS.md`.

---
*Roadmap created: 2026-09-09*
