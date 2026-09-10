# Architecture Research

**Domain:** Single-node scanning appliance (SANE → PDF → paperless-ngx) — hardening an existing Python 3.14 app for release
**Researched:** 2026-09-09
**Confidence:** HIGH (every claim below was verified by reading the shipped source in `src/saneless/` and the review at `.planning/reviews/2026-09-09-code-review.md`; library facts verified against the installed venv)

**Scope note.** This is *not* a redesign. The existing module graph is sound — the review's own verdict is "the codebase is not under-engineered; it is under-connected. The pieces are good and the joints need attention." This document therefore answers one question: **where does each new v2.0 piece attach to the existing graph, and in what order can they be attached without ever leaving the suite red?**

---

## Standard Architecture

### System Overview — target state after v2.0

```
┌──────────────────────────────────────────────────────────────────────────┐
│  ENTRY POINTS  (own global state: logging, settings, lifespan)           │
│  ┌────────────────────────┐          ┌──────────────────────────────┐    │
│  │ cli.py (Click)         │          │ web/app.py (FastAPI lifespan)│    │
│  │  scan · devices · jobs │          │  create_app()                │    │
│  │  auto-profiles · serve │          │  + startup reconcile (M-03)  │    │
│  │  + doctor      (U-03)  │          │  + startup profiles (M-04)   │    │
│  └───────┬────────────────┘          └──────┬───────────────────────┘    │
│          │                                   │ web/routes.py (def, M-01) │
│          │                                   │ + /api/status  (U-03)     │
├──────────┼───────────────────────────────────┼───────────────────────────┤
│  ORCHESTRATION                               │                           │
│          │                            ┌──────▼──────────┐                │
│          │                            │ worker.py       │  thread + queue│
│          │                            │  loop guard C-09│                │
│          │                            └──────┬──────────┘                │
│          └──────────────┬────────────────────┘                           │
│                  ┌──────▼─────────────────────────────┐                  │
│                  │ pipeline.py  run_pipeline()        │                  │
│                  │   → returns ScanResult (C-03)      │                  │
│                  └──┬──────────┬───────────┬──────────┘                  │
├─────────────────────┼──────────┼───────────┼─────────────────────────────┤
│  DOMAIN SERVICES    │          │           │                             │
│   ┌─────────────┐ ┌─▼───────┐ ┌▼────────┐ ┌▼──────────────┐ ┌──────────┐ │
│   │ scanner/    │ │pages.py │ │ pdf.py  │ │ delivery.py ★ │ │doctor.py★│ │
│   │  base.py    │ │+PageSpl★│ │ paths→  │ │ upload→fall-  │ │ checks   │ │
│   │  +classify★ │ │ (M-08)  │ │ PDF+DPI │ │ back→failed/  │ │ (U-03)   │ │
│   │  sane_bknd  │ └─────────┘ └─────────┘ └───┬───────────┘ └──────────┘ │
│   └──────┬──────┘                             │ paperless.py (pure HTTP) │
├──────────┼────────────────────────────────────┼──────────────────────────┤
│  PERSISTENCE / VOCABULARY (leaf layer — imports nothing from saneless)   │
│   ┌──────▼──────┐ ┌──────────┐ ┌───────────┐ ┌──────────┐ ┌────────────┐ │
│   │ config.py   │ │ job.py   │ │ states.py★│ │results.py│ │exceptions  │ │
│   │ +duplex     │ │ JobStore │ │ JobState  │ │ScanResult│ │ +PdfError  │ │
│   │ +label/desc │ │ +RLock   │ │ LABELS    │ │Outcome   │ │ +WorkerBusy│ │
│   │ +config_path│ │ +columns │ │ MESSAGES  │ │Upload    │ │ +FlipTimeut│ │
│   └─────────────┘ └──────────┘ └───────────┘ └──────────┘ └────────────┘ │
│                        ★ = new module in v2.0                            │
└──────────────────────────────────────────────────────────────────────────┘

  EXTERNAL:  saned / libsane (python-sane)    paperless-ngx REST + consume dir
```

**The one structural rule this milestone must establish:** dependencies point downward only. The vocabulary layer (`states.py`, `results.py`, `exceptions.py`) imports nothing from `saneless`; every other module may import it. This is what makes "one enum, one label map, one classifier" (M-05, C-06) mechanically enforceable rather than a convention people re-violate.

### Component Responsibilities — new and changed

