# Phase 29: Geometry, Memory, and Timeouts - Context

**Gathered:** 2026-09-15
**Status:** Ready for planning
**Mode:** `--auto` (every gray area resolved to its recommended option; see 29-DISCUSSION-LOG.md)

<domain>
## Phase Boundary

A long scan is ordered, bounded in memory, and cancellable without wedging the process.
Delivers HARD-01..HARD-05 (review findings M-08, N-02, M-12, M-13, N-04):

- Pages are spooled to disk as they arrive, each with an explicit ordered page record. A
  12-page scan comes out 1..12, duplex interleave reorders records rather than files, and peak
  memory is bounded by roughly one page (HARD-01, M-08)
- A scanner error after N pages keeps those N pages and reports the error with the count
  (HARD-02, N-02)
- A timeout cancels the SANE read and waits for it to return before closing the device. A read
  that never returns blocks neither process exit nor `docker stop` nor `pytest` (HARD-03, M-12)
- The flatbed path goes through the same timeout and image validation as the ADF path
  (HARD-04, M-13)
- `sane.init()` runs once per process behind a re-entry guard, `sane.exit()` runs at shutdown,
  and neither can be reached from a request path (HARD-05, N-04)
- `docs/explanation/architecture.md`, plus every other doc sentence this phase makes true or
  false, is corrected in the same phase

**Not in this phase:**
- **DPI in PDFs (M-06):** already delivered by OUTC-06 (Phase 23), with the device read-back DPI
  from Phase 24 D-12. The phase name keeps "Geometry" from review step 8, but no geometry work
  remains here. Do not re-open it.
- **Mid-pass abort** (aborting during pass B). Phase 25 D-16 and Phase 28 both pointed it here,
  but only as "needs HARD-02's lazy consumption". No HARD requirement or success criterion
  covers it, and D-16 already fixed M-02 by removing the control. It stays deferred. The
  cancel mechanism built here (D-11) is the seam it would use later.
- **Page counts on failed jobs in the UI or history table:** APPL-03, **Phase 30**. This phase
  puts the count in the error message only (D-09).
- **A degraded `/health` or status strip when the scanner is wedged:** APPL-01/02, **Phase 30**.
  This phase refuses the next scan with a clear message (D-13).
- **`PIL.Image.MAX_IMAGE_PIXELS` set in three modules:** N-10, **Phase 32**.
- **Scanner tests whose names over-claim**, e.g. `init_calls_sane_init_exactly_once`: TEST-04,
  **Phase 32**. This phase adds the real init-once test it needs (D-17), and a test it replaces
  may be deleted in passing.
- **A separate Paperless connect timeout:** Phase 26 deferred it with "Phase 29 Timeouts is the
  natural home", but no v2.0 requirement maps it. It stays deferred (see below).

</domain>

<decisions>
## Implementation Decisions

### Carried forward (already decided, do not re-litigate)
- **Phase 24 D-12, amended by this phase:** `ScanBatch` is the one channel for what the backend
  measured. It keeps `actual_resolution` and `pages_rejected`, and HARD-01's page records join
  it (D-01). Its docstring reserved per-page structure for this phase.
- **Phase 24 D-03 / D-06 / D-04:** `StopIteration` is the only feeder-empty signal. An
  integrity-failed page is skipped and counted, and only a batch where every page was rejected
  raises. `_MAX_ADF_PAGES` still applies per `scan_pages` call. D-06's all-or-nothing edge is
  what HARD-02 softens.
- **Phase 24 D-05:** blank-page *policy* belongs to the pipeline, under the profile toggle. The
  backend measures facts only.
- **Phase 24 D-08 / Phase 23 D-08:** a duplex mismatch delivers two partial PDFs, unfiltered, as
  an anomaly for human review.
- **Phase 23 OUTC-04 / `_preserving`:** preserved scans go to `<data_dir>/failed/` with unique
  job-keyed names. saneless never prunes that directory.
