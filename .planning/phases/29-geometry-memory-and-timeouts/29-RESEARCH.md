# Phase 29: Geometry, Memory, and Timeouts - Research

**Researched:** 2026-09-15
**Domain:** CPython threading and interpreter shutdown, SANE/python-sane cancellation semantics, Pillow/img2pdf/pikepdf memory behaviour, page spooling design
**Confidence:** HIGH for everything measured in this repo's venv and against the real SANE `test` backend; MEDIUM for `net`-backend cancel behaviour (read from sane-backends source, not executed against saned)

## Summary

Every mechanism this phase needs was measured, in this project's `.venv` (CPython 3.14.2, python-sane 2.9.2, Pillow 12.1.1, img2pdf 0.6.3, pikepdf 10.5.1) or against real libsane. Three results change how the phase should be planned.

**One.** M-12's diagnosis is exactly right on 3.14 and D-11's fix is exactly right: a `ThreadPoolExecutor` worker stuck in a blocking C call makes the process hang at exit forever (measured: `timeout 5` killed it), a non-daemon `threading.Thread` does the same, and a `daemon=True` thread stuck in the identical call lets the process exit in 0.24 s. `concurrent.futures.thread._python_exit` is still registered through `threading._register_atexit` and still `join()`s every worker. `sane_read` runs with the GIL released, so cancelling from another thread is possible in Python terms — but `sane_close`, `sane_init` and `sane_exit` all run **holding** the GIL in `_sane.c`, so those cannot be raced against anything.

**Two.** D-03's `outputstream=` does **not** bound assembly memory, and the phase should not claim it does. `img2pdf.convert` builds the entire document in memory before writing a byte; `outputstream=` only removes the final whole-PDF `bytes` copy. Measured on 48 synthetic A4/300 dpi colour pages: 1395 MB peak with `pdf_path.write_bytes(...)`, 787 MB with `outputstream=` — still linear in page count. A bounded alternative exists inside the current dependency set and honours D-03's letter (the spooled PNG is still exactly what img2pdf embeds, still via `outputstream=`): convert **one page at a time** to a one-page PDF, then merge with `pikepdf.Job(["qpdf", "--empty", "--pages", *singles, "--", out])`, whose native qpdf page copy is lazy. Measured peak: **131 MB for 12 pages and 131 MB for 48 pages** — flat — with byte-identical image streams, identical MediaBoxes and the same total wall clock.

**Three.** On the real SANE `test` backend a cancelled read does not raise: `snap()` **returns a truncated image**. So the timeout path must discard whatever the reader hands back after a cancel; a late partial page must never be spooled, and it would pass `_validate_page_image` if it were.