| Component | Status | Owns | Resolves |
|-----------|--------|------|----------|
| `states.py` | **NEW** (leaf) | The single `JobState` enum (adds `SCANNING_REVERSE`, `FALLBACK`), `ErrorCategory` (moved from `job.py`), `ACTIVE_STATES: frozenset`, `STATE_LABELS: dict[JobState, str]`, `ERROR_MESSAGES: dict[ErrorCategory, UserMessage]` | M-05, C-03, M-02, U-05 |
| `results.py` | **NEW** (leaf) | `ScanOutcome` (`SUCCESS` / `FALLBACK` / `PARTIAL` / `FAILED`), `ScanResult` dataclass, `UploadResult` dataclass | C-03, U-02 |
| `delivery.py` | **NEW** | The "never lose a scan" policy: try upload → consume-dir fallback (atomic, unique name) → preserve under `failed/`. Returns `UploadResult`. | C-04, C-05, U-08, doc row 25 |
| `flip.py` | **NEW** | `FlipCoordinator` Protocol + `FlipDecision` enum + `EventFlipCoordinator` (threading impl with timeout and owner token) | C-01, C-02, M-07, U-06 |
| `doctor.py` | **NEW** | `Check` dataclass and `run_checks(settings, …) -> list[Check]`: scanner reachable, paperless token, profiles, consume/failed dirs, config path, placeholder-token detection | U-03, U-01, U-08 |
| `scanner/base.py` | MODIFIED | Gains `SourceKind` enum + `classify_source(name)`. Becomes the single authority on "is this a feeder?" | C-06 |
| `pipeline.py` | MODIFIED | Returns `ScanResult`; branches on `profile.duplex`; delegates delivery; emits `JobState` (not `PipelineEvent`); takes a `FlipCoordinator` | C-01, C-02, C-03, M-05, M-08 |
| `paperless.py` | MODIFIED | Shrinks to pure HTTP: `upload_document` returns a task id or raises; `poll_task` raises on FAILURE/TIMEOUT. No `consume_dir`, no `"fallback"` string. | C-03, C-05, M-22, M-17 |
| `pdf.py` | MODIFIED | `assemble_pdf(pages: Sequence[Path], output_path: Path, dpi: int) -> Path` — caller-supplied name, caller-supplied DPI, path input | C-05, M-06, M-08 |
| `job.py` | MODIFIED | `RLock` on every method; `PRAGMA user_version` migration ladder; new columns; `fail_active_jobs()`, `list_pending()` | C-07, M-03, U-02, U-06 |
| `worker.py` | MODIFIED | Guarded loop; `put_nowait` + `WorkerBusyError`; stop flag instead of sentinel; writes `ScanResult` fields to the store; owns the `EventFlipCoordinator` | C-07, C-09, M-03 |
| `web/routes.py` | MODIFIED | `def` not `async def`; 429/503 mapping; `/api/status`; owner-token flip gating | M-01, C-09, U-03, U-06 |
| `web/app.py` | MODIFIED | Lifespan does reconcile → generate profiles → start worker; `humanize_state` delegates to `STATE_LABELS`; shutdown checks `join()` | M-03, M-04, M-05 |
| `config.py` | MODIFIED | `duplex`, `label`, `description` on `ProfileConfig`; `extra="forbid"`; `Settings.config_path`; `output.data_dir`; `SecretStr` | C-01, U-04, M-18, M-04 |
| `pages.py` | MODIFIED | Gains `PageSpool` — writes each page to disk on arrival, records stats + thumbnail, yields paths | M-08 |
| `cli.py` | MODIFIED | `doctor` command; real flip prompt via `CliFlipCoordinator`; uses `STATE_LABELS`; prints `ScanResult` counts | C-02, U-02, U-03, M-05 |
| `web/templates/` | MODIFIED | `_scan_button.html` include with `hx-swap-oob`; status strip partial; page counts; friendly errors; queue position | C-10, U-02, U-03, U-05, U-06 |
| `web/static/app.js` | **DELETED** | The button-state logic moves to the server | C-10 |

---

## Recommended Project Structure

```
src/saneless/
├── states.py          ★ NEW  vocabulary: JobState, ErrorCategory, labels, messages
├── results.py         ★ NEW  typed outcomes: ScanOutcome, ScanResult, UploadResult
├── exceptions.py         MOD  + PdfError, WorkerBusyError, WorkerUnavailableError,
│                              FlipTimeoutError
├── config.py             MOD  + duplex/label/description, extra=forbid, config_path,
│                              output.data_dir
├── job.py                MOD  JobStore only (enums moved to states.py); RLock;
│                              user_version migrations; new columns
├── flip.py            ★ NEW  FlipCoordinator protocol + EventFlipCoordinator
├── delivery.py        ★ NEW  upload → consume-dir → failed/ policy
├── doctor.py          ★ NEW  Check list shared by CLI, web strip, docs
├── pipeline.py           MOD  returns ScanResult; duplex branch; spool
├── pages.py              MOD  + PageSpool
├── pdf.py                MOD  paths in, name + dpi from caller
├── paperless.py          MOD  pure HTTP; raises on FAILURE/TIMEOUT
├── worker.py             MOD  guarded loop, backpressure, ScanResult persistence
├── auto_profiles.py      MOD  classify_source; always emit default; in-place --force;
│                              ensure_profiles() called at startup
├── scanner/
│   ├── base.py           MOD  + SourceKind, classify_source()
│   └── sane_backend.py   MOD  classify_source; drop white-page policy; error wrapping
└── web/
    ├── app.py            MOD  lifespan ordering; label filter delegates to states.py
    ├── routes.py         MOD  sync handlers; /api/status; 429/503
    ├── static/app.js     DEL
    └── templates/
        ├── partials/_scan_button.html   ★ NEW (single source, hx-swap-oob)
        └── partials/status_strip.html   ★ NEW (doctor output)
```

### Structure Rationale