- **Phase 28 D-07 / D-08:** the exit-code table (1 = scan error, 4 = PDF error, 130 = cancelled)
  and the message shape `<what saneless was doing, with identifiers>: <original message>`.
  A cancel is never preserved, logged at ERROR, or shown red.
- **Phase 26 D-08 / D-09:** `worker.stop()` joins for at most `STOP_JOIN_SECONDS`, and the store
  and Paperless client close only after a confirmed stop. The worker thread is already a daemon.
- **Phase 25 D-16:** a flip answer is final. Mid-pass abort is not delivered here (see Boundary).

### Page spool and ordered page records (HARD-01, M-08)
- **D-01: `scan_pages` takes a page sink, and `ScanBatch.pages` becomes an ordered tuple of page
  records.** The backend acquires a page, crops it if needed, and hands it to a small `PageSink`
  protocol declared in `scanner/base.py`. It never holds a list of images. The concrete spool is
  pipeline-owned, in a new module such as `saneless/spool.py`. It writes the page to disk and
  returns a record, and `ScanBatch` returns those records alongside `actual_resolution` and
  `pages_rejected`.
  - Rejected: going back to a generator. Phase 24 moved away from it because the return value
    that carries DPI and the rejection count is thrown away by every `list()` (base.py
    docstring). Rejected: a free callback with no protocol, because the type checkers could not
    see the contract.
- **D-02: a page record is a frozen dataclass of facts, never verdicts.** It has an explicit
  1-based `sequence` assigned at acquisition, the `path` of the spooled file, the pixel `size`
  and `mode`, and the greyscale mean and stddev measured once at spool time. It carries no
  "is blank" flag: `_drop_empty_pages` applies the profile's thresholds to the stored stats
  (Phase 24 D-05). The order of the tuple is the document order, and `sequence` is the proof.
  Nothing ever sorts or globs the spool directory to recover order.
  - The two passes of a manual duplex job spool into distinguishable names (e.g. `a-0001.png`,
    `b-0001.png`) for debuggability only. Order still comes from the record list.
- **D-03: the spooled file is PNG, and it is exactly what img2pdf embeds.** `assemble_pdf` takes
  records (paths) and stops re-saving every page as a second PNG (`pdf.py:208-214` goes).
  - **Amended 2026-09-15 after research (29-RESEARCH.md Finding 3, Open Question 1).** The
    locked clause "passes `outputstream=` so the PDF streams to its file" does not achieve what
    it was locked for: `img2pdf.convert` reads every page fully into memory and finalises the
    whole document before `outputstream` is ever written, so peak memory stays linear in page
    count (measured: 787 MB at 48 pages, versus 1395 MB today). **Assembly therefore runs
    per page — one `img2pdf.convert(..., outputstream=...)` per page — and the single-page PDFs
    are merged with `pikepdf.Job(["qpdf", "--empty", "--pages", *singles, "--", out])`.**
    Measured flat at 131 MB for both 12 and 48 pages, byte-identical `/FlateDecode` image
    streams, same MediaBox, same wall clock. Both of D-03's locked clauses stay literally true,
    and this is the only shape in which success criterion 1's memory sentence is true end to end.
    - Accepted cost: `Job.run()` holds the GIL for roughly 12 ms per page, so a 500-page job
      stalls its thread for about 6 s during assembly. It is the worker thread, already blocked
      for the whole scan, and the alternative is gigabytes of RAM.
    - **`pikepdf` becomes an explicit runtime dependency in `pyproject.toml`.** It is already
      installed as a hard transitive dependency of img2pdf and as a dev dependency, but a
      mandatory runtime import must be declared, not inherited: a missing mandatory dependency
      has to fail loudly at install time, never at scan time.
  - **PNG compression level: Pillow's default (6), passed implicitly by not passing the
    argument.** Measured 13.7 MB / 1.57 s versus 15.8 MB / 0.62 s at level 1 on a noisy A4
    300 DPI colour page. The PNG *is* the PDF's page content, so the 13% is saved in the PDF, in
    the upload and in Paperless storage forever, while the second is paid once against a 10-15 s
    scan. `pdf.py` already saves at the default, so output stays byte-comparable. Level 1 is
    recorded in the module docstring as the throughput fallback, not as a config key.