**Primary recommendation:** Build `_acquire_with_timeout` as a fresh `daemon=True` thread per acquisition with an `Event` + result slot; fire `dev.cancel()` from *another* daemon thread (the `net` backend's `sane_cancel` is itself a blocking RPC and can hang); wait `_CANCEL_GRACE_SECONDS = 10.0`; close only if the reader returned; never use a post-cancel result. Spool each page as PNG at Pillow's default `compress_level=6`, assemble per page with `img2pdf … outputstream=` and merge with `pikepdf.Job`, and state the memory bound honestly in `architecture.md` as "one decoded page during scanning, one compressed page during assembly".

## User Constraints (from CONTEXT.md)

*Copied from `.planning/phases/29-geometry-memory-and-timeouts/29-CONTEXT.md`. Where research contradicts an assumption inside a decision, it is flagged in the findings below — the decision itself is not reversed here.*

### Locked Decisions

**Carried forward (already decided, do not re-litigate)**
- **Phase 24 D-12, amended by this phase:** `ScanBatch` is the one channel for what the backend measured. It keeps `actual_resolution` and `pages_rejected`, and HARD-01's page records join it (D-01). Its docstring reserved per-page structure for this phase.
- **Phase 24 D-03 / D-06 / D-04:** `StopIteration` is the only feeder-empty signal. An integrity-failed page is skipped and counted, and only a batch where every page was rejected raises. `_MAX_ADF_PAGES` still applies per `scan_pages` call. D-06's all-or-nothing edge is what HARD-02 softens.
- **Phase 24 D-05:** blank-page *policy* belongs to the pipeline, under the profile toggle. The backend measures facts only.
- **Phase 24 D-08 / Phase 23 D-08:** a duplex mismatch delivers two partial PDFs, unfiltered, as an anomaly for human review.
- **Phase 23 OUTC-04 / `_preserving`:** preserved scans go to `<data_dir>/failed/` with unique job-keyed names. saneless never prunes that directory.
- **Phase 28 D-07 / D-08:** the exit-code table (1 = scan error, 4 = PDF error, 130 = cancelled) and the message shape `<what saneless was doing, with identifiers>: <original message>`. A cancel is never preserved, logged at ERROR, or shown red.
- **Phase 26 D-08 / D-09:** `worker.stop()` joins for at most `STOP_JOIN_SECONDS`, and the store and Paperless client close only after a confirmed stop. The worker thread is already a daemon.
- **Phase 25 D-16:** a flip answer is final. Mid-pass abort is not delivered here.

**Page spool and ordered page records (HARD-01, M-08)**
- **D-01: `scan_pages` takes a page sink, and `ScanBatch.pages` becomes an ordered tuple of page records.** The backend acquires a page, crops it if needed, and hands it to a small `PageSink` protocol declared in `scanner/base.py`. It never holds a list of images. The concrete spool is pipeline-owned, in a new module such as `saneless/spool.py`. It writes the page to disk and returns a record, and `ScanBatch` returns those records alongside `actual_resolution` and `pages_rejected`.
  - Rejected: going back to a generator. Phase 24 moved away from it because the return value that carries DPI and the rejection count is thrown away by every `list()`. Rejected: a free callback with no protocol, because the type checkers could not see the contract.
- **D-02: a page record is a frozen dataclass of facts, never verdicts.** It has an explicit 1-based `sequence` assigned at acquisition, the `path` of the spooled file, the pixel `size` and `mode`, and the greyscale mean and stddev measured once at spool time. It carries no "is blank" flag: `_drop_empty_pages` applies the profile's thresholds to the stored stats (Phase 24 D-05). The order of the tuple is the document order, and `sequence` is the proof. Nothing ever sorts or globs the spool directory to recover order.
  - The two passes of a manual duplex job spool into distinguishable names (e.g. `a-0001.png`, `b-0001.png`) for debuggability only. Order still comes from the record list.
- **D-03: the spooled file is PNG, and it is exactly what img2pdf embeds.** `assemble_pdf` takes records (paths) and stops re-saving every page as a second PNG (`pdf.py:208-214` goes). It passes `outputstream=` so the PDF streams to its file instead of being built as one `bytes` object (M-08). The PNG compression level is the planner's call, measured on a 300 DPI colour page.
- **D-04: `_interleave_duplex` operates on records.** Backs are reversed and zipped with fronts as record objects, and no file is renamed. Success criterion 1's test builds a duplex job whose filesystem order differs from its document order.
- **D-05: the first page's thumbnail is generated when that page is spooled.** The image is in memory at that moment, and reopening a 26 MB page later would be a second decode. The thumbnail callback therefore fires during pass A instead of after it. The spool owns this, because the thumbnail is pipeline behaviour, not scanner behaviour.
- **D-06: M-08's cheap wins land too.** `_validate_page_image` computes its byte count from `size` and the band count instead of `tobytes()`. The greyscale conversion for blank-page statistics happens once, at spool time (D-02), instead of again in `is_empty_page`.
- **D-07: disk is checked per page, and a spool write never escapes as a raw `OSError`.** The up-front `min_free_space_mb` check stays. Before each page is written, the spool checks free space against that page's decoded size (an upper bound for its PNG) and keeps `min_free_space_mb` in reserve for assembly. A shortfall, or an `OSError` from the write, raises `ScanError` naming the page number and the spool path. That is a mid-batch error, so D-09 keeps the pages already spooled. `docs/reference` wording for `min_free_space_mb` changes to match.
- **D-08 (proof of the memory bound): count live page images, don't sample RSS.** The fake device tracks every page image it hands out with a `weakref`. A 12-page scan asserts that the high-water mark of live page images stays at a small constant, independent of page count. The planner confirms the exact bound during research. The same test reads the PDF back with `pikepdf` and checks that each page carries its distinct per-page content in order 1..12.

**Keeping pages on a mid-batch error (HARD-02, N-02)**
- **D-09: a failure after at least one page was spooled fails the job and preserves the pages. It never uploads a partial document.** The kept pages are assembled, unfiltered, into a PDF under `<data_dir>/failed/` with a job-keyed name that marks it partial. The job still ends ERROR, with its original exception type and so its original exit code and category. The message follows Phase 28 D-08 and carries the count and the path, e.g. `Scanner error on page 40: Document feeder jammed. The 39 page(s) scanned before the error were preserved at /data/failed/…-invoice (partial).pdf`.
  - Rejected: uploading the partial PDF to Paperless with a warning.
  - Mechanism: the pipeline catches the failure around acquisition. The spool already holds the records, so the exception does not have to carry the pages. It then reuses the `_preserving` move and message pattern. Pages are not blank-filtered.
- **D-10: the rule is uniform across what can end a scan after pages exist.**
  - A scanner fault, per-page timeout, page-cap overrun or spool shortage mid-batch preserves the N pages (simplex, and each manual-duplex pass).
  - **Manual duplex pass B:** a failure during pass B, or an empty pass B, preserves the fronts and any partial backs as the two separately named partial PDFs the mismatch path already uses. This closes the gap noted at `pipeline.py:1080`.
  - **Flip-wait timeout or a broken flip prompt** preserve the fronts, because nobody chose to stop.
  - **A cancel** (`ScanCancelledError`, Ctrl-C at the prompt, the web Abort) preserves **nothing**.
  - **A `PdfError` during assembly:** the spooled page files themselves move into a `failed/<job-keyed-name>/` directory named in the error. `_warn_if_failed_dir_growing` must count these directories as well as `*.pdf` files.
  - Zero pages spooled means nothing to keep, and today's messages are unchanged.
  - If preservation itself fails, both failures are reported, as `_preserving` already does.

**Timeout and cancellation (HARD-03, HARD-04, M-12, M-13)**
- **D-11: one `_acquire_with_timeout` helper runs each blocking SANE acquisition on a fresh daemon thread.** It serves the ADF `next(iterator)` and the flatbed `start()`+`snap()` alike. The per-call `ThreadPoolExecutor` goes. The result or exception comes back through a queue or `Event`, and the caller waits with a timeout.
  - Rejected: M-12's "one long-lived executor per backend".
- **D-12: the timeout sequence is cancel, then wait, then close only if the read returned.** On timeout: `dev.cancel()` from the waiting thread. Then wait up to a grace period, a module constant `_CANCEL_GRACE_SECONDS`, injectable for tests the same way `timeout_per_page` is. The planner picks a value in the region of 10 s after research. If the read returned, close normally and raise the timeout `ScanError` (`Page N timed out after 120s`). If it did not return, **skip `close()`**, log CRITICAL, and raise the timeout `ScanError`, adding that the scanner did not respond to cancel. `_open_device`'s `finally` must know about that state.
- **D-13: a device left mid-read wedges the backend until the read returns.** While a stuck read thread is alive, the next `scan_pages` / `get_capabilities` refuses at once with a `ScanError` telling the operator to restart saneless, and no SANE call is made. When the late read does return, the reader thread itself closes the handle and clears the wedge.
- **D-14: flatbed and ADF share one timeout constant and one validation path.** The same `_DEFAULT_PAGE_TIMEOUT_SECONDS` (120 s) bounds one page's `start()`+`snap()` on either path, with no new config key. HARD-04's validation half is proven by a test, not rebuilt. The flatbed timeout test mirrors `test_page_timeout_raises_scan_error`.
- **D-15: Ctrl-C during a read takes the same device-safe path.** The waiting thread is the one that receives `KeyboardInterrupt`, so the helper runs the D-12 cancel, grace wait, and close-or-leave, then re-raises `KeyboardInterrupt`. Exit 130 and the one-line message are unchanged.
- **D-16: tests prove it with an `Event`-gated blocking fake, never a sleep.** `FakeSaneDev` gains a blocking-read mode gated on a `threading.Event`. It records whether `close()` or `sane.exit()` was called while the read was blocked, and it unblocks on `cancel()` or stays blocked, per test. TEST-02 bans new `time.sleep`. The in-process tests assert the ordering: cancel called, close never called while blocked, close called once the read returns. **Process exit is proven in a subprocess.**

**SANE lifecycle (HARD-05, N-04)**
- **D-17: a thread-safe, module-level init guard, not a singleton.** `sane_backend.py` owns a lock and a flag. `SaneBackend.__init__` calls the guard, and only the first call in a process sets `SANE_NET_HOSTS` and calls `sane.init()`. A later construction with a different `host` logs a WARNING naming both hosts. `SaneBackend` stays freely constructible. After `sane.exit()` the guard resets. The module and class docstrings are reworded to the process-level truth.
- **D-18: `sane.exit()` is called explicitly at each entry point's shutdown, never through `atexit`, and never while a read is outstanding.** A `close()` on the backend, declared on `ScannerBackend` so `create_app` can call it through the abstraction, calls an idempotent module-level shutdown.
  - **One-shot CLI commands** (`scan`, `devices`, `auto-profiles`) close the backend when the command ends, on success and on error. The mechanism is the planner's.
  - **`serve`:** the lifespan closes it after `worker.stop()` confirms a stop, next to the store and Paperless client. If the worker did not stop, or the backend is wedged (D-13), `sane.exit()` is skipped and logged.
  - Rejected: `atexit`.
- **D-19: "not reachable from a request path" is proven by behaviour, not grep.** A `TestClient` test drives the app through startup, every route including a scan submission and the flip routes, and lifespan shutdown. It asserts `FakeSaneModule.init_call_count` is 1 throughout and `exit_call_count` goes from 0 to 1 only at shutdown. A companion test constructs two backends and asserts one `init` call, with the host warning when the hosts differ.

**Documentation**
- **D-20: `docs/explanation/architecture.md` is updated in the same phase** — the pipeline diagram, "Pipeline Orchestration", "PDF Assembly" (including rewording "byte-for-byte identical to what the scanner produced" to *lossless*), a new "Memory, disk and timeouts" subsection, and "Worker Thread Model" shutdown. Also corrected in-phase: `docs/how-to/troubleshoot-a-failed-scan.md`, `docs/explanation/consume-directory-fallback.md`, `docs/how-to/set-up-adf-duplex.md`, the `min_free_space_mb` reference, and code docstrings naming Phase 29 as future work.

### Claude's Discretion
- Module and type names (`spool.py`, `PageSink`, `PageRecord`, `SpooledPage`) and whether the sink is a `Protocol` or an ABC.
- PNG compression level (D-03), measured rather than guessed. → **Finding 5: use Pillow's default, `compress_level=6`.**
- The exact `_CANCEL_GRACE_SECONDS` value (D-12), and whether the hand-off is a `queue` or an `Event` plus a slot (D-11). → **Finding 6 / Finding 1: `10.0`, `Event` + slot.**
- The exact live-image bound asserted in D-08. → **Finding 4: `<= 2`, asserted equal for N=3 and N=12.**
- Whether the partial-scan exception carries a structured `pages_kept` attribute as well as the message.
- How `MagicMock(spec=ScannerBackend)` call sites in `tests/test_pipeline.py` migrate. → **Finding 7.**
- The exact wording of every new message within Phase 28 D-08's shape.

### Deferred Ideas (OUT OF SCOPE)
- Mid-pass abort during manual duplex pass B.
- A configurable per-page timeout or cancel grace (`scanner.page_timeout_seconds`).
- Uploading a partial scan to Paperless with a warning.
- A degraded `/health` and a status-strip line while the scanner is wedged (Phase 30, APPL-01/02).
- Structured page counts on failed jobs (Phase 30, APPL-03).
- A Paperless connect timeout separate from the upload timeout.
- `PIL.Image.MAX_IMAGE_PIXELS` consolidation (N-10, Phase 32); scanner-test renaming (TEST-04, Phase 32).

## Phase Requirements

| ID | Description (REQUIREMENTS.md:119-125) | Research Support |
|----|---------------------------------------|------------------|
| HARD-01 | Pages spooled to disk as they arrive with an explicit ordered page record; 12-page scan in order 1..12; duplex interleave reorders records; peak memory bounded by one page | Findings 3 (assembly is the unbounded half and how to bound it), 4 (how to prove the bound), 5 (PNG level), Code Examples 2-3 |
| HARD-02 | Mid-batch error after N pages keeps the N pages and reports the error with the count | Finding 8 (`_preserving` extension, exception-type survival, `ScanCancelledError` exclusion) |
| HARD-03 | Timeout waits for the cancelled read to return before closing; a stuck read on a daemon thread never blocks process exit | Findings 1, 2, 6, 9; Code Example 1 |
| HARD-04 | Flatbed shares the ADF timeout and validation | Finding 2 (one helper serves `next(iterator)` and `start()`+`snap()`), Finding 6 (hardware test) |
| HARD-05 | `sane.init()` once per process behind a re-entry guard, `sane.exit()` at shutdown, neither from a request path | Findings 2 (GIL semantics of init/exit/close), 10 (where to close) |

## Project Constraints (from CLAUDE.md)

| Directive | Consequence for this phase |
|-----------|---------------------------|
| Python 3.14, `uv` only (`uv add`, `uv sync`) | Any dependency change is `uv add pikepdf` + `uv.lock`; never `pip` |
| `uv run ruff check .` and `ruff format .` clean | `EM`/`G`/`ANN`/`D`/`PLR0913`/`PLR0915`/`FBT` all enabled; `BLE` and `TRY` are **not** selected, so `except BaseException as exc:` in the reader thread needs no suppression (verified: ruff clean on that pattern) |
| `uv run ty check` **and** `uv run pyrefly check src tests` clean | The `scan_pages` signature change breaks every `ScannerBackend` subclass in `tests/`; both checkers see them (pyrefly is run over `tests` at pre-push) |
| No `# type: ignore` / `# noqa` | The sink contract must be expressible: a `Protocol` in `scanner/base.py` implemented structurally by `spool.SpooledPageSink` |
| Prefer external packages; favour Context7-available libraries | `pikepdf` is already a *transitive runtime* dependency (`img2pdf` requires it unconditionally) — using it directly is not a new install, only a declaration |
| Playwright for all browser checks; never "manual-only" | No browser surface changes here; the D-19 lifespan test is `TestClient`, not Playwright |
| TDD mode (RED commits allowed); commit hooks type-check `src/` only | Test-first plans are fine; never `--no-verify`/`SKIP=` |
| `prek run --all-files` for any pre-flight | Known repo gotcha: `check-ast`/`debug-statements` hooks parse as Python 3.12 and reject PEP 758 bracketless `except` (Phase 28 finding) — avoid `except A, B:` in new code |
| No new `time.sleep` in tests (TEST-02) | D-16's Event-gated fake; `subprocess.run(timeout=…)` and `Event.wait(timeout)` are bounded waits, not sleeps |

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| Acquire a page under a wall-clock bound, cancel, wait, close-or-leave | Scanner backend (`sane_backend.py`) | — | Only the backend owns the SANE handle; the SANE rule "no other operation until the cancelled one returns" is a handle-level invariant |
| Decide *where* a page is materialised, and write it | Pipeline (`spool.py`) | Scanner backend calls the sink | D-01: the backend must not know about `tmp_dir`, workspaces or job ids; the spool is pipeline-owned |
| Per-page disk headroom check | Pipeline (`spool.py`) | — | It owns the directory and knows `min_free_space_mb` |
| Blank-page policy, thumbnails | Pipeline (`pages.py` + spool) | — | Phase 24 D-05: the backend measures facts, the pipeline judges |
| Page order | Page records (data) | — | D-02: order is the tuple's order, never the filesystem's |
| PDF assembly and its memory bound | `pdf.py` | pikepdf/qpdf | Finding 3 |
| Preserving a partial scan | Pipeline (`_preserving`) | `pdf.py` for the partial PDF name | Phase 23's guard already owns `failed/` |
| Process-global SANE init/exit | `sane_backend` module + entry points (`cli.py`, `web/app.py`) | — | Review principle 7: globals are configured and torn down at the entry point |
| Wedge state after an unreturned read | Scanner backend module | Phase 30 reads it for `/health` | D-13 |

## Standard Stack

No new third-party capability is needed. One existing transitive dependency should be declared directly.

### Core

| Library | Version (verified in `.venv`) | Purpose | Why standard |
|---------|------------------------------|---------|--------------|
| `Pillow` | 12.1.1 | Page decode, crop, greyscale stats, PNG spool write | Already the image layer [VERIFIED: `uv pip list`] |
| `img2pdf` | 0.6.3 | Lossless PNG→PDF page embedding (IDAT passthrough) | Already used; D-03 keeps it [VERIFIED: venv source read] |
| `pikepdf` | 10.5.1 (PyPI latest 10.13.0.post1) | Bounded page merge via `pikepdf.Job` (qpdf 12.3.2); PDF read-back in tests | **Already installed at runtime**: `img2pdf` has `Requires-Dist: pikepdf` with no extra marker [VERIFIED: `img2pdf-0.6.3.dist-info/METADATA`, `uv tree --no-dev`] |
| `python-sane` | 2.9.2 | SANE binding | Existing [VERIFIED] |
| stdlib `threading` | 3.14.2 | Daemon reader thread, `Event` hand-off, init lock | Replaces `concurrent.futures` here (Finding 1) |

### Supporting

| Library | Version | Purpose | When to use |
|---------|---------|---------|-------------|
| `pytest-timeout` | 2.4.0, `timeout=60`, `timeout_method="signal"` | Hang guard | Already configured; Finding 1 confirms it interrupts every blocking wait this phase adds |
| stdlib `subprocess` | — | The TEST-02-safe process-exit proof | Finding 9 |
| stdlib `weakref` | — | D-08's live-image counter | Finding 4 |

### Alternatives Considered

| Instead of | Could use | Tradeoff |
|------------|-----------|----------|
| per-page `img2pdf` + `pikepdf.Job` merge | one `img2pdf.convert(all_pages, outputstream=…)` | Simplest, and what D-03 assumed — but memory is linear in page count (787 MB at 48 pages, ~8 GB extrapolated at 500). Keeps the old "one page" claim untrue |
| `pikepdf.Job(["qpdf", …])` | `pikepdf.Pdf.new(); dst.pages.extend(src.pages)` | The Python page-copy path is **not** lazy: 705 MB peak at 48 pages, same as not merging at all (measured) |
| `pikepdf.Job` in-process | shelling out to the `qpdf` CLI | The CLI is not in the slim image; `pikepdf.Job` is the same qpdf, already installed |
| `threading.Thread(daemon=True)` per acquisition | `ThreadPoolExecutor` (current), long-lived or per call | Non-daemon workers are joined at interpreter exit — the hang M-12 measured, reproduced here on 3.14.2 |

**Installation (only if the planner adopts the bounded assembly path):**
```bash
uv add pikepdf          # already present transitively and in uv.lock; this only makes the import declared
```

**Version verification:** `pip index versions pikepdf` → 10.13.0.post1 latest; `.venv` pins 10.5.1 via `img2pdf`. `uv add pikepdf` with no upper bound will keep the resolved 10.5.1 unless the lock is refreshed.

## Package Legitimacy Audit

| Package | Registry | Age | Downloads | Source Repo | slopcheck | Disposition |
|---------|----------|-----|-----------|-------------|-----------|-------------|
| pikepdf | PyPI | 8+ yrs (0.1.0 → 10.13.0.post1) | millions/mo | github.com/pikepdf/pikepdf | `[OK]` | Approved — already installed transitively |

**Packages removed due to slopcheck `[SLOP]`:** none.
**Packages flagged `[SUS]`:** none.
No other package is added by this phase. `slopcheck install pikepdf` ran clean (`1 OK`); note it attempted a `pip install` into the ambient pyenv 3.13 environment as a side effect — nothing was left installed and the project `.venv` was untouched.

## Architecture Patterns

### System architecture diagram (target state)

```
                        ┌──────────── scanner/sane_backend.py ────────────┐
ADF  next(iterator) ┐   │                                                 │
                    ├──▶│ _acquire_with_timeout(dev, work, timeout)       │
flatbed start+snap ─┘   │   ├─ daemon Thread ── work() ──▶ slot + Event   │
                        │   ├─ Event.wait(timeout)                        │
                        │   └─ on timeout/KeyboardInterrupt:              │
                        │        daemon cancel-thread → dev.cancel()      │
                        │        Event.wait(_CANCEL_GRACE_SECONDS)        │
                        │        returned? close : WEDGE + log CRITICAL   │
                        │            (a post-cancel result is DISCARDED)  │
                        │                    │ Image (one at a time)      │
                        │                    ▼                            │
                        │  _validate_page_image (size×bands, no tobytes)  │
                        │  _maybe_crop(page)          ── crop copy ──┐    │
                        │                    │                       │    │
                        │            sink.add(image, pass_label) ◀───┘    │
                        └────────────────────│────────────────────────────┘
                                             ▼  (pipeline-owned)
                        ┌──────────── spool.py (PageSink) ────────────────┐
                        │ free-space check (decoded size + reserve)       │
                        │ image.save(png, compress_level=6)   → disk      │
                        │ Stat(image.convert("L")) → mean, stddev         │
                        │ first page only: thumbnail_callback             │
                        │ returns PageRecord(sequence, path, size, mode,  │
                        │                    mean, stddev)                │
                        └────────────────────│────────────────────────────┘
                                             ▼ records only (no images)
   ScanBatch(pages=(rec,…), actual_resolution, pages_rejected)
                                             │
       _interleave_duplex(records)  ─────────┤  _drop_empty_pages(records, profile)
                                             ▼
                        ┌──────────── pdf.py ─────────────────────────────┐
                        │ per record: img2pdf.convert([str(png)],         │
                        │              layout_fun=fixed_dpi,              │
                        │              outputstream=one_page_pdf)         │
                        │ then pikepdf.Job(["qpdf","--empty","--pages",   │
                        │                   *singles,"--",out]).run()     │
                        └────────────────────│────────────────────────────┘
                                             ▼
      upload ─(failure)─▶ _preserving([pdf], failed_dir)     (existing)
      acquisition failure after N pages ─▶ assemble kept records → failed/…(partial).pdf
      PdfError with records on disk ─▶ move page files → failed/<job-key>/
```

### Recommended module layout

```
src/saneless/
├── spool.py          # NEW: PageRecord, SpooledPageSink (free space, PNG write, stats, thumbnail)
├── scanner/
│   ├── base.py       # PageSink Protocol, ScanBatch.pages -> tuple[PageRecord, ...], ScannerBackend.close()
│   └── sane_backend.py  # _acquire_with_timeout, wedge flag, module init guard, shutdown()
├── pdf.py            # assemble_pdf(records, …) -> per-page convert + qpdf merge
├── pages.py          # is_empty_page(mean, stddev, …) on stored stats; thumbnail without a full copy
└── pipeline.py       # spool lifetime, partial-scan preservation, records everywhere
```

### Pattern 1: bounded acquisition with cancel-then-wait (D-11/D-12/D-15)

**What:** one fresh daemon thread per acquisition; the *cancel* also runs off the waiting thread.
**When:** every blocking SANE call that can hang — `next(iterator)`, `start()`+`snap()`.
**Why the cancel gets its own thread:** in the `net` backend `sane_cancel` is a blocking RPC on the control wire (`sanei_w_call(&s->hw->wire, SANE_NET_CANCEL, …)` *before* `do_cancel()` closes the local data fd). If saned is unreachable — the exact scenario a 120 s timeout fires for — the cancel call itself can block for as long as TCP takes to give up, and it would then hang the *waiting* thread, which is the worker thread. [VERIFIED: sane-backends `backend/net.c:2369-2380`, `do_cancel` at `:666`]

See Code Examples 1. Measured: the whole helper, with a read that never unblocks, lets the process exit in 0.54 s.

### Pattern 2: wedge flag (D-13)

A module-level record of "a reader thread is still inside SANE": set when the grace expires, cleared by the reader thread itself when it finally returns (and it, not anyone else, calls `dev.close()` at that point). Every entry to `scan_pages`/`get_capabilities` checks it first and raises without touching SANE; `shutdown()` (D-18) checks it and skips `sane.exit()`. Store the handle in the wedge record so nothing else can collect it — `SaneDev_dealloc` calls `sane_close()` at GC time, and `_SaneIterator.__del__` calls `device.cancel()`, so dropping the last reference to a wedged handle would violate the very rule D-12 exists to keep. [VERIFIED: `_sane.c:63-68`, `sane.py:118-123`]

### Anti-patterns to avoid

- **Using a result that arrives after a cancel.** Measured on real libsane: after `dev.cancel()` the blocked `snap()` *returned a truncated image* (3779×242 out of a full page) instead of raising. It would pass `_validate_page_image`. The helper must discard any value the slot receives once the timeout path has started.
- **Calling `dev.close()` (or `sane.exit()`) while a read may be outstanding.** `SaneDev_close` and `PySane_exit` run with the **GIL held** and no `Py_BEGIN_ALLOW_THREADS`, and `sane_exit()` closes every open handle by specification.
- **Claiming `outputstream=` bounds memory.** It removes one whole-PDF copy, nothing more (Finding 3).
- **Reconstructing page order from file names or `Path.glob`.** D-02 forbids it; the duplex test is built to punish it.
- **Catching the partial-scan failure with a bare `except Exception` around acquisition.** `ScanCancelledError` is an `Exception` (a direct `SanelessError` child, *not* a `ScanError`), and D-10 says a cancel preserves nothing — it must be re-raised before the preservation branch.

## Don't Hand-Roll

| Problem | Don't build | Use instead | Why |
|---------|-------------|-------------|-----|
| Merging one-page PDFs with bounded memory | A PDF writer, or an incremental-update hack | `pikepdf.Job(["qpdf","--empty","--pages",*paths,"--",out])` | qpdf's native page copy is lazy; flat 131 MB at any page count (measured). Also handles >200 open files by reopening |
| Timeout on a blocking C call | `signal.alarm` (not safe off the main thread), polling `dev.fileno()` | daemon thread + `Event` + `sane_cancel` | The SANE standard's own cancellation contract; `sane_read` releases the GIL so cancel is deliverable |
| Detecting "the page is blank" twice | A second `convert("L")` in `is_empty_page` | `PIL.ImageStat.Stat(image.convert("L"))` once at spool time, stored on the record | D-02/D-06; the conversion is 6 ms and the copy is a third of a page |
| Measuring raw page bytes | `len(image.tobytes())` (a full 26 MB copy) | `width * height * len(image.getbands())` | Exact for `L` and `RGB` — the only modes `snap()` can produce (`sane.py:303`). **Not** exact for mode `"1"`, where `tobytes()` packs 8 px/byte (measured 1300 vs 10000) |
| Thumbnail from a full-page copy | `image.copy(); thumb.thumbnail(...)` (a 26 MB copy) | `PIL.ImageOps.contain(image, (300, 300), LANCZOS)` | Same result, no full-size intermediate (measured 29 ms) |
| Proving the memory bound | Sampling RSS or `tracemalloc` | `weakref` live-image counter | Confirmed: a 26 MB Pillow image adds **460 bytes** to `tracemalloc` (its pixels are malloc'd in C) |

**Key insight:** every hard part of this phase is a *contract* problem (who may call SANE when; who holds a page) rather than an algorithm problem. The library calls are all one-liners; the design work is the ownership rules.

## Findings

### Finding 1 — CPython 3.14 thread lifetime at exit, and pytest-timeout (HIGH, measured)

`concurrent.futures.thread` still ends with `threading._register_atexit(_python_exit)`, and `_python_exit` does `for t, q in items: t.join()` over every worker. Workers are created with plain `threading.Thread(...)` (non-daemon). [VERIFIED: `inspect.getsource` in the project venv, CPython 3.14.2]

Measured (`os.read` on a pipe that is never written, `timeout 5` wrapper):

| Shape | Process exit |
|-------|--------------|
| `ThreadPoolExecutor` worker stuck | **never** (killed at 5 s, rc 124) |
| `threading.Thread(daemon=False)` stuck | **never** (killed at 5 s, rc 124) |
| `threading.Thread(daemon=True)` stuck | **0.24 s**, rc 0 |

So M-12's mechanism is real on 3.14 and D-11's per-call daemon thread is the fix. `docker stop` implications: uvicorn handles SIGTERM → lifespan shutdown → interpreter exit; with the executor the exit blocks until SIGKILL (10 s later), with a daemon thread it does not.

`pytest-timeout` with `timeout_method = "signal"` delivers SIGALRM to the main thread. Measured that a handler raising an exception interrupts **every** wait this phase introduces, in 0.2 s each: `Event.wait()`, `Event.wait(30)`, `SimpleQueue.get()`, `Queue.get(timeout=30)`, and `Thread.join()`. A daemon reader left blocked after such a failure does not delay session exit.

Secondary: `filterwarnings = ["error"]` means an *unhandled* exception in a test-spawned thread becomes an error via `PytestUnhandledThreadExceptionWarning`. The reader thread must therefore catch `BaseException` and store it, never let it escape. `BLE` is not in ruff's `select`, so this needs no suppression (verified with `ruff check`).

### Finding 2 — SANE and python-sane cancellation semantics (HIGH for standard + `_sane.c` + `test` backend; MEDIUM for `net`)

**Standard.** "It is safe to call this function asynchronously (e.g., from within a signal handler)"; "completion of this operation does *not* imply that the currently pending operation has been cancelled. It only guarantees that cancellation has been *initiated*"; and "a frontend must *not* call any other operation until the cancelled operation has returned". `sane_close` performs a `sane_cancel` first if the device is active; `sane_exit` "will first close all device handles that still might be open". [CITED: sane-project.gitlab.io/standard/api.html]

**python-sane 2.9.2 GIL map** [VERIFIED: `_sane.c` v2.9.2 read in full]:

| Call | GIL released? | Note |
|------|---------------|------|
| `SaneDev_start` (`sane_start`) | **yes** | |
| `SaneDev_snap` read loop (`sane_read`) | **yes**, around each scan line | reacquired per line |
| `SaneDev_cancel` (`sane_cancel`) | **yes** | so it can run from another Python thread while a read blocks |
| `PySane_open`, `PySane_get_devices` | yes | |
| `SaneDev_close` (`sane_close`) | **no** | runs holding the GIL |
| `PySane_init` / `PySane_exit` | **no** | run holding the GIL |
| `SaneDev_dealloc` | n/a | calls `sane_close` if `g_sane_initialized` |

Two consequences beyond D-12: (a) `snap()` treats `SANE_STATUS_EOF` as normal completion and returns whatever lines it has, so a cancel that closes the data pipe yields a **truncated image, not an exception**; only a status that is neither GOOD nor EOF raises `_sane.error`. (b) `sane.exit()` while a read is outstanding is doubly unsafe — it closes handles *and* it holds the GIL while doing so.

**Measured against the real `test` backend** (`SANE_CONFIG_DIR` with `dll.conf` = `test`, device `test:0`, ADF source, Gray, 1200 dpi, `read-delay=yes`, `read-delay-duration=200000`, `read-limit=yes`, `read-limit-size=1`):

- the read blocked > 3 s in a worker thread;
- `dev.cancel()` from the main thread returned in **0.000 s**;
- the blocked `snap()` returned **0.0 s later**, with a truncated `(3779, 242)` image (no exception);
- `dev.close()` afterwards returned in 0.000 s.

**`net` backend (MEDIUM — source-read, not executed).** `sane_cancel` = `sanei_w_call(SANE_NET_CANCEL)` on the control wire, then `do_cancel()` closes the local data fd. saned's `do_scan` `select()`s on the control fd while scanning, so a live saned processes a mid-scan cancel and the client's blocked `read()` sees EOF → `sane_read` returns CANCELLED on its next call. But: (1) the cancel RPC is itself blocking, so on a dead link it can hang the caller — hence the cancel-on-its-own-thread recommendation; (2) if what is blocked is `start()` (also a wire RPC) rather than the data read, two threads are using one wire and the protocol exchange can interleave. Treat a timed-out `net` device as poisoned, which is what D-13 already does. [CITED: gitlab.com/sane-project/backends `backend/net.c`, `frontend/saned.c`]

### Finding 3 — img2pdf memory, and how to actually bound assembly (HIGH, measured)

`img2pdf.convert(*images, outputstream=None, **kwargs)` → `convert_to_docobject` loops every input, reads it **fully into memory** (`f.read()` / `read_bytes()`), extracts the PNG IDAT and calls `pdf.add_imagepage(...)`, then `pdf.finalize()`; only afterwards does `tostream(outputstream)` or `tostring()` run. So `outputstream=` removes exactly one whole-PDF `bytes` object and nothing else. [VERIFIED: `img2pdf.py:2905-3104` in the venv]

Measured, 12 and 48 synthetic noisy A4 300 dpi RGB pages (13.7 MB PNG each), peak `ru_maxrss` in a subprocess, base ≈ 32 MB:

| Strategy | 12 pages | 48 pages | Scales with N? |
|----------|----------|----------|----------------|
| `pdf_path.write_bytes(img2pdf.convert(paths, …))` (today) | 454 MB | 1395 MB | yes |
| `img2pdf.convert(paths, …, outputstream=f)` (D-03 as written) | 291 MB | 787 MB | **yes** |
| `pikepdf.Pdf.new()` + `pages.extend(src.pages)` merge | 230 MB | 705 MB | yes |
| **per-page `img2pdf … outputstream=` + `pikepdf.Job(["qpdf","--empty","--pages",…])`** | **131 MB** | **131 MB** | **no** |

The merged output is equivalent: same page count, `/MediaBox [0 0 595.2 841.92]`, same `/FlateDecode` + `/Predictor 15` image stream of exactly 13,696,994 bytes, same PDF version 1.3, same total wall clock (11.2 s vs 9.8 s for 48 pages). PNG embedding stays lossless: img2pdf passes the IDAT chunk straight through for a non-interlaced, non-alpha PNG (`img2pdf.py:2299`), which is what Pillow writes by default.

**Costs of the qpdf merge:** `pikepdf.Job.run()` **holds the GIL** (measured: 0.57 s for a 48-page/657 MB merge with a 1 ms ticker thread getting 1 tick). Extrapolated ~12 ms/page, so a 500-page job would block the event loop for ~6 s during assembly. That is a real but bounded stall, on the same thread that is already blocked for the whole scan; and the alternative is ~8 GB of RAM. `Job.run()` raises pikepdf exceptions on failure — `assemble_pdf`'s existing `except Exception → PdfError` boundary already covers them (EXC-01 unaffected).

**Planner decision required.** D-03 locks "PNG is exactly what img2pdf embeds" and "pass `outputstream=`" — both hold in the per-page+merge shape. What the research contradicts is the *assumption* that `outputstream=` bounds memory. Either adopt the merge (HARD-01's bound becomes true end to end) or keep the single `convert` and reword every memory claim to "one decoded page during scanning; assembly holds the compressed pages". Do not ship the second option with the first option's sentence in `architecture.md`.

### Finding 4 — Pillow memory, `tracemalloc`, and the exact live-image bound (HIGH, measured)

- A 2480×3508 RGB image is 26,099,520 bytes of pixels and adds **460 bytes** to `tracemalloc`'s traced total. D-08's rejection of `tracemalloc` is correct. (Python `bytes`/`bytearray` *are* traced — a 26 MB `bytearray` showed up in full — which is why `snap()`'s intermediate copies would be visible while the `Image` is not.)
- `Image` objects are weak-referenceable and die immediately on the last `del` (refcounting, no cycle). `image.crop(...)` does **not** share the core (`c.im is im.im` → False), so a crop is a genuine second full-size allocation.
- `Image` defines `__eq__` and therefore has no `__hash__`: **`weakref.WeakSet` cannot be used.** Track `list[weakref.ref[Image.Image]]` and count `r() is not None`.

**Recommended bound for D-08:** at each `snap()` the fake counts live issued pages *including the one about to be returned*. The backend's loop variable still references page *k-1* while the helper acquires page *k*, so the honest high-water mark is **2**, and it is 2 whether the run is 3 pages or 12 — assert both: `high_water == high_water_for_3_pages` and `high_water <= 2`. The independence from N is the actual proof; a hard `== 1` would only be reachable by adding a `del` purely to satisfy a test.

Not visible to that counter, and worth one sentence in the docstring: `snap()` itself transiently holds three copies of the page (C `imgBuf`, the `bytearray`, the `bytes()` copy — RGB `frombuffer` copies again; `L` shares the buffer), and `crop`/`convert("L")` each add one. So the real decoded-memory ceiling is ~2-3 pages, constant in N — that is the claim `architecture.md` can defend.

### Finding 5 — PNG compression level (HIGH, measured)

One synthetic noisy A4 300 dpi RGB page (mean 220.0, stddev 66.5 — a realistic light text page with σ≈3 sensor noise), Pillow 12.1.1, this machine:

| `compress_level` | Time | PNG size |
|---|---|---|
| 0 | 0.39 s | 26.1 MB |
| 1 | 0.62 s | 15.8 MB |
| 3 | 0.86 s | 15.6 MB |
| **6 (Pillow default)** | **1.57 s** | **13.7 MB** |
| 9 | 1.57 s | 13.7 MB |

A truly blank page: 0.12 MB at level 1, 0.03 MB at level 6.

**Recommendation: omit the argument and keep Pillow's default 6.** Three reasons: (a) the PNG *is* the PDF's page content, so the 13% saved is saved in the PDF, on the Paperless upload and in Paperless storage forever, while the ~1 s is paid once against a 10-15 s per-page scan; (b) `pdf.py` already saves at the default today, so the produced PDFs stay byte-comparable and no existing size assertion moves; (c) level 3 buys essentially nothing over level 1, so the only real choice is 1 vs 6. If ADF throughput ever matters more than output size, level 1 is the documented fallback — note it in the module docstring rather than adding a config key.

### Finding 6 — `_CANCEL_GRACE_SECONDS`, and an opt-in hardware test (HIGH/MEDIUM)

**Recommend `_CANCEL_GRACE_SECONDS: float = 10.0`**, module-level, injectable exactly like `timeout_per_page`. Rationale, in order: a cooperative backend returns in microseconds (measured: the real `test` backend returned the instant cancel landed); a `net` backend with a live saned returns within one RPC round trip; a `net` backend with a dead link will not return within *any* grace, so a longer wait only delays the operator's error message, and D-13 already makes the wedge recoverable when the read does eventually return. 10 s also sits well inside the web worker's own bounds (`STOP_JOIN_SECONDS = 5.0` is shorter, so a shutdown during the grace still returns promptly because the worker thread is a daemon) and inside `pytest-timeout`'s 60 s.

**Hardware test (opt-in, `tests/test_sane_hardware.py`).** The real `test` backend can be made to block a read for as long as needed:

```python
dev.read_delay = True
dev.read_delay_duration = 200_000   # µs, the option's maximum
dev.read_limit = True
dev.read_limit_size = 1             # bytes per sane_read
dev.resolution = 1200               # enough lines that the delay repeats
```
Measured: `snap()` still blocked after 3 s; `dev.cancel()` from another thread returned immediately and the read returned immediately after. Note the delay lives in the backend's reader child (`test.c:1501`, one `usleep` per buffer written), so a low resolution finishes in one buffer and *cannot* be made slow — the high resolution is load-bearing. This test is the only place the whole D-12 sequence is proven against real libsane; keep it behind the `sane_hardware` marker.

### Finding 7 — Blast radius and a parallelizable migration (HIGH, enumerated)

Counts are occurrences per file (`grep -c`), current tree.

**`scan_pages` (signature → `(device_id, settings, sink)`)**

| File | Count | Nature |
|------|-------|--------|
| `src/saneless/scanner/base.py` | 1 | ABC declaration |
| `src/saneless/scanner/sane_backend.py` | 6 | implementation + docstrings |
| `src/saneless/pipeline.py` | 5 | `_scan_simplex`, `_scan_manual_duplex` ×2, docstrings |
| `tests/test_scanner.py` | 105 | direct backend calls + `.pages` assertions (48 `.pages`) |
| `tests/test_pipeline.py` | 50 | 36 `MagicMock(spec=ScannerBackend)` sites |
| `tests/test_worker.py` | 11 | 2 hand-written `ScannerBackend` subclasses (`_PassBGatedScanner`, `_GatedScanner`) |
| `tests/test_cli.py` | 8 | 4 subclasses (`MockSaneBackend`, `FailScanner`, `CountingScanner`, `RaisingScanner`) |
| `tests/test_outcomes_e2e.py` | 5 | 2 `MagicMock(spec=…)` |
| `tests/test_sane_hardware.py` | 3 | real-backend calls, `.pages` ×2 |
| `tests/test_browser.py` | 3 | `_BrowserTestScanner` (gate) |
| `tests/conftest.py` | 2 | `scan_batch()` helper + `mock_scanner` fixture |
| `tests/test_web.py`, `test_web_errors.py`, `test_cross_origin.py`, `test_web_state_rendering.py`, `test_app_lifespan.py` | 1 each | one-page `StubScanner` classes |
| `docs/explanation/architecture.md` | 3 | prose |

**`ScanBatch(` construction:** `sane_backend.py` ×1, `pipeline.py` ×2, `conftest.py` ×1 (the `scan_batch` helper), plus 8 test files ×1-2 (the stub scanners above).
**`assemble_pdf(`:** `pdf.py` ×1, `pipeline.py` ×3, `tests/test_pdf.py` ×18, `tests/test_scanner.py` ×1.
**`SaneBackend(`:** `cli.py` ×4 (`scan` :543, `devices` :640, `serve` :761, `auto-profiles` :861), `tests/test_scanner.py` ×37, `tests/test_sane_hardware.py` ×5, `tests/test_pipeline.py` ×2, `tests/test_cli.py` ×1.
**Fake/module doubles:** `tests/fake_sane.py` (`FakeSaneDev.snap/start/cancel/close`, `FakeSaneModule.init/exit`, `set_page_delay`, `load_feeder`, `_page_image`).
**Pages helpers:** `pages.py` ×5, `pipeline.py` ×4, `tests/test_pages.py` ×22, `tests/test_pipeline.py` ×2.
**Duplex internals:** `pipeline.py` ×20 (`_interleave_duplex`, `_drop_empty_pages`, `_DuplexMismatch`, `_handle_duplex_mismatch`), tests ×7.

**Recommended migration shape (keeps plans parallel):**

1. **Wave 1, one plan, one commit: the contract.** `PageRecord` + `PageSink` in `scanner/base.py`, `spool.py`, the new `ScanBatch.pages` type, the `scan_pages` signature, `ScannerBackend.close()`, **and** the conftest test seam — all together. This is a single atomic type-checker event: `ty`/`pyrefly` fail on every stub until each is updated, so splitting it produces a red tree between plans.
2. **The conftest seam is the lever.** 36 of the 50 `test_pipeline.py` sites already go through `conftest.scan_batch(...)`. Replace it with a callable factory used as `side_effect`, so most sites change by one word (`return_value=scan_batch(x)` → `side_effect=spooling(x)`), and multi-pass sites take `spooling_in_turn(fronts, backs)` (a `side_effect` **list** of callables is *not* called by `MagicMock` — one dispatching callable is required). A shared `conftest.StubScannerBackend` base gives the seven web/CLI/worker stub classes a one-line `scan_pages` override.
3. **Waves 2+ can then be parallel and per-area:** (a) `sane_backend` timeout/cancel/wedge, (b) `sane_backend` init guard + `shutdown()` + entry points, (c) `pdf.py` assembly, (d) pipeline preservation rules, (e) docs. Each touches a disjoint file set except `sane_backend.py`, whose two plans should be sequenced.
4. **`tests/test_scanner.py` (105 sites) is the long pole** and is mostly mechanical `.pages` → records. Consider a test-side helper `images_of(batch)` that opens the spooled PNGs so existing image assertions survive with a one-line change.

### Finding 8 — Preservation, exception identity, and `_warn_if_failed_dir_growing` (HIGH, read)

- `_preserving` re-raises `type(exc)(msg) from exc` for any `SanelessError`, so a `ScanError` stays a `ScanError` → `classify_error` still returns `SCANNER` → exit 1 and `ErrorCategory.SCANNER` hold (Phase 28 D-07). A non-`SanelessError` becomes a `PaperlessError`, which for the new acquisition-failure path would be a lie; the partial-scan guard should therefore be its own narrow helper that reuses `_preserving`'s *move + message* shape rather than wrapping acquisition in `_preserving` itself.
- **`ScanCancelledError` is an `Exception`** (direct `SanelessError` child) — a blanket `except Exception` around acquisition would preserve a cancel, which D-10 forbids. Re-raise it first. `KeyboardInterrupt` is a `BaseException` and already passes through untouched.
- The worker's `except Exception` at `worker.py:1349` keeps the three-way ending (shutdown / cancelled / error) and calls `classify_error(exc)` — unchanged by this phase as long as the type survives.
- `_warn_if_failed_dir_growing` counts `failed_dir.glob("*.pdf")` and sums `stat().st_size`. For D-10's page-file directories it must also count directories and sum them recursively (`sum(f.stat().st_size for f in d.rglob("*") if f.is_file())`), still inside the existing `except OSError: return` and still never raising.
- `_open_workspace` returns a `TemporaryDirectory` whose `__exit__` deletes the spool. Anything preserved (partial PDF, page directory) must be **moved out before** `run_pipeline`'s `with workspace` block exits — the same trap Phase 23 named, now with a second kind of artefact.
- `_preserving` uses `shutil.move` to an explicit destination path because `data_dir` and `tmp_dir` may be on different filesystems; a page **directory** move has the same constraint and `shutil.move` handles it (`copytree`+`rmtree` fallback), but the destination must not already exist — key it on the job id like `build_pdf_filename` does.

### Finding 9 — Process-exit proof in a subprocess, no `time.sleep` (HIGH, measured)

Pattern (verified end to end with a real blocking C call and the prototype helper — the child printed its line and the process exited in 0.54 s):

```python
def test_a_stuck_read_never_blocks_process_exit(tmp_path: Path) -> None:
    child = tmp_path / "wedged.py"
    child.write_text(CHILD_SOURCE, encoding="utf-8")
    result = subprocess.run(                      # noqa-free: bounded wait, not a sleep
        [sys.executable, str(child)],
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0
    assert "returned=False cancels=1 closes=0" in result.stdout
```

Notes: `subprocess.run(timeout=…)` is a bounded wait, not a `time.sleep`, so TEST-02 is satisfied; a `TimeoutExpired` *is* the failure this test exists to catch, so let it raise. The child must block in a call that releases the GIL — `os.read` on an unwritten pipe is the honest stand-in for `sane_read`; a `threading.Event().wait()` would also work but proves less. Run the child with the same `sys.executable` so it uses the project venv; `saneless` is installed in editable mode via `saneless.pth`, so imports resolve without `PYTHONPATH` fiddling.

### Finding 10 — Where each entry point closes the backend (HIGH, read)

- **click 8.3.1.** `ctx.call_on_close(fn)` callbacks run when the subcommand's `Context` exits its `with` block inside `MultiCommand.invoke` — i.e. on success, on `ctx.exit()` (a `click.exceptions.Exit`), and while an exception propagates. That is *before* `_GuardedGroup.invoke`'s handlers run, so the backend is already shut down when the error line is printed and the exit code chosen. Recommendation: `ctx.call_on_close(scanner.close)` immediately after each `SaneBackend(...)` in `scan`, `devices` and `auto-profiles` — one line per site, no `try`/`finally` indentation churn, and it cannot be skipped by an early `ctx.exit()` path. `close()` must never raise (log and swallow), because a raise from a close callback would replace the real error.
- **`serve`.** The backend is constructed in `cli.py:761` and handed to `create_app`; the close belongs in the lifespan, at `web/app.py:134`, in the branch that already runs only after `worker.stop()` returned `True`, ordered `paperless.close(); job_store.close(); scanner.close()`. The `return` at `:133` (worker did not stop) must **not** close — that is the same "a thread may still be inside SANE" rule, and it is already the shape of the surrounding code. A second skip inside `shutdown()` guards the D-13 wedge.
- `SaneBackend` is constructed 4× in `cli.py`; after D-17 only the first `sane.init()` in a process does anything, so `serve`'s construct-then-fail path (`ScanError` → `ConfigError`, exit 2) is unchanged.

### Finding 11 — Doc sentences this phase makes false (HIGH, enumerated)

| File:line | Sentence | Why it changes |
|-----------|----------|----------------|
| `docs/explanation/architecture.md:12` | `Scanner -> [SaneBackend] -> PIL Images -> [Empty Page Filter] -> [img2pdf] -> PDF -> …` | Pages become spooled files + ordered records (D-20) |
| `architecture.md:40` | "the image data in the PDF is byte-for-byte identical to what the scanner produced" | The PNG re-encode means *lossless*, not byte-identical (D-20) |
| `architecture.md` §"Pipeline Orchestration" (22-37) | no mention of spooling or of what a mid-batch failure keeps | D-09/D-10 |
| `architecture.md` §"PDF Assembly" (38-41) | no mention of streaming/bounded memory | D-03 + Finding 3 wording |
| `architecture.md` §"Worker Thread Model" (48-63) | shutdown order says nothing about SANE | D-18 |
| `architecture.md:62` | "The backend opens and closes the device inside each `scan_pages()` call, so the scanner handle is already released between the two passes" | Still true *unless* wedged — needs the D-12/D-13 exception |
| `docs/how-to/set-up-adf-duplex.md:95` and `:97` | "Nothing is uploaded in either case" / flip-timeout failure text | Still not uploaded, but the fronts are now **preserved** (D-10) |
| `set-up-adf-duplex.md:162` | "saneless does not discard your scans" (mismatch only) | Now the general rule for any pass-B failure |
| `docs/how-to/troubleshoot-a-failed-scan.md:47-48` | flip-timeout entry | Add "the fronts already scanned are preserved at …" |
| `troubleshoot-a-failed-scan.md` (new rows) | — | Needs a "restart saneless" wedge symptom (D-13) and a partial-scan row (D-09) |
| `docs/explanation/consume-directory-fallback.md:62` | "saneless moves the assembled PDF into `failed/`" | `failed/` now also holds partial PDFs and page-file directories (D-10) |
| `docs/reference/configuration.md:70` | "Minimum free disk space (MB) required before a scan starts" | Now also the per-page reserve (D-07) |
| `docs/reference/environment-variables.md:55` | `SANELESS_OUTPUT__MIN_FREE_SPACE_MB` row | same |
| `docs/reference/docker.md:52-54` | "Preserved scans in `failed/`" | Mention partial scans and directories |
| Code docstrings | `_MAX_ADF_PAGES` (`sane_backend.py:115-136`), `ScanBatch` (`base.py:194-230`), `ScannerBackend.scan_pages` (`base.py:260`), `SaneBackend.scan_pages` (`:1325`), module docstring line 7, `pipeline.py:1080` | All name Phase 29 as future work |

**Doc-truth tests:** the pinning tests live in `tests/test_deployment_config.py` (it already imports `ARCHITECTURE`, used once at `:238` for a cross-page scan). **No existing doc-truth test pins any of the sentences above**, so none will fail — which also means nothing will catch a doc that is left stale. Adding one assertion for the new "Memory, disk and timeouts" subsection is cheap and matches the phase's own standard.

## Runtime State Inventory

*This phase is a refactor with no rename and no persisted-format change, but the categories are answered explicitly rather than skipped.*

| Category | Items found | Action required |
|----------|-------------|-----------------|
| Stored data | Job rows in `saneless.db` store `error`, `category`, `pages_*` — no schema change (page counts on failed jobs are Phase 30/APPL-03). `ScanBatch`/`PageRecord` are in-process only | none |
| Live service config | None. No external service holds saneless state | none — verified: no n8n/systemd/scheduler integration in the tree |
| OS-registered state | None (the container `ENTRYPOINT`/`HEALTHCHECK` are unchanged) | none |
| Secrets/env vars | `SANE_NET_HOSTS` is now set **once per process** by the init guard; a second `SaneBackend(host=…)` warns instead of silently doing nothing. No new env var | code only (D-17) |
| Build artifacts | `<data_dir>/failed/` gains a new artefact *kind* (page directories) that no prior version wrote; `_warn_if_failed_dir_growing` and the docs must account for it. `tmp_dir` gains per-job spool subdirectories, deleted with the workspace | doc + code (D-10, Finding 8) |

## Common Pitfalls

### Pitfall 1: spooling a page that arrived after the cancel
**What goes wrong:** the timeout fires, cancel lands, the reader returns a half-read page, and it becomes page N of the PDF.
**Why:** `snap()` returns truncated data on `SANE_STATUS_EOF` (measured on real libsane), and `_validate_page_image` accepts it (3779×242 ≈ 914 KB > `_MIN_PAGE_BYTES`).
**Avoid:** once the timeout path starts, the slot's value is discarded unconditionally; only the reader's *return* (for close-vs-wedge) is consulted.
**Warning sign:** a timed-out job whose partial PDF has one more page than the error message claims.

### Pitfall 2: the cancel itself blocks the worker
**What goes wrong:** `dev.cancel()` is called from the waiting thread; on `net` with a dead saned it blocks on the control wire and the worker thread hangs anyway.
**Avoid:** cancel from a separate daemon thread; the grace wait bounds the *reader*, not the cancel.
**Warning sign:** a timeout `ScanError` that never reaches the log.

### Pitfall 3: letting a wedged `SaneDev` be garbage-collected
**What goes wrong:** `SaneDev_dealloc` calls `sane_close()` (GIL held) and `_SaneIterator.__del__` calls `device.cancel()` — both are "another operation while a read is outstanding".
**Avoid:** the wedge record keeps a strong reference to the handle *and* the iterator until the reader returns.

### Pitfall 4: assuming `outputstream=` fixed the memory
**Avoid:** Finding 3. Either merge per page, or reword the claim.

### Pitfall 5: `MagicMock` `side_effect` lists of callables
**What goes wrong:** `scanner.scan_pages.side_effect = [spool_fronts, spool_backs]` **returns the function objects**; `unittest.mock` only calls a `side_effect` that is itself callable, not items of an iterable.
**Avoid:** one dispatching callable that tracks its own call count.

### Pitfall 6: `weakref.WeakSet` of `Image`
**What goes wrong:** `TypeError: unhashable type: 'Image'` — Pillow defines `__eq__` without `__hash__`.
**Avoid:** a list of `weakref.ref`.

### Pitfall 7: the byte-count formula and mode `"1"`
**What goes wrong:** `w*h*bands` is 8× `len(tobytes())` for a bilevel image, loosening `_MIN_PAGE_BYTES`.
**Avoid:** it cannot happen through `snap()` (L/RGB only) — but say so in the docstring, and have the fake keep producing RGB/L.

### Pitfall 8: `filterwarnings = ["error"]` and thread exceptions
**Avoid:** the reader thread catches `BaseException` into the slot; nothing escapes `threading.excepthook`.

### Pitfall 9: PEP 758 bracketless `except` and the prek hooks
**Avoid:** the repo's `check-ast`/`debug-statements` hooks parse as Python 3.12 (Phase 28 finding). Write `except (A, B) as exc:` or a tuple constant.

## Code Examples

### 1. `_acquire_with_timeout` (prototype run and verified: cancel fired, close skipped, process exited in 0.54 s)

```python
_CANCEL_GRACE_SECONDS: float = 10.0


class _Slot:
    """The one value a reader thread hands back, or the one it raised."""

    __slots__ = ("error", "value")

    def __init__(self) -> None:
        self.value: object = None
        self.error: BaseException | None = None


def _acquire_with_timeout(
    dev: SaneDevice,
    work: Callable[[], Image.Image],
    page_label: str,
    timeout: float,
    grace: float = _CANCEL_GRACE_SECONDS,
) -> Image.Image:
    done = threading.Event()
    slot = _Slot()

    def read() -> None:
        try:
            slot.value = work()
        except BaseException as exc:          # handed to the waiter, never escapes
            slot.error = exc
        finally:
            done.set()

    reader = threading.Thread(target=read, name=f"sane-read-{page_label}", daemon=True)
    reader.start()
    try:
        finished = done.wait(timeout)
    except KeyboardInterrupt:                  # D-15: same device-safe path
        _cancel_and_settle(dev, done, grace)
        raise
    if finished:
        if slot.error is not None:
            raise slot.error                   # StopIteration included, by design
        return _as_image(slot.value)
    returned = _cancel_and_settle(dev, done, grace)   # D-12; a late value is discarded
    ...                                        # close-or-wedge, then raise ScanError
```

`_cancel_and_settle` starts a second daemon thread that calls `dev.cancel()` (suppressing and logging any failure) and returns `done.wait(grace)`.

### 2. Bounded assembly (measured: 131 MB peak at 12 and at 48 pages)

```python
# Source: measured against img2pdf 0.6.3 + pikepdf 10.5.1 / qpdf 12.3.2 in this venv
layout = img2pdf.get_fixed_dpi_layout_fun((dpi, dpi))
singles: list[str] = []
for record in records:
    single = work_dir / f"{record.sequence:04d}.pdf"
    with single.open("wb") as stream:
        img2pdf.convert([str(record.path)], layout_fun=layout, outputstream=stream)
    singles.append(str(single))

pikepdf.Job(["qpdf", "--empty", "--pages", *singles, "--", str(pdf_path)]).run()
```

### 3. Spool-time stats and thumbnail without a second full-page copy

```python
# Source: Pillow 12.1.1, measured -- convert 6 ms, Stat 3 ms, contain 29 ms
grey = image.convert("L")
stats = Stat(grey)
record = PageRecord(
    sequence=sequence,
    path=png_path,
    size=image.size,
    mode=image.mode,
    mean=stats.mean[0],
    stddev=stats.stddev[0],
)
# first page only -- ImageOps.contain returns a small copy; image.copy() would be 26 MB
if sequence == 1 and thumbnail_callback is not None:
    thumbnail_callback(encode_jpeg(ImageOps.contain(image, (300, 300), Resampling.LANCZOS)))
```

### 4. Raw byte count without `tobytes()` (D-06)

```python
raw_size = page_image.size[0] * page_image.size[1] * len(page_image.getbands())
```
Exact for `L` and `RGB` — verified 26,099,520 == `len(im.tobytes())`. `snap()` produces only those two modes (`sane.py:303`).

## State of the Art

| Old approach | Current approach | When changed | Impact |
|--------------|------------------|--------------|--------|
| `concurrent.futures` for timeouts on blocking C calls | daemon thread + explicit cancellation | unchanged since 3.9; `_python_exit` still joins workers in 3.14.2 | The executor is the bug, not a detail |
| `img2pdf.convert(..., outputstream=)` treated as streaming | still buffers the whole document | unchanged through 0.6.3 (latest) | Finding 3 |
| `pikepdf.Pdf.pages.extend` for merges | `pikepdf.Job` (native qpdf) for bounded merges | pikepdf ≥ 3 exposes `Job`; typed via `py.typed` + `_core.pyi` | Finding 3 |
| Daemon threads killed abruptly at shutdown | 3.14 hangs them at the GIL instead of `pthread_exit` | CPython 3.14 | No crash, no block — the desired behaviour |

**Deprecated/outdated:** nothing in this phase's stack is deprecated. `img2pdf` 0.6.3 is the latest release.

## Assumptions Log

| # | Claim | Section | Risk if wrong |
|---|-------|---------|---------------|
| A1 | `net`-backend `sane_cancel` can block on the control wire when saned is unreachable | Finding 2 / Pattern 1 | If wrong, the extra cancel thread is harmless overhead. If right and ignored, the worker hangs despite the whole phase |
| A2 | A concurrent `sane_cancel` while `sane_start` is outstanding on `net` can confuse the wire | Finding 2 | Argues for treating a timed-out device as poisoned (D-13 already does) |
| A3 | Real scanned pages compress like the σ≈3-noise synthetic page (13.7 MB/page at 300 dpi colour) | Finding 5 | The level-6 recommendation is insensitive to this; only the absolute MB figures quoted in docs would move. M-08's own measurement (26 MB RAM, 10.6 MB PNG) is the same order |
| A4 | `pikepdf.Job.run()` holding the GIL for ~12 ms/page is acceptable for a 500-page job (~6 s) | Finding 3 | A long assembly stalls `/api/status` polling; visible as a frozen UI, not as data loss |
| A5 | Click's `ctx.call_on_close` runs before `_GuardedGroup.invoke`'s handlers | Finding 10 | If wrong, `sane.exit()` would run after the error line — cosmetic ordering only |

## Open Questions (ALL RESOLVED — resolutions recorded in 29-CONTEXT.md and carried into the plans)

1. **Does D-03 stand as written, or gain the qpdf merge?** — **RESOLVED: gains the merge.** CONTEXT.md D-03 amended 2026-09-15; delivered by plan 29-08.
   - Known: `outputstream=` alone leaves assembly memory linear in page count (measured, both engines).
   - Unclear: whether the phase prefers a true bound (adds `pikepdf` as a declared dependency and a GIL-holding merge step) or an honest weaker claim.
   - Recommendation: take the merge. It keeps D-03's two locked clauses literally true, keeps the PDF byte-equivalent, and is the only version of the phase in which "peak memory stays bounded by roughly one page" is a true sentence end to end. Flag it to the user as the one research-driven amendment.

2. **Is `snap()`'s truncated-on-cancel return worth a dedicated test?** — **RESOLVED: yes.** CONTEXT.md D-12/D-16 amended; plan 29-07's `post_cancel_page_discarded` test.
   - Known: measured on the real `test` backend; the fake currently cannot express it.
   - Recommendation: yes — `FakeSaneDev`'s blocking mode should support both "unblocks by *returning a partial page*" and "unblocks by raising", because the first is what real hardware does and the second is what the code was written to expect.

3. **How does D-13's wedge interact with `get_capabilities` in `auto-profiles`?** — **RESOLVED: check both entry points.** Plan 29-10 guards `scan_pages` and `get_capabilities`.
   - Known: D-13 says both refuse; `auto-profiles` constructs its own backend.
   - Unclear: whether a wedge in a *previous* CLI process matters — it cannot, the flag is per process. Only `serve` can observe it.
   - Recommendation: keep the check in both entry points anyway; it is one `if`.

## Environment Availability

| Dependency | Required by | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| CPython | everything | ✓ | 3.14.2 | — |
| `uv` | all commands | ✓ | project standard | — |
| libsane + `test` backend | `sane_hardware` tests, Finding 6 | ✓ | `sane_init` → version 16777248 (1.0.32) | tests are marker-gated |
| `pikepdf`/qpdf | bounded assembly | ✓ | 10.5.1 / qpdf 12.3.2 | already transitive |
| `pytest-timeout` | hang guard | ✓ | 2.4.0 | — |
| Playwright/chromium | browser suite (untouched here) | not probed | — | `-m "not browser"` |

**Missing dependencies with no fallback:** none.

## Validation Architecture

### Test framework

| Property | Value |
|----------|-------|
| Framework | pytest 9.0.2 + pytest-timeout 2.4.0 |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` (`timeout = 60`, `timeout_method = "signal"`, `filterwarnings = ["error"]`, `xfail_strict`) |
| Quick run command | `uv run pytest tests/test_scanner.py tests/test_spool.py -x -q` |
| Full suite command | `uv run pytest -m "not browser and not sane_hardware"` |
| Hardware suite | `uv run pytest -m sane_hardware` |

### Success criteria → test map

| Criterion / Req | Behaviour | Test type | Automated command | File exists? |
|---|---|---|---|---|
| SC1 / HARD-01 | 12-page scan yields records `sequence` 1..12 and a PDF whose pages carry per-page content in order (pikepdf read-back) | integration | `uv run pytest tests/test_pipeline.py -k twelve_page_order -x` | ❌ Wave 0 (`tests/test_pipeline.py` new class) |
| SC1 / HARD-01 | duplex interleave reorders **records** while the spool file names sort differently | unit | `uv run pytest tests/test_pipeline.py -k interleave_records -x` | ✅ extend existing `_interleave_duplex` tests |
| SC1 / HARD-01 | live-image high-water ≤ 2 and identical for 3 and 12 pages (weakref counter in `FakeSaneDev`) | unit | `uv run pytest tests/test_scanner.py -k live_page_images -x` | ❌ Wave 0 (fake + test) |
| SC1 / HARD-01 | one decoded page per sink call: `sink.add` sees exactly one image and the backend holds no list | unit | `uv run pytest tests/test_scanner.py -k sink -x` | ❌ Wave 0 |
| SC2 / HARD-02 | error on page N+1 → job ERROR, original type, message names the count and the `failed/` path, N pages in the partial PDF | integration | `uv run pytest tests/test_outcomes_e2e.py -k partial_scan_preserved -x` | ❌ Wave 0 |
| SC2 / HARD-02 | pass-B failure preserves fronts as `(fronts)` partial; flip timeout likewise | integration | `uv run pytest tests/test_pipeline.py -k pass_b_preserves_fronts -x` | ❌ Wave 0 |
| SC2 / HARD-02 | a cancel preserves **nothing** | integration | `uv run pytest tests/test_pipeline.py -k cancel_preserves_nothing -x` | ❌ Wave 0 |
| SC2 / D-10 | `PdfError` moves the page files to `failed/<job>/` and the growth warning counts them | unit | `uv run pytest tests/test_pipeline.py -k failed_dir_counts_directories -x` | ❌ Wave 0 |
| SC3 / HARD-03 | `cancel()` called, `close()` **not** called while the read is blocked, `close()` called once it returns | unit | `uv run pytest tests/test_scanner.py -k close_not_called_while_blocked -x` | ❌ Wave 0 (Event-gated fake) |
| SC3 / HARD-03 | a never-returning read → CRITICAL log, no `close()`, `ScanError` naming the unresponsive cancel | unit | `uv run pytest tests/test_scanner.py -k did_not_respond_to_cancel -x` | ❌ Wave 0 |
| SC3 / HARD-03 | the process still exits (subprocess, bounded by `subprocess.run(timeout=20)`) | integration | `uv run pytest tests/test_scanner.py -k process_exits -x` | ❌ Wave 0 (Finding 9 pattern) |
| SC3 / D-13 | a wedged backend refuses the next `scan_pages`/`get_capabilities` with no SANE call, and recovers when the read returns | unit | `uv run pytest tests/test_scanner.py -k wedge -x` | ❌ Wave 0 |
| SC3 / D-15 | `KeyboardInterrupt` mid-read cancels, waits, closes, re-raises; CLI still exits 130 | unit | `uv run pytest tests/test_scanner.py -k keyboard_interrupt_mid_read -x` | ❌ Wave 0 |
| SC4 / HARD-04 | flatbed `start()+snap()` times out with the same message shape as the ADF path | unit | `uv run pytest tests/test_scanner.py -k flatbed_timeout -x` | ❌ Wave 0 (mirror `test_page_timeout_raises_scan_error`) |
| SC4 / HARD-04 | flatbed page validation is fatal and shares `_validate_page_image` | unit | `uv run pytest tests/test_scanner.py -k flatbed_unreadable -x` | ✅ exists (assert it still passes through the shared helper) |
| SC5 / HARD-05 | two `SaneBackend()` constructions → one `init`; differing hosts → WARNING naming both | unit | `uv run pytest tests/test_scanner.py -k init_once -x` | ❌ Wave 0 (replaces the over-claiming TEST-04 test) |
| SC5 / HARD-05 | `TestClient` through startup, every route incl. scan submit + flip, shutdown: `init_call_count == 1` throughout, `exit_call_count` 0→1 only at shutdown | integration | `uv run pytest tests/test_app_lifespan.py -k sane_lifecycle -x` | ❌ Wave 0 |
| SC5 / D-18 | each CLI command closes the backend on success and on error | unit | `uv run pytest tests/test_cli.py -k closes_the_backend -x` | ❌ Wave 0 |
| D-03/D-07 | per-page disk shortfall → `ScanError` naming page and path; no raw `OSError` | unit | `uv run pytest tests/test_spool.py -x` | ❌ Wave 0 (new file) |
| D-20 | architecture page states the memory/timeout rules | doc-truth | `uv run pytest tests/test_deployment_config.py -k architecture -x` | ❌ Wave 0 (optional but recommended) |
| Real hardware | cancel unblocks a genuinely slow `test`-backend read; `close()` then succeeds | integration (opt-in) | `uv run pytest -m sane_hardware -k cancel -x` | ❌ Wave 0 (`tests/test_sane_hardware.py`) |

### Sampling rate

- **Per task commit:** `uv run pytest tests/test_scanner.py tests/test_spool.py tests/test_pdf.py -x -q` (the files each task touches) — seconds.
- **Per wave merge:** `uv run pytest -m "not browser and not sane_hardware"` plus `uv run ruff check . && uv run ty check && uv run pyrefly check src tests`.
- **Phase gate:** full suite green + `uv run pytest -m sane_hardware` + `uv run pytest -m browser` before `/gsd-verify-work`.

### Wave 0 gaps

- [ ] `tests/test_spool.py` — new file, covers D-02/D-06/D-07
- [ ] `tests/fake_sane.py` — Event-gated blocking read (unblock-by-return, unblock-by-raise, never-unblock), `close_while_blocked` / `exit_while_blocked` flags, weakref issue log, lazy distinct page content
- [ ] `tests/conftest.py` — `spooling()` / `spooling_in_turn()` `side_effect` factories and a `StubScannerBackend` base for the seven stub classes
- [ ] subprocess child script for the exit proof (inline source constant, written to `tmp_path`)
- [ ] no framework install needed

## Security Domain

`security_enforcement` is not set in `.planning/config.json`, so it is treated as enabled.

### Applicable ASVS categories

| ASVS category | Applies | Standard control |
|---------------|---------|------------------|
| V2 Authentication | no | no auth surface changes |
| V3 Session management | no | — |
| V4 Access control | no | — |
| V5 Input validation | **yes** | Spool file names are derived from an integer `sequence` and a fixed pass label — never from the operator's title. The partial-PDF name and the `failed/<job>/` directory name go through `build_pdf_filename` / `sanitise_title_for_filename` (allow-list, 60-char cap) exactly as today |
| V6 Cryptography | no | — |
| V12 File handling | **yes** | New writes land inside the per-job `TemporaryDirectory`; preserved artefacts land in `failed/` via `shutil.move` to an explicit path (never a bare directory — `shutil.Error` is not an `OSError` and would escape the guard) |
| V7 Error handling & logging | **yes** | Messages name paths and counts; no token or secret is in scope. The CRITICAL wedge log must not include device credentials — `device_id` only |

### Known threat patterns for this stack

| Pattern | STRIDE | Standard mitigation |
|---------|--------|---------------------|
| Path traversal through a job title reaching `failed/` | Tampering | existing allow-list sanitiser, unchanged |
| Disk exhaustion by a runaway feed | Denial of service | `_MAX_ADF_PAGES` (500) + D-07's per-page free-space check + the `min_free_space_mb` reserve |
| Decompression-bomb PNG on the *read* side | DoS | Not reachable: the spool only ever reads back files it wrote; `MAX_IMAGE_PIXELS` is already raised in three modules (N-10, Phase 32) |
| A stuck reader thread holding a device forever | DoS | D-13's wedge refuses instead of queueing more work; daemon threads never block shutdown |
| Use-after-free / segfault from `sane_close` during `sane_read` | Tampering (memory safety) | D-12's cancel-then-wait-then-close-or-leave; verified GIL map in Finding 2 |

## Sources

### Primary (HIGH confidence)
- `.venv/lib/python3.14/site-packages/sane.py` (python-sane 2.9.2) — `_SaneIterator`, `SaneDev.__setattr__/snap/cancel/close`, `init/exit`
- `python-pillow/Sane` `_sane.c` v2.9.2 (downloaded, read in full) — GIL map, `snap` read loop, EOF/cancel handling
- `.venv/.../img2pdf.py` 0.6.3 — `convert`, `convert_to_docobject`, PNG IDAT passthrough
- `.venv/.../PIL/PngImagePlugin.py`, `PIL/Image.py` 12.1.1 — PNG `exif` only from `encoderinfo`, `thumbnail`/`reduce`
- CPython 3.14.2 `concurrent/futures/thread.py`, `threading.py` (via `inspect.getsource` in the venv)
- SANE Standard API (sane-project.gitlab.io/standard/api.html) — `sane_cancel`, `sane_read`, `sane_close`, `sane_exit`, reentrancy
- sane-backends `backend/net.c`, `backend/test.c`, `frontend/saned.c` (downloaded from gitlab.com/sane-project/backends)
- Live execution in this venv: exit-behaviour matrix, signal interruption matrix, PNG level benchmark, four assembly-memory strategies, `tracemalloc`/weakref probes, real `test:0` cancel-during-read run, `_acquire_with_timeout` prototype
- This repository: `src/saneless/{scanner/base.py,scanner/sane_backend.py,pipeline.py,pdf.py,pages.py,worker.py,cli.py,web/app.py,exceptions.py,vocabulary.py}`, `tests/{conftest.py,fake_sane.py,test_sane_hardware.py,test_deployment_config.py}`, `docs/`, `.planning/reviews/2026-09-09-code-review.md`

### Secondary (MEDIUM confidence)
- `net.c` + `saned.c` reasoning about a cancel arriving mid-scan (read, not executed against a live saned)

### Tertiary (LOW confidence)
- None relied upon.

## Metadata

**Confidence breakdown:**
- Thread/exit semantics: HIGH — measured three shapes on 3.14.2 in this venv
- SANE cancel semantics (local backends): HIGH — standard text plus a live run against `test:0`
- SANE cancel semantics (`net`): MEDIUM — source-read only; no saned available here
- Assembly memory and the bounded alternative: HIGH — four strategies measured at two page counts, output verified equivalent
- PNG level: HIGH for the measurement, MEDIUM for its generalisation to real scans (A3)
- Blast radius: HIGH — enumerated by grep over the current tree
- Docs inventory: HIGH — line-referenced

**Research date:** 2026-09-15
**Valid until:** 2026-10-15 (stable stack; re-verify if `img2pdf`, `pikepdf` or CPython minor versions move)