- **Two new leaf modules instead of one:** `states.py` is what the *user interface* must agree on (labels, messages, active-state sets); `results.py` is what the *pipeline* produces. Keeping them separate means `cli jobs` and the history template can import `states.py` without pulling `pathlib`-heavy result types, and the pipeline can import `results.py` without knowing that jobs exist.
- **Why not put `JobState` in `job.py` and be done?** It works — `job.py` is stdlib-only, so `pipeline.py → job.py` is acyclic and cheap. The cost is that the domain core imports the persistence module for a name, which is the exact coupling that makes a future JSON API or second store painful. The extra file is 60 lines. **Recommendation: create `states.py`, re-export `JobState`/`ErrorCategory` from `job.py` so the ~40 existing test imports keep working.** The re-export is what lets this land as a zero-behaviour-change step.
- **Why `delivery.py` rather than fixing `paperless.py` in place?** Today `PaperlessClient` knows about `consume_dir` and returns the magic string `"fallback"` (C-03), while `pipeline.py` owns the `TemporaryDirectory` whose deletion destroys the PDF (C-04). Neither module can fix the other's half. A third module that owns *"where does this PDF end up"* gives the policy one home, makes it unit-testable without a scanner, and shrinks the HTTP client to something whose failure modes are obvious. It also fixes doc row 25 for free (4xx currently bypasses the fallback because the branch is inside the retry loop).
- **`classify_source` in `scanner/base.py`, not a new module:** `base.py` has no runtime third-party imports (PIL is `TYPE_CHECKING` only) and `scanner/__init__.py` lazy-imports `SaneBackend`, so `auto_profiles.py` and `pipeline.py` can import the classifier without requiring `python-sane` to be installed. Verified: the lazy `__getattr__` in `scanner/__init__.py` already guarantees this.

---

## Architectural Patterns

### Pattern 1: Typed result flowing through four layers (C-03, U-02)

**What:** `run_pipeline` returns a `ScanResult`; the worker persists it; the templates and the CLI render it. No caller may "forget to look" because there is nothing optional to look at.

**When to use:** Any time an operation has more than two outcomes and more than one caller.

**Trade-offs:** Adds five database columns and a migration. In exchange, the "green tick on a failed upload" bug class becomes unrepresentable — and U-02 ("Scanned 10, removed 2, uploaded 8") becomes two template lines instead of a feature.

```python
# results.py — leaf, imports nothing from saneless
class ScanOutcome(StrEnum):
    SUCCESS  = "SUCCESS"   # paperless task reached SUCCESS
    FALLBACK = "FALLBACK"  # written to the consume directory
    PARTIAL  = "PARTIAL"   # duplex mismatch: two half documents uploaded
    FAILED   = "FAILED"    # preserved under failed/, nothing in paperless

@dataclass(frozen=True)
class ScanResult:
    outcome: ScanOutcome
    pages_scanned: int
    pages_removed: int
    pages_uploaded: int
    task_id: str | None = None
    kept_path: Path | None = None     # consume-dir or failed/ location
    warning: str | None = None        # duplex mismatch text, etc.
```

**Flow contract:** `pipeline → worker → JobStore → status.html / history.html / cli jobs`. The worker's success branch becomes
`self._job_store.finish_job(job.id, result)` — one call that writes state *and* counts *and* warning atomically under the store lock, so a browser poll can never observe `DONE` with null counts.

### Pattern 2: One collaborator object instead of two optional events (C-02, M-07, U-06)

**What:** Replace `PipelineRequest.flip_event` / `.abort_event` (both `| None`, both silently changing semantics) with a single required-when-manual-duplex `FlipCoordinator`.

**When to use:** When two entry points share an orchestrator and one of them keeps forgetting to supply a collaborator. This is precisely the C-02 failure: the CLI built a `PipelineRequest` without a flip event and pass B ran back to back.

```python
# flip.py
class FlipDecision(StrEnum):
    CONTINUE = "CONTINUE"
    ABORT    = "ABORT"
    TIMEOUT  = "TIMEOUT"

class FlipCoordinator(Protocol):
    def request_flip(self, timeout: float) -> FlipDecision: ...

# pipeline.py — the contract cannot be forgotten
if profile.duplex == "manual" and request.flip is None:
    raise ConfigError("Manual duplex requires a flip coordinator")
```

- **CLI implementation:** blocks on `click.confirm` on the pipeline's own thread (the CLI runs `run_pipeline` synchronously, so this is correct and needs no events at all).
- **Web implementation:** `EventFlipCoordinator` wraps the two `threading.Event`s the worker already creates, adds `settings.output.flip_timeout_seconds` (M-07), and carries the `owner_token` that `routes.continue_flip` must present (U-06).

**Trade-off:** the pipeline gains a Protocol import. The payoff is that `TIMEOUT` becomes a first-class outcome the pipeline can treat like an abort *while preserving the fronts* (C-04), which the current bare `Event.wait()` cannot express.

### Pattern 3: Server-owned UI state via out-of-band swap (C-10, M-05)

**What:** Delete `app.js`. Move the Scan button into `partials/_scan_button.html`, included by `index.html` and re-emitted by `partials/status.html` with `hx-swap-oob="true"`. Every status poll therefore re-renders the button from the same server-side truth.

**Why this is architecture, not a bug fix:** today the button rule exists twice — once in `index.html`'s inline `{% if job.state.value in [...] %}` and once in `app.js`'s `afterSwap` handler — and the JavaScript copy reads `evt.detail.target`, which htmx 2.x has already detached for `outerHTML` swaps. Two sources of truth, one wrong. Combined with M-05's third copy of the active-state list inside `status.html`, the same rule is written three times in three languages.