- **D-04: `_interleave_duplex` operates on records.** Backs are reversed and zipped with fronts
  as record objects, and no file is renamed. Success criterion 1's test builds a duplex job
  whose filesystem order differs from its document order.
- **D-05: the first page's thumbnail is generated when that page is spooled.** The image is in
  memory at that moment, and reopening a 26 MB page later would be a second decode. The
  thumbnail callback therefore fires during pass A instead of after it, a small visible
  improvement. The spool owns this, because the thumbnail is pipeline behaviour, not scanner
  behaviour.
- **D-06: M-08's cheap wins land too.** `_validate_page_image` computes its byte count from
  `size` and the band count instead of `tobytes()`, which copied the whole page just to measure
  its length. The greyscale conversion for blank-page statistics happens once, at spool time
  (D-02), instead of again in `is_empty_page`.
- **D-07: disk is checked per page, and a spool write never escapes as a raw `OSError`.** The
  up-front `min_free_space_mb` check stays. Before each page is written, the spool checks free
  space against that page's decoded size (an upper bound for its PNG) and keeps
  `min_free_space_mb` in reserve for assembly. A shortfall, or an `OSError` from the write,
  raises `ScanError` naming the page number and the spool path. That is a mid-batch error, so
  D-09 keeps the pages already spooled. `docs/reference` wording for `min_free_space_mb`
  changes to match.
- **D-08 (proof of the memory bound): count live page images, don't sample RSS.** Pillow
  allocates pixel memory outside `tracemalloc`'s view, and RSS is too noisy for CI. The fake
  device tracks every page image it hands out with a `weakref`. A 12-page scan asserts that the
  high-water mark of live page images stays at a small constant, independent of page count.
  **Measured bound (research Finding 4): 2**, because the backend's loop variable still holds
  page *k-1* while page *k* is acquired. The test asserts both `high_water <= 2` and that the
  mark for 12 pages equals the mark for 3 — the independence from N is the actual proof, and a
  hard `== 1` would only be reachable by adding a `del` that exists to satisfy a test.
  `Image` defines `__eq__` and so is unhashable: use `list[weakref.ref]`, never a `WeakSet`.
  The same test reads the PDF back with `pikepdf` and checks that each page carries its distinct
  per-page content in order 1..12.