**Fix shape:** pass `ACTIVE_STATES` from `states.py` into the template context (via a Jinja global set in `create_app`), delete all three hardcoded lists, and render the button once.

### Pattern 4: Spool-as-you-scan (M-08)

**What:** `list(scanner.scan_pages(...))` becomes a `PageSpool` that consumes the generator, writes each page as PNG into the job's temp directory, records `(path, mean, stddev, size)` per page, and generates the thumbnail from page 1.

**Why it must come *after* the `pdf.py` signature change:** `assemble_pdf` currently takes `list[Image.Image]`. C-05 needs a caller-supplied filename and M-06 needs a DPI. Change the signature once — to `assemble_pdf(pages: Sequence[Path], output_path: Path, dpi: int)` — and the spool becomes a pipeline-internal change with no further churn in `pdf.py` or `tests/test_pdf.py`.

**Verified library facts:** `img2pdf.convert(images, outputstream, **kwargs)` accepts an `outputstream` (so the PDF streams to disk rather than materialising `pdf_bytes`), and `img2pdf.get_fixed_dpi_layout_fun(fixed_dpi)` exists for the M-06 geometry fix. Both confirmed against the installed `img2pdf` in this venv.

**Interaction with empty-page detection:** `filter_empty_pages(images, …)` becomes `select_pages(spooled: Sequence[SpooledPage], …) -> list[SpooledPage]` operating on already-computed statistics. This also removes the duplicated greyscale conversion the review flags in M-08.

### Pattern 5: Diagnostics as data, rendered three ways (U-03)

**What:** `doctor.run_checks()` returns `list[Check]`. `saneless doctor` prints them and exits non-zero on any FAIL; `/api/status` renders `partials/status_strip.html` from the same list; the docs quote the same check names.

**Two integration hazards that must be designed for, not discovered:**

1. **The scanner is exclusive hardware.** A doctor check that calls `get_devices()` while the worker is mid-scan can block or fail. `run_checks` must accept the worker's busy state and return `Check(status=SKIPPED, summary="scan in progress")` rather than probing. This is why `doctor.py` must **not** import `worker.py` — the caller passes `busy: bool`, keeping `doctor.py` importable from the CLI where no worker exists.
2. **Polling cost.** The status strip is on a page that already polls every second. Back `/api/status` with the existing `web/cache.MetadataCache` (TTL ≈ 30 s) plus an explicit refresh button, or a slow paperless makes the strip the new source of the M-01 freeze.

**Container healthcheck decision:** keep `HEALTHCHECK` pointed at `GET /health` (cheap liveness: is the worker thread alive). Do **not** point it at `saneless doctor`, which spawns a second Python process, re-reads config, and probes the scanner every interval. Document `saneless doctor` as the *human* diagnostic and `/health` as the *orchestrator* probe. This keeps C-09's "worker died → 503" signal intact.

---

## Data Flow

### Current flow (what the review found)

```
routes.start_scan (async, blocking put)
  → JobStore.create_job                    ← unlocked, shared connection (C-07)
  → worker.submit  ─ queue.put (blocks at 10) ────────────► C-09 hangs event loop
       └─ worker._process_job
            └─ run_pipeline() ──► dict {"status": ...}   ◄── DISCARDED (C-03)
                 ├─ scanner.scan_pages   list() all pages in RAM (M-08)
                 ├─ assemble_pdf → tmp/output.pdf         (C-05 collision)
                 └─ paperless.upload_document → "fallback" magic string (C-03)
                      └─ on PaperlessError → TemporaryDirectory deletes PDF (C-04)
            → JobStore.update_state(DONE)   ← always DONE, whatever happened
```

### Target flow

```
routes.start_scan (def)
  → worker.submit(job)  put_nowait → WorkerBusyError → HTTP 429 (C-09)
       (job row created only after a successful enqueue)
  → worker._process_job                                   [guarded loop, C-09]
       └─ run_pipeline(request with FlipCoordinator) ──► ScanResult   (C-03)
            ├─ profile.duplex == "manual"?                (C-01, one rule, one place)
            ├─ PageSpool: page → disk + stats + thumb     (M-08)
            ├─ select_pages(spooled, thresholds)          → pages_removed
            ├─ assemble_pdf(paths, out/<slug>-<stamp>.pdf, dpi=actual)  (C-05, M-06)
            └─ delivery.deliver(pdf, meta) ──► UploadResult
                 ├─ upload_document → task_id; poll_task raises on FAILURE (M-22)
                 ├─ except PaperlessError → consume_dir (.part → rename)  → FALLBACK
                 └─ except → move to <data_dir>/failed/  → FAILED, kept_path (C-04)
       → JobStore.finish_job(job.id, result)   ← state + counts + warning, one lock
  ← status poll → status.html renders STATE_LABELS[state] + counts + friendly error
                  + _scan_button.html via hx-swap-oob     (C-10)
```

### Key data-flow changes, itemised for the roadmapper

1. **`run_pipeline` return type: `dict` → `ScanResult`.** Blast radius: `worker._process_job`, `cli.scan`, `tests/test_pipeline.py` (833 lines), `tests/test_worker.py` (1054 lines), `tests/test_cli.py` (821 lines). This is the single largest test-touching change in the milestone; it belongs early so later steps assert against the final shape.
2. **`PipelineEvent` → `JobState` on the status callback.** The worker's `_status_cb` currently translates six events into five states with an inline `if/elif` chain and manipulates `_transition_event` along the way. After M-02 removes `wait_transition`, `_status_cb` collapses to `self._job_store.update_state(jid, state)`.
3. **`upload_document` return: `str | "fallback"` → `str` (raises).** `poll_task` return: `dict` → `dict` on success, raises on FAILURE/TIMEOUT. Both make the type checkers (`ty`, `pyrefly`, both already clean and gating) able to catch a caller who ignores the outcome — exactly the review's lesson 1.
4. **`assemble_pdf` input: `list[Image]` → `Sequence[Path]`, plus `output_path` and `dpi`.**
5. **`JobStore` schema: +5 columns** (`outcome`, `pages_scanned`, `pages_removed`, `pages_uploaded`, `warning`) and possibly `owner_token` for U-06. The current ad-hoc `try: ALTER TABLE / except OperationalError: pass` does not scale to five more; replace it with a `PRAGMA user_version` ladder **in the first step that adds a column**.
6. **Queue position (U-06) needs no new worker state.** `JobStore.list_pending()` ordered by `created_at` already gives position — index of this job among `PENDING` rows. This survives restarts (where M-03's `fail_active_jobs` then cleans up) and avoids reaching into `queue.Queue` internals.
7. **`failed/` and the SQLite DB must move off `tmp_dir`.** `web/app.py` currently puts `saneless.db` at `Path(settings.output.tmp_dir) / "saneless.db"`, and `tmp_dir` defaults to `/tmp/saneless`. Crash recovery (M-03) and preserved PDFs (C-04, U-08) are both meaningless on a directory the OS may clear. **Introduce `output.data_dir`** (default `$XDG_STATE_HOME/saneless`, mapped to the `saneless-data` volume in compose) for `saneless.db` and `failed/`; keep `tmp_dir` for genuinely disposable scratch. This is an extension of U-08's parenthetical ("make sure `failed/` lives on the `saneless-data` volume") but it applies equally to the job history the UI depends on.

### Internal boundaries after the change

| Boundary | Communication | Notes |
|----------|---------------|-------|
| `web/routes` ↔ `worker` | direct sync calls; `WorkerBusyError`/`WorkerUnavailableError` → 429/503 | routes become `def` (M-01) so blocking is legal and correct |
| `worker` ↔ `pipeline` | `PipelineRequest` in, `ScanResult` out, `JobState` callback | the untested seam that hides C-01..C-10 (M-33) |
| `pipeline` ↔ `scanner` | `ScanSettings` in, `Iterator[Image]` out | `source` is now a *pure SANE value*; strategy lives in `profile.duplex` (C-01) |
| `pipeline` ↔ `delivery` | `Path` + metadata in, `UploadResult` out | delivery owns the fallback/preserve policy; pipeline owns the tmpdir |
| `delivery` ↔ `paperless` | pure HTTP; raises `PaperlessError` | no filesystem knowledge in the HTTP client |
| `cli`/`web` ↔ `states` | `STATE_LABELS`, `ACTIVE_STATES`, `ERROR_MESSAGES` | one map, two renderers (M-05, U-05) |
| `cli`/`web` ↔ `doctor` | `list[Check]` | `doctor` never imports `worker` or `web`; busy state is a parameter |
| everything ↔ `scanner/base.classify_source` | pure function | four current implementations collapse to one (C-06) |

### External integration points

| Service | Integration pattern | Gotchas the design must respect |
|---------|--------------------|-------------------------------|
| `saned` / libsane | `python-sane` via lazy import in `scanner/__init__.py` | Exclusive device — doctor must not probe during a scan. Real python-sane silently accepts unknown attributes (M-15), so the fakes must too. `sane.init()` is per-process, not per-instance (N-04). |
| paperless-ngx REST | `httpx.Client`, sync | `FAILURE` is the *normal* response to a duplicate — must raise (C-03/M-22). Split timeouts: `connect=5, read=30` (M-01). |
| paperless consume dir | shared filesystem | Write `.part` then `rename` — the consumer must never see a partial file (C-05). Needs a mounted volume the container can write and paperless can read (U-08). |
| Docker / compose | `HEALTHCHECK` → `/health` | Env beats TOML in this app's source order, so the shipped `SANELESS_PAPERLESS__TOKEN=changeme` silently wins (U-01). Doctor must detect placeholder tokens. |

---

## Build Order

The review's section 10 gives eleven steps. The dependency analysis below **validates the grouping but changes the order in five places.** Each change is justified by "this step rewrites code the other step would have to rewrite again", or "this step's tests are the ones that protect everything after it".

### Recommended order

| # | Step | Findings | Why here (dependency rationale) |
|---|------|----------|--------------------------------|
| **0** | **CI first** — workflow running `ruff`, `ruff format --check`, `ty`, `pyrefly`, `pytest -m "not browser"` on every push | M-25 | ⬆️ **Moved from review step 7.** The entire milestone rests on "each step leaves the suite green". Today the only gate is a local pre-commit hook. Ten steps of large refactors without CI is exactly how a step lands red and nobody notices. Zero source dependencies — can be the first commit. |
| **1** | **Vocabulary & contracts, no behaviour change** — create `states.py` and `results.py`; add `PdfError`, `WorkerBusyError`, `WorkerUnavailableError`, `FlipTimeoutError`; add `SourceKind` + `classify_source()` to `scanner/base.py`; re-export `JobState`/`ErrorCategory` from `job.py` | M-05 (structural half), C-06 (definition half) | ⬆️ **New step, split out of review steps 1–4.** Steps 2, 3, 4 and 5 each need these names. Introducing them inside whichever step gets there first means the other three re-edit `job.py`, `web/app.py` and `cli.py` label maps. Nothing consumes the new types yet, so the suite stays green by construction. Tests added: every `JobState` has a `STATE_LABELS` entry; `classify_source` parametrised over "ADF", "Automatic Document Feeder", "Brother …(left aligned)", "Flatbed", "Auto". |
| **2** | **Job store hardening** — `RLock` on every method, `with self._conn`, `PRAGMA user_version` migration ladder, `fail_active_jobs()`, `list_pending()`, the five result columns | C-07, M-03 (store half), U-02 (schema), U-06 (schema) | ⬆️ **Moved from review step 4.** Step 3 rewrites the body of every `JobStore` write method to persist `ScanResult`. Locking afterwards means touching every method twice. Adding the columns here also forces the migration ladder to exist before four more steps want columns. Test: the two-thread 200-round stress test, which fails today 10/10. |
| **3** | **Honest outcomes & never lose a scan** — `delivery.py`; `ScanResult` threaded through pipeline → worker → store → templates → CLI; `poll_task` raises; `"fallback"` sentinel deleted; unique atomic PDF names; **`pdf.py` final signature including `dpi`**; `output.data_dir` | C-03, C-04, C-05, M-22, **M-06**, U-02 (data), U-08 (partial) | Review step 1, **plus M-06 hoisted from review step 8.** C-05 and M-06 both change `assemble_pdf`'s signature and `tests/test_pdf.py`; doing them apart means two rewrites of the same 85-line test module. This step alone removes the "silently wrong result" class. |
| **4** | **Scanner truthfulness** — `classify_source` consumers in `sane_backend` and `auto_profiles`; remove white/black page policy from the backend; wrap SANE errors with real messages; option-presence check before geometry; read resolution back; source-first ordering; **rewrite the SANE fakes** | C-06, M-11, M-14, M-15, M-16, M-32 | ⬇️ **Moved ahead of manual duplex (review had it after).** Two hard dependencies point this way: (a) M-14's backend-level white-page drop silently breaks manual-duplex page parity, so a duplex parity test written before M-14 is testing the wrong system; (b) M-32's honest fakes are what make the C-01 duplex test meaningful — the review itself notes every manual-duplex test currently passes against a `MagicMock` that accepts any source. Fix the test double before writing the test that depends on it. |
| **5** | **Manual duplex** — `flip.py` coordinator; `ProfileConfig.duplex` field with a back-compat validator for `source = "Manual Duplex"`; CLI prompt; flip timeout; `SCANNING_REVERSE` visible; delete `wait_transition`; **C-08 round-trip guard** | C-01, C-02, M-07, M-02, N-07, **C-08** | Review step 2, **plus C-08 pulled forward from review step 5.** This step adds a profile field that `auto_profiles` must emit and `load_settings` must accept. C-08's "write then load" round-trip test is the guard that makes adding profile fields safe; every later field addition (U-04's `label`/`description`) then inherits it. Also update `docs/how-to/set-up-adf-duplex.md` in this step (doc rows 1, 2). |
| **6** | **Worker & web robustness** — guarded `_run`; `put_nowait` + 429; stop flag replaces sentinel; `join()` checked; routes `async def → def`; startup reconcile; startup profile generation; server-owned Scan button; single label map wired into templates and CLI | C-09, C-10, M-01, M-03, M-04, M-05 (wiring half) | Review step 4, minus the store work now in step 2. **Do M-01 (`async def → def`) first within this step** — it is mechanical and makes every subsequent route edit (429 mapping, `/api/status`, owner token) straightforward instead of async-contaminated. Tests: worker-survives-`prune`-exception; eleventh job gets 429; browser scan cycle asserting `#scan-btn` enabled after `.status-done`. |
| **7** | **Configuration strictness** — `extra="forbid"` with nested-key messages; refuse missing `--config`; `~`/XDG expansion; validated `log_level` and real `-v`; `Settings.config_path` used by the worker and CLI; `--force` merges in place; atomic writes; `title` implemented or deleted; `SecretStr` | M-18, M-09, M-10, M-19, M-20, M-21, M-24, N-15 | Review step 5, minus C-08 (now step 5). Must come **after** step 5, because `extra="forbid"` turns any config key the writer emits but the reader lacks into a hard startup failure — so every new profile field must already exist and round-trip before the gate closes. |
| **8** | **Exception translation** — wrap SANE, httpx, img2pdf, tomllib errors; `ConfigError` handling and a python-sane import guard in the CLI; `exc_info` logging in the worker; `load_settings` raises one type | M-17, N-06, N-08 | Review step 6, unchanged position (after 3–7). Rationale confirmed: the boundaries only stop moving once `delivery.py` exists (step 3) and the scanner's error paths are rewritten (step 4). Doing this earlier means re-wrapping the same call sites. This step is also the precondition for U-05 — the review is explicit that a friendly-message map built on today's misclassified errors "will be confidently wrong". |
| **9** | **Geometry, memory, timeouts (remainder)** — `PageSpool`; wait for the cancelled read before closing; flatbed timeout parity | M-08, M-12, M-13 | Review step 8, minus M-06 (done in step 3). What remains is pipeline-internal: `pdf.py`'s interface already accepts paths, so the spool is a contained change with no cross-module churn. |
| **10** | **Appliance layer** — `doctor.py` + `saneless doctor` + status strip + `/api/status`; friendly error messages from `ERROR_MESSAGES`; page counts rendered; profile `label`/`description`; queue position and owner-token flip gating; compose token/mount fixes; help text and tag picker | U-01 … U-08 | Review step 11, unchanged position. Correctly last among behaviour work: U-02 is "two columns and two template lines" only *after* step 3, U-05 is only truthful after step 8, U-04 needs step 6's startup generation, U-06 needs step 6's queue backpressure and step 2's `list_pending()`. |
| **11** | **Delivery & identity** — release workflow fixes, `kdknigga/saneless` rename + CI grep check, container logging, port alignment, `.dockerignore`, docs pages (U-09, U-10) and the section 8 doc-row sweep | M-26 … M-31, U-09, U-10, doc rows | Review step 7 minus M-25 (now step 0). Deliberately last: the rename touches 24 lines in nine files and would conflict with every other step's doc edits if done in the middle. **Docs for behaviour changes stay inside their own step** — only the identity rename and the two new explanatory pages live here. |
| **12** | **Suite hygiene and the minor sweep** — hermetic fixtures, remove the 26 sleeps, delete vacuous/duplicate tests, remaining negative-path tests, N-01…N-45 | M-33, M-34, N-18, N-24, N-40, section 5 | Review steps 9–10. Last by definition. Note M-33's "integration seam" tests are *not* deferred here — each one is written inside the step that fixes its finding (that is what keeps the suite protective rather than retroactive). |