### Keeping pages on a mid-batch error (HARD-02, N-02)
- **D-09: a failure after at least one page was spooled fails the job and preserves the pages.
  It never uploads a partial document.** The kept pages are assembled, unfiltered, into a PDF
  under `<data_dir>/failed/` with a job-keyed name that marks it partial. The job still ends
  ERROR, with its original exception type and so its original exit code and category. The
  message follows Phase 28 D-08 and carries the count and the path, e.g.
  `Scanner error on page 40: Document feeder jammed. The 39 page(s) scanned before the error
  were preserved at /data/failed/…-invoice (partial).pdf`.
  - Rejected: uploading the partial PDF to Paperless with a warning (N-02's alternative). A
    jammed 50-sheet job would land in Paperless as an incomplete document marked green, and the
    user rescans the stack anyway. Preservation matches OUTC-04 and the project's stated
    preference for failures that announce themselves (Phase 24 specifics).
  - Mechanism: the pipeline catches the failure around acquisition. The spool already holds
    the records, so the exception does not have to carry the pages. It then reuses the
    `_preserving` move and message pattern. Pages are not blank-filtered, following the
    duplex-mismatch precedent (D-08 of Phases 23 and 24).
- **D-10: the rule is uniform across what can end a scan after pages exist.**
  - A scanner fault, per-page timeout, page-cap overrun or spool shortage mid-batch preserves
    the N pages (simplex, and each manual-duplex pass).
  - **Manual duplex pass B:** a failure during pass B, or an empty pass B, preserves the fronts
    and any partial backs as the two separately named partial PDFs the mismatch path already
    uses. This closes the gap noted at `pipeline.py:1080` ("The fronts are still lost here;
    keeping them needs Phase 29's spooling").
  - **Flip-wait timeout or a broken flip prompt** (both ERROR per Phase 28 D-02) preserve the
    fronts, because nobody chose to stop.
  - **A cancel** (`ScanCancelledError`, Ctrl-C at the prompt, the web Abort) preserves
    **nothing**, because the operator chose to stop, and `failed/` is never pruned
    automatically.
  - **A `PdfError` during assembly** (deferred here by Phase 28): the PDF cannot be built, so
    the spooled page files themselves move into a `failed/<job-keyed-name>/` directory named in
    the error. `_warn_if_failed_dir_growing` must count these directories as well as `*.pdf`
    files.
  - Zero pages spooled means nothing to keep, and today's messages are unchanged
    (`FeederEmptyError`, "No pages were scanned").
  - If preservation itself fails, both failures are reported, as `_preserving` already does.

### Timeout and cancellation (HARD-03, HARD-04, M-12, M-13)
- **D-11: one `_acquire_with_timeout` helper runs each blocking SANE acquisition on a fresh
  daemon thread.** It serves the ADF `next(iterator)` and the flatbed `start()`+`snap()` alike.
  The per-call `ThreadPoolExecutor` goes: its workers are non-daemon threads that
  `concurrent.futures` joins at interpreter exit, which is exactly how a stuck read blocks
  process exit. A thread per page costs nothing next to a multi-second scan, and a stuck thread
  cannot poison a shared executor for the next job. The result or exception comes back through
  a queue or `Event`, and the caller waits with a timeout.
  - Rejected: M-12's "one long-lived executor per backend". It is still non-daemon, and one
    stuck read would block every later call.
- **D-12: the timeout sequence is cancel, then wait, then close only if the read returned.** On
  timeout: `dev.cancel()`, which is sound cross-thread — research read `_sane.c` and confirmed
  `sane_read`, `sane_start` and `sane_cancel` all release the GIL, while `sane_close`,
  `sane_init` and `sane_exit` hold it (so close-while-reading is doubly unsafe). Then wait up to
  a grace period, the module constant `_CANCEL_GRACE_SECONDS = 10.0`, injectable for tests the
  same way `timeout_per_page` is. If the
  read returned, close normally and raise the timeout `ScanError`
  (`Page N timed out after 120s`). If it did not return, **skip `close()`**, log CRITICAL, and
  raise the timeout `ScanError`, adding that the scanner did not respond to cancel.
  `_open_device`'s `finally` must know about that state.
  - **A page that arrives after the cancel is discarded, never spooled.** Measured on real
    libsane: a cancelled `snap()` returns a **truncated image** rather than raising — 3779x242
    of a full page, which would pass `_validate_page_image`. The timeout has already been
    reported, so any late result is dropped.
  - **On the `net` backend `sane_cancel` is itself a blocking RPC** on the control wire before
    the local data fd closes, so calling it from the waiting thread can hang the worker. Fire
    the cancel on its own daemon thread and let the grace bound only the reader.
  - **A wedged `SaneDev` stays strongly referenced.** Its `dealloc` calls `sane_close` and
    `_SaneIterator.__del__` calls `cancel`, so letting it be collected reintroduces exactly the
    close-while-reading this decision removes.
- **D-13: a device left mid-read wedges the backend until the read returns.** While a stuck read
  thread is alive, the next `scan_pages` / `get_capabilities` refuses at once with a `ScanError`
  telling the operator to restart saneless, and no SANE call is made. When the late read does
  return, the reader thread itself closes the handle and clears the wedge, so a transient hang
  recovers without a restart. This refuses honestly and follows the SANE rule that no other
  operation runs while one is outstanding.
- **D-14: flatbed and ADF share one timeout constant and one validation path.** The same
  `_DEFAULT_PAGE_TIMEOUT_SECONDS` (120 s) bounds one page's `start()`+`snap()` on either path,
  with no new config key (consistent with Phase 24 D-04's rejection of config knobs).
  Flatbed validation already exists (Phase 24 made `_validate_page_image` run on the flatbed
  page, fatal there because no next sheet exists). HARD-04's validation half is proven by a
  test, not rebuilt. The flatbed timeout test mirrors `test_page_timeout_raises_scan_error`.
- **D-15: Ctrl-C during a read takes the same device-safe path.** Phase 28 deferred "safe device
  cancel on Ctrl-C mid-read" here. The waiting thread is the one that receives
  `KeyboardInterrupt`, so the helper runs the D-12 cancel, grace wait, and close-or-leave, then
  re-raises `KeyboardInterrupt`. Exit 130 and the one-line message are unchanged.
- **D-16: tests prove it with an `Event`-gated blocking fake, never a sleep.** `FakeSaneDev`
  gains a blocking-read mode gated on a `threading.Event`. It records whether `close()` or
  `sane.exit()` was called while the read was blocked, and it unblocks on `cancel()` or stays
  blocked, per test. It must be able to unblock **both** ways real hardware and the code expect:
  by returning a truncated partial page, and by raising. TEST-02 bans new `time.sleep`, and the existing `set_page_delay` sleep is
  not extended. The in-process tests assert the ordering: cancel called, close never called
  while blocked, close called once the read returns. **Process exit is proven in a subprocess**:
  a child whose read never unblocks must exit within a bound, which is the only honest proof
  that neither `pytest` nor `docker stop` can hang on it.

### SANE lifecycle (HARD-05, N-04)
- **D-17: a thread-safe, module-level init guard, not a singleton.** `sane_backend.py` owns a
  lock and a flag. `SaneBackend.__init__` calls the guard, and only the first call in a process
  sets `SANE_NET_HOSTS` and calls `sane.init()`. A later construction with a different `host`
  logs a WARNING naming both hosts, because SANE reads `SANE_NET_HOSTS` only at first init
  (N-04's silently ignored host). `SaneBackend` stays freely constructible, so tests and every
  CLI command keep working. After `sane.exit()` the guard resets, so a later init is allowed.
  - The module docstring's "sane.init() called exactly once at construction time" and the class
    docstring are reworded to the process-level truth.
- **D-18: `sane.exit()` is called explicitly at each entry point's shutdown, never through
  `atexit`, and never while a read is outstanding.** A `close()` on the backend (N-04), declared
  on `ScannerBackend` so `create_app` can call it through the abstraction, calls an idempotent
  module-level shutdown.
  - **One-shot CLI commands** (`scan`, `devices`, `auto-profiles`) close the backend when the
    command ends, on success and on error. The mechanism is the planner's
    (`ctx.call_on_close`, or `try`/`finally`).
  - **`serve`:** the lifespan closes it after `worker.stop()` confirms a stop, next to the store
    and Paperless client (Phase 26 D-09). If the worker did not stop, or the backend is wedged
    (D-13), `sane.exit()` is skipped and logged, following the same rule as close-while-reading.
  - Rejected: `atexit`. It runs while a daemon reader thread may still be inside `sane_read`,
    and review principle 7 says globals are configured and torn down at the entry point.
- **D-19: "not reachable from a request path" is proven by behaviour, not grep.** A `TestClient`
  test drives the app through startup, every route including a scan submission and the flip
  routes, and lifespan shutdown. It asserts `FakeSaneModule.init_call_count` is 1 throughout
  and `exit_call_count` goes from 0 to 1 only at shutdown. A companion test constructs two
  backends and asserts one `init` call, with the host warning when the hosts differ.

### Documentation (architecture page in-phase, plus what the phase changes)
- **D-20: `docs/explanation/architecture.md` is updated in the same phase.** Specifically:
  - the pipeline diagram (`PIL Images` becomes spooled page files with ordered records);
  - "Pipeline Orchestration" (pages spooled as they arrive; what a mid-batch failure keeps, and
    where);
  - "PDF Assembly" (streams from the spooled files). While there, reword "byte-for-byte
    identical to what the scanner produced", which is not what a PNG-to-Flate embed means;
    say lossless;
  - a new short "Memory, disk and timeouts" subsection (one page in memory; the per-page disk
    check; the per-page timeout on both paths; cancel-then-wait; a stuck read never blocks
    shutdown; restart after a wedge);
  - "Worker Thread Model" shutdown (SANE is initialised once and shut down after the worker
    stops).
  Also corrected in-phase, wherever they describe the old behaviour:
  - `docs/how-to/troubleshoot-a-failed-scan.md` (partial pages preserved; the "restart saneless"
    wedge message);
  - `docs/explanation/consume-directory-fallback.md` (`failed/` now also holds partial scans and
    page directories);
  - `docs/how-to/set-up-adf-duplex.md` (fronts are kept when pass B fails or the flip times out);
  - the `min_free_space_mb` reference;
  - code docstrings that name Phase 29 as future work (`_MAX_ADF_PAGES`, `ScanBatch`,
    `scan_pages`, `pipeline.py:1080`).

### Claude's Discretion
The planner may refine these defaults, but should not reverse them without cause.
- Module and type names (`spool.py`, `PageSink`, `PageRecord`, `SpooledPage`).
- ~~Whether the sink is a `Protocol` or an ABC~~ — resolved by pattern mapping: **an ABC**. The
  repo already states the rule in `pipeline.FlipCoordinator`'s docstring (`pipeline.py:127-132`)
  — `Protocol` describes shapes the project does not own, `ABC` defines seams it implements
  itself — and the sink is a seam this project implements. Following the existing rule beats
  amending a docstring that is itself a doc-truth site.
- ~~PNG compression level~~ — resolved by research: Pillow's default 6 (D-03).
- ~~`_CANCEL_GRACE_SECONDS`~~ — resolved by research: 10.0. ~~Hand-off primitive~~ — resolved:
  an `Event` plus a slot, prototyped and measured (D-11, D-12).
- ~~The live-image bound~~ — resolved by research: `<= 2`, and equal for N=3 and N=12 (D-08).
- Whether the partial-scan exception carries a structured `pages_kept` attribute as well as the
  message (the message is what is locked; Phase 30 may want the attribute).
- How `MagicMock(spec=ScannerBackend)` call sites in `tests/test_pipeline.py` (~40) migrate to
  the new `scan_pages(..., sink)` signature: a shared fake backend that spools real records is
  preferred over hand-built records per test.
- The exact wording of every new message within Phase 28 D-08's shape.

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements and roadmap
- `.planning/REQUIREMENTS.md` lines 119-125: HARD-01..HARD-05 verbatim
- `.planning/REQUIREMENTS.md` APPL-01/02/03 (lines 129-131), TEST-02/TEST-04 (lines 167, 169),
  SWP (N-10): the neighbouring requirements that fence this phase
- `.planning/ROADMAP.md` § "Phase 29: Geometry, Memory, and Timeouts": goal and five success
  criteria

### Review findings (the source of every HARD requirement)
- `.planning/reviews/2026-09-09-code-review.md` § M-08 (line 459): measured memory and disk
  figures, the cheap wins, and the structural spool fix
- same file § M-12 (line 509): close-while-reading, non-daemon executor threads, the
  cancel-then-wait sketch, and the "close() not called while blocked" test
- same file § M-13 (line 531): flatbed without a timeout, and the shared-helper prescription
- same file N-02 (line 839): mid-batch failure discards acquired pages
- same file N-04 (line 843): per-instance init, host silently ignored, no `sane.exit()`
- same file § 9 principles 3 and 7 (lines ~967, ~975): "preserve what the user cannot
  regenerate", and "configure globals once, at the entry point"
- same file § 10 "Step 8" (line 1078): the remediation summary

### Prior phase decisions carried forward
- `.planning/phases/24-scanner-truthfulness/24-CONTEXT.md`: D-03, D-04 (`_MAX_ADF_PAGES`), D-05,
  D-06, D-07, D-08, D-12 (the `ScanBatch` channel this phase extends), and the "Not in this
  phase" list assigning M-12/M-13/N-02/N-04 here
- `.planning/phases/25-manual-duplex/25-CONTEXT.md` D-16 (mid-pass abort pointer, not a
  commitment)
- `.planning/phases/26-worker-and-web-robustness/26-CONTEXT.md` D-08/D-09 (bounded stop, close
  order) and its Deferred list (Paperless connect timeout)
- `.planning/phases/28-exception-translation/28-CONTEXT.md` D-02, D-07, D-08, and its Deferred list
  (PdfError page preservation, safe Ctrl-C mid-read)
- `.planning/phases/23-honest-outcomes-and-never-lose-a-scan/23-CONTEXT.md`: the preservation
  guard and the `failed/` directory contract

### External references
- SANE standard, `sane_cancel` / cancellation: a frontend must not call another operation until
  a cancelled one has returned. `sane_cancel` may be called asynchronously (confirm for `net`).
- `.venv/lib/python3.14/site-packages/sane.py`: `_SaneIterator.__next__` (`start()` +
  `snap(True)`), `SaneDev.cancel`/`close`, and `sane.exit`
- CPython `concurrent.futures.thread`: `_python_exit` registered through
  `threading._register_atexit` joins every worker thread at exit (the M-12 mechanism; confirm on
  3.14)
- img2pdf `convert(..., outputstream=...)`: confirm against the installed version via Context7 or
  the venv source
- Pillow memory allocation vs `tracemalloc` visibility (the reason for D-08's weakref proof)

### Background
- `.planning/debug/scanner-memory-overflow.md`: the real hung or failed network flatbed read on an
  HP LaserJet 3030 via `hpaio` over `net`. It shows the flatbed-hang scenario M-13 cites is real.
  Its scanimage workaround is not in the current code.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `ScanBatch` (`scanner/base.py:194-230`): frozen; its docstring reserves per-page records for
  this phase. `pages: list[Image.Image]` becomes the ordered record tuple.
- `_open_workspace` (`pipeline.py:369`): the per-job `TemporaryDirectory` under `tmp_dir` that
  already exists when scanning starts. The spool lives inside it.
- `_check_disk_space` (`pipeline.py:342`): the up-front check that D-07 extends per page.
- `_preserving` (`pipeline.py:473-560`) and `_warn_if_failed_dir_growing` (`:429`): the move,
  message and never-raise patterns that D-09/D-10 reuse. The growth check counts `*.pdf` only
  today.
- `_handle_duplex_mismatch` (`pipeline.py:773`): distinct fronts/backs partial-PDF naming that
  D-10's pass-B rule reuses.
- `build_pdf_filename` (`pdf.py:86`): the job-keyed unique names.
- `FakeSaneDev` / `FakeSaneModule` (`tests/fake_sane.py:597`, `:1107`): `calls`, `cancel_calls`,
  `close_calls`, `init_call_count`, `exit_call_count`, `load_feeder()` for exact per-page images,
  and `set_page_delay` (sleep-based; D-16 adds an Event-gated mode instead).
- `pikepdf` (dev dependency, already used in `tests/test_pdf.py`) for reading PDFs back.

### Established Patterns
- Frozen dataclasses for reports of what already happened (`ScanBatch` precedent).
- Total `match` + `assert_never` for any new variant set.
- `raise SanelessType(msg) from exc`, with `msg` built in a variable first (ruff `EM`/`TRY`).
- Docstrings explain the "why" and cite finding and decision IDs, matching the density of
  `sane_backend.py` and `pipeline.py`.
- No `# type: ignore` / `# noqa` additions; `ty` and `pyrefly check src tests` both clean. TDD RED
  commits are allowed.
- Module constants over config knobs for internal bounds (`_MAX_ADF_PAGES`,
  `_DEFAULT_PAGE_TIMEOUT_SECONDS`).
- No new `time.sleep` in tests (TEST-02); `pytest-timeout` runs with `timeout_method = "signal"`.

### Integration Points
- `scanner/sane_backend.py`:
  - `_next_page_with_timeout` / `_acquire_pages` (`:662-827`; the executor at `:764` and
    `shutdown(wait=False)` at `:809`)
  - `_snap_flatbed` (`:1095`, no timeout)
  - `_validate_page_image` (`:623`, `tobytes()` at `:654`)
  - `_maybe_crop` over the list (`:1447-1455`), which moves to per page before the sink
  - `SaneBackend.__init__` (`:1169-1202`, init guard)
  - `_open_device` `finally` (`:1228-1239`, skip close when wedged)
  - `_MAX_ADF_PAGES` comment (`:115-136`)
- `scanner/base.py`: `ScannerBackend.scan_pages` signature and `close()`.
- `pipeline.py`:
  - `_scan_simplex` (`:1144`), `_scan_manual_duplex` (`:991`, pass B at `:1076-1088`, flip
    failures at `:1055-1072`), `_interleave_duplex` (`:959`)
  - `_drop_empty_pages` (`:563`, uses stored stats), `run_pipeline` (`:1286-1404`: the spool
    lifetime, the preservation of a partial scan, and `pages_scanned` from records)
  - `_DuplexMismatch` (`:~675`, records rather than images)
- `pages.py`: `is_empty_page` / `filter_empty_pages` / `generate_thumbnail` operate on stats or
  records.
- `pdf.py:128-239` `assemble_pdf`: takes paths, no re-save, `outputstream=`.
- `cli.py`: `SaneBackend(...)` at `:543` (scan), `:640` (devices), `:761` (serve), `:861`
  (auto-profiles), each needing a matching close.
- `web/app.py:74-136` lifespan: close the scanner after a confirmed `worker.stop()`.
- `web/routes.py`: touches no scanner today; D-19 proves it stays that way.
- Tests: `tests/test_scanner.py`, `tests/test_pipeline.py` (~40
  `MagicMock(spec=ScannerBackend)` sites), `tests/test_pdf.py`, `tests/test_pages.py`,
  `tests/test_outcomes_e2e.py`, `tests/test_app_lifespan.py`, `tests/test_cli.py`,
  `tests/fake_sane.py`, `tests/test_sane_hardware.py` (opt-in real `test` backend, whose
  `read-delay` option can exercise a real slow read).

</code_context>

<specifics>
## Specific Ideas

- The message shape for a partial scan:
  `Scanner error on page 40: Document feeder jammed. The 39 page(s) scanned before the error were
  preserved at <path>`. The error type and exit code are the original ones.
- "Order comes from the record list, never from the filesystem" is the invariant the HARD-01 test
  attacks: a duplex job whose file names sort differently from the document order.
- "Never call another SANE operation while one is outstanding" applies to `close()`,
  `sane.exit()`, and the *next job's* `open()` alike (D-12, D-13, D-18).
- A cancel keeps nothing; a failure keeps everything it can. Both rules come from the prior
  phases, not new policy.

</specifics>

<deferred>
## Deferred Ideas

- **Mid-pass abort during manual duplex pass B.** No v2.0 requirement maps it. Phase 25 D-16
  removed the control instead. The D-11 cancel helper is the seam a later phase would use.
- **A configurable per-page timeout or cancel grace** (a `scanner.page_timeout_seconds` key).
  Rejected for now as new config surface, consistent with Phase 24 D-04. Revisit if a real
  1200 DPI flatbed exceeds 120 s.
- **Uploading a partial scan to Paperless with a warning** (N-02's alternative). Rejected by D-09
  in favour of preservation in `failed/`.
- **A degraded `/health` and a status-strip line while the scanner is wedged.** This belongs with
  APPL-01/02 (Phase 30). The wedge flag from D-13 is the fact such a check would read.
- **Structured page counts on failed jobs** (a `pages_scanned` column filled on ERROR): APPL-03,
  Phase 30.
- **A Paperless connect timeout separate from the upload timeout, and caching an unreachable
  result** (Phase 26 deferral that pointed at "Phase 29 Timeouts"). Still no requirement; check
  at milestone audit.

No pending todos matched this phase.

</deferred>

---

*Phase: 29-geometry-memory-and-timeouts*
*Context gathered: 2026-09-15*