### Where this differs from the review's section 10 — summary

| Change | From | To | Reason |
|--------|------|----|--------|
| CI (M-25) | step 7 | **step 0** | The premise "each step leaves the suite green" is unenforced without it |
| Contracts/vocabulary (`states.py`, `results.py`, `classify_source` definition) | spread across steps 1–4 | **new step 1** | Four steps otherwise re-edit the same label maps and enums |
| JobStore lock + migrations (C-07) | step 4 | **step 2** | Step 3 rewrites every store method to persist `ScanResult`; lock after = two rewrites |
| DPI in PDFs (M-06) | step 8 | **step 3** | Same function signature and same test module as C-05 |
| Scanner layer (step 3) vs manual duplex (step 2) | duplex first | **scanner first** | M-14 breaks duplex page parity; M-32's fakes are what make the duplex test meaningful |
| Config round-trip (C-08) | step 5 | **step 5 (duplex)** | It is the guard for adding the `duplex` profile field, and for every field added after |

### Green-suite checkpoints

Each step ends with a specific test that could not have passed before it and cannot silently regress after:

| Step | The test that locks it in |
|------|--------------------------|
| 1 | every `JobState` has a label; `classify_source` over five real-world source names |
| 2 | `JobStore` hammered from two threads, 200 rounds, zero exceptions; migration from a v1 database file |
| 3 | worker + real pipeline + stub scanner + `httpx.MockTransport`, parametrised over SUCCESS / FAILURE / TIMEOUT / fallback / mismatch, asserting the **persisted** state; upload raises after five pages → PDF exists under `failed/`; two fallbacks leave two files |
| 4 | `SaneBackend` against the real SANE `test` backend with the long feeder name yields ten pages; fakes mirror `SaneDev.__setattr__`/`__getattr__` |
| 5 | pipeline with a manual-duplex profile against the honest fake; `CliRunner` with `input="y\n"` shows the prompt between passes; `write_profiles_to_config` → `load_settings` round-trip |
| 6 | `prune` raises and the worker survives; eleventh submission returns 429; browser clicks Scan, waits `.status-done`, asserts `#scan-btn` enabled |
| 7 | misspelled nested key names the section and the valid keys; missing `--config` exits 2 |
| 8 | each boundary raises the saneless type, not the third-party one |
| 9 | 50-page scan peak RSS bounded; flatbed timeout raises `ScanError` |
| 10 | `doctor` exits non-zero with a placeholder token; status strip renders each check state |

---

## Anti-Patterns

### Anti-Pattern 1: Fixing C-03 by adding a field to `Job` and leaving the dict

**What people do:** Keep `run_pipeline() -> dict` and have the worker read `result["status"]`.
**Why it's wrong:** It re-creates the exact bug — a return value the caller must remember to inspect. `ty` and `pyrefly` (both gating, both currently clean) cannot help with a `dict`.
**Do this instead:** `ScanResult` dataclass with an enum outcome. Make the type checkers the enforcement mechanism.

### Anti-Pattern 2: Per-thread SQLite connections instead of a lock

**What people do:** Reach for `threading.local()` connections as the "proper" concurrency fix for C-07.
**Why it's wrong here:** `JobStore(db_path=":memory:")` is the default and is used throughout the suite; per-thread connections to `":memory:"` are **separate databases**, so the worker thread and the test thread would silently see different data. Escaping that requires `file::memory:?cache=shared` URIs plus `busy_timeout` tuning — real complexity for a workload of one writer and a 1 Hz poller.
**Do this instead:** `threading.RLock` around every method, `with self._conn` for transaction scoping. (Note: `sqlite3.threadsafety` is 3 in this environment, meaning the C library is serialized — but that only prevents crashes in the library, not the interleaved implicit-transaction state that produced the review's `cannot commit - no transaction is active`. The lock is still required.)

### Anti-Pattern 3: Keeping `source = "Manual Duplex"` as the strategy marker

**What people do:** Add the translation in `pipeline.py` and call it done.
**Why it's wrong:** The review found the rule implemented in `pipeline.py:94` *and* inline in `worker.py:206-209`. A translation in one place leaves the second copy to drift again, and the profile field still means two things.
**Do this instead:** `ProfileConfig.duplex: Literal["none","hardware","manual"]`, with a single model validator translating the legacy string (and warning). Both readers become `profile.duplex == "manual"`.

### Anti-Pattern 4: A friendly-message layer over unclassified errors

**What people do:** Ship U-05's `ErrorCategory → message` map early, because it is a small template change.
**Why it's wrong:** M-11 currently reports *any* first-page ADF error as "No paper detected in feeder". A friendly message on top of that says "Load paper and try again" when the lid is open or the device is busy — confidently wrong is worse than raw text.
**Do this instead:** Order it after step 8 (exception translation), as scheduled.

### Anti-Pattern 5: Pointing the container healthcheck at `saneless doctor`

**What people do:** Reuse the new doctor command as `HEALTHCHECK`, since U-03 mentions it.
**Why it's wrong:** It spawns a Python process every interval, re-reads config, and probes the exclusive scanner while a scan may be running. It also destroys C-09's clean signal (worker thread dead → 503).
**Do this instead:** `/health` stays liveness for orchestrators; `doctor` is the human/CLI diagnostic; `/api/status` is the UI's cached view of the same checks.

### Anti-Pattern 6: Leaving the job database and `failed/` under `tmp_dir`

**What people do:** Put `failed/` next to the scratch directory, as the review's C-04 sketch shows (`Path(settings.output.tmp_dir) / "failed"`).
**Why it's wrong:** `tmp_dir` defaults to `/tmp/saneless`, and `web/app.py` already puts `saneless.db` there. Crash recovery (M-03) and preserved scans (C-04) both assume durability that `/tmp` does not provide, and in the recommended compose deployment neither path is on a volume (U-08).
**Do this instead:** Add `output.data_dir` for the database and `failed/`; mount it as `saneless-data`. Keep `tmp_dir` genuinely disposable.

---

## Scaling Considerations

This is a single-scanner household appliance; "scale" means pages per job, concurrent browsers, and history size — not users.

| Dimension | Today | After v2.0 | First bottleneck |
|-----------|-------|-----------|------------------|
| Pages per job | ~1.3 GB RAM for 50 colour pages at 300 DPI (M-08) | bounded by disk, ~O(1) RAM via `PageSpool` | Disk in `tmp_dir`; the existing `min_free_space_mb` check becomes meaningful once it is computed from `page_image.size` rather than `tobytes()` |
| Concurrent browsers | every route blocks the event loop (M-01) | `def` handlers run in the threadpool | uvicorn's default 40-thread pool; a 1 Hz poll per tab plus a cached `/api/status` is well within it |
| Queue depth | 10, blocking put → server hang (C-09) | 10, `put_nowait` → 429 with queue position (U-06) | The scanner itself; queue depth is a UX number, not a throughput number |
| Job history | prune inside the per-job `finally` (C-09) | prune moved out of the job path, run at startup and on a timer | SQLite is comfortable at the 500-row cap; the risk was never size, it was a housekeeping failure killing the worker |

**Explicit non-goal:** none of this milestone's work should introduce async I/O, a task broker, or a second process. The review's M-01 fix moves *away* from async precisely because every collaborator is synchronous.

---

## Sources

- `.planning/reviews/2026-09-09-code-review.md` — sections 3 (C-01…C-10), 4 (M-01…M-34), 7 (cross-cutting lessons), 8 (documentation audit), 9 (test-suite assessment), 10 (remediation order), 11 (U-01…U-10). HIGH confidence: every finding cited above was independently re-verified by the review's lead against executed code.
- Direct reads of `src/saneless/`: `pipeline.py`, `worker.py`, `job.py`, `web/routes.py`, `web/app.py`, `paperless.py`, `pdf.py`, `config.py`, `exceptions.py`, `scanner/base.py`, `scanner/sane_backend.py` (lines 94–200, 460–519), `auto_profiles.py`, `cli.py`, `web/templates/{index,partials/status,partials/flip}.html`. HIGH confidence.
- `.planning/PROJECT.md` — milestone scope, constraints, prior key decisions. HIGH confidence.
- Installed environment (`uv run python`): `img2pdf.convert(images, outputstream, **kwargs)` and `img2pdf.get_fixed_dpi_layout_fun(fixed_dpi)` both present; pydantic 2.12.5, pydantic-settings 2.13.1, FastAPI 0.135.1, httpx 0.28.1, SQLite 3.34.1 with `sqlite3.threadsafety == 3`, Python 3.14.2. HIGH confidence.

---
*Architecture research for: saneless v2.0 "Prep for release" — integration of review remediations into the existing module graph*
*Researched: 2026-09-09*
