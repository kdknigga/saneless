---
phase: 29-geometry-memory-and-timeouts
plan: 02
subsystem: scanner
tags: [abc, dataclass, sink, memory, pillow, testing, conftest]

# Dependency graph
requires:
  - phase: 29-geometry-memory-and-timeouts
    provides: "plan 01's PageRecord, PageSink and SpooledPageSink, landed additively"
  - phase: 24-scanner-truthfulness
    provides: "ScanBatch as the one channel for what the backend measured, and the scan_pages-returns-a-record argument this plan extends"
provides:
  - "ScanBatch.pages as tuple[PageRecord, ...] -- the switched contract"
  - "ScannerBackend.scan_pages(device_id, settings, sink) with a required PageSink"
  - "ScannerBackend.close() as a concrete, non-abstract no-op (D-18)"
  - "SaneBackend acquiring, validating, cropping and sinking one page at a time on both the ADF and flatbed paths"
  - "_validate_page_image measuring a page by arithmetic instead of a full raw-buffer copy"
  - "tests/conftest.py migration seam: spooling, spooling_in_turn, StubScannerBackend, images_of"
affects: [29-03, 29-04, 29-05, 29-06, 29-07, 29-09, 29-10]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Sink-shaped acquisition: the backend fills a caller-supplied seam and retains nothing"
    - "functools.partial to carry per-job geometry into a per-page crop without breaking PLR0913"
    - "Test doubles subclass the ABC so a contract change is a type error, not a silent drift"

key-files:
  created: []
  modified:
    - src/saneless/scanner/base.py
    - src/saneless/scanner/sane_backend.py
    - tests/conftest.py

key-decisions:
  - "Executed and committed together with plan 29-03: the contract change is atomic across src/, and the commit-stage hooks type-check src/ with always_run, so plan 02 alone cannot produce a commit without bypassing a gate this project does not bypass"
  - "ScannerBackend.close() carries a logger.debug body rather than a bare docstring, because ruff B027 flags an empty non-abstract method on an ABC and this project forbids noqa"
  - "The conftest factories collect the records sink.add returns instead of reading sink.records, which is declared on the concrete SpooledPageSink and not on the PageSink ABC"
  - "EXIF is stripped before the crop on both paths, because Pillow copies info into a cropped result"

patterns-established:
  - "One sink per acquisition pass, handed down from the pipeline; the backend never learns where a page lands"
  - "Per-page crop before the hand-off, so what is spooled is what the PDF embeds"

requirements-completed: []  # HARD-01 lands across 29-02..29-06; see Next Phase Readiness

# Metrics
duration: ~95min (jointly with 29-03)
completed: 2026-09-16
---

# Phase 29 Plan 02: Switch the Scanner Contract to Page Records and a Sink Summary

**`ScanBatch.pages` is now an ordered tuple of `PageRecord`, `scan_pages` takes a required `PageSink`, `ScannerBackend` has a concrete `close()`, and `SaneBackend` hands each page straight to the sink — with the conftest seam the four test-migration plans pull.**

## Executed as One Commit With Plan 29-03 — Read This First

This plan produced **no commit of its own**, and that is deliberate, not a shortfall.

The contract change is atomic across `src/`. The moment `ScanBatch.pages` becomes records and `scan_pages` grows a `sink`, `src/saneless/pipeline.py` stops type-checking — 12 errors, all in that one file. This repo's `prek` **pre-commit** stage runs `uv run ty check src` and `uv run pyrefly check src` with `always_run: true`, so **every** commit while `src/` is inconsistent is refused, including a docs-only one. Measured, with the complete plan-02 change staged: every hook passed except `ty type checker (src)`.

Plan 29-02's own objective anticipated this — *"If the `src` type-check hook blocks a commit here, that is the signal to carry straight on into plan 03's work in the same working tree — not to suppress the hook."* The executor stopped, reported the blocker, and the coordinator chose exactly that route. Plans 02 and 03 are split only because the change does not fit in one context window; the commit gate makes them one commit.

The combined commit is **`06337cd`** — `refactor(29-02,29-03): switch the scanner contract to ordered page records`, 6 files, 730 insertions, 237 deletions. No `--no-verify`, no `SKIP=`, no `# type: ignore`, no `# noqa`, no disabled rule, no shim, no widened ABC.

## Performance

- **Duration:** ~95 min for plans 02 and 03 together, including the blocker investigation and the coordinator round trip
- **Completed:** 2026-09-16
- **Tasks:** 3
- **Files modified:** 3

## Accomplishments

- **`ScanBatch.pages: tuple[PageRecord, ...]`** — the docstring's reservation of per-page design for "Phase 29's HARD-01" is replaced by the delivered truth, and states that `PageRecord.sequence`, not the filesystem, is the proof of document order (D-02). `frozen=True`'s rationale paragraph survives verbatim.
- **`scan_pages(self, device_id, settings, sink: PageSink)`** — keeps the existing "why not a generator" argument and adds D-01's "why a sink": the backend must never hold more than one decoded page, and where a page lands is a pipeline fact. Both rejected alternatives are named.
- **`ScannerBackend.close()`** — concrete and deliberately not abstract, so no test stub needs an empty override; documents the logged-never-raised rule it inherits from `_open_device`'s `finally`.
- **`SaneBackend` holds no list of images.** `_acquire_pages` takes the sink and a crop closure, and a page flows device → validate → strip EXIF → crop → `sink.add` → record. `_snap_flatbed` runs the identical sequence, so HARD-04's validation half is now structurally the same code path rather than a parallel one.
- **`_maybe_crop` moved to per page**, before the sink, via a `functools.partial` bound once in `scan_pages`. Its own signature and geometry logic are untouched — only the call site moved.
- **`_validate_page_image` stopped copying the page to measure it** — `size[0] * size[1] * len(getbands())` replaces a full raw-buffer materialisation (D-06, M-08's cheap win), with the mode-`"1"` caveat recorded.
- **`tests/conftest.py` seam:** `spooling`, `spooling_in_turn` (one dispatching callable that counts its own calls — RESEARCH.md Pitfall 5), `StubScannerBackend(ScannerBackend)`, `images_of`, and `_inked_page`; `scan_batch` re-signed to records and `mock_scanner` moved from `return_value` to `side_effect`.

## Task Commits

All three tasks are in the single combined commit `06337cd`; see the section above for why. The exception-translation ladder, the `Scanner error on page N: ` prefix, `ThreadPoolExecutor`, `_next_page_with_timeout` and the init guard were all left untouched for plans 07 and 10, as instructed.

## Files Created/Modified

- `src/saneless/scanner/base.py` — `ScanBatch.pages`, `scan_pages`, `close()`, a module logger, and the two docstrings rewritten. `PageRecord`/`PageSink` from plan 01 are unchanged apart from one reworded sentence.
- `src/saneless/scanner/sane_backend.py` — `_acquire_pages`, `_snap_flatbed`, `_scan_adf_pages`, `scan_pages` and `_validate_page_image`; `functools` imported; `PageRecord`/`PageSink`/`Callable` under `TYPE_CHECKING`.
- `tests/conftest.py` — the migration seam (+234 lines net).

## Decisions Made

- **`close()` has a `logger.debug` body, not a bare docstring.** See deviation 1: ruff's `B027` forbids an empty non-abstract method on an ABC, `pass`/`...` do not help, a bare `return` trips `PLR1711`, and both `@abstractmethod` and `# noqa` are excluded by the design and by CLAUDE.md. The log line is honest — it says which backend declined to close — and it is what a shutdown log needs to show nothing was skipped by accident.
- **The conftest factories build the batch from the records `sink.add` returns.** `<interfaces>` specified `ScanBatch(pages=sink.records, …)`, but `records` lives on the concrete `SpooledPageSink`, not on the `PageSink` ABC; typing the parameter as the concrete class would have broken `StubScannerBackend`'s override, and widening the ABC was forbidden. Collecting the return values is equivalent and type-clean.
- **EXIF is stripped before the crop.** Pillow's `Image.crop` copies `info` into the new image, so a strip placed after the crop would have to be repeated on whichever object came back. Stripping first makes it unconditional.
- **The live-page high-water mark is left at 2.** No `del` or rebind was added: the loop variable legitimately still references page *k-1* while page *k* is acquired (RESEARCH.md Finding 4), and a `del` existing only to satisfy a test would be exactly that.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] ruff `B027` forbids the plan's "body is its docstring alone" for `close()`**

- **Found during:** Task 1
- **Issue:** The action requires `close()` to be concrete with "a documented no-op body", and `<behavior>` requires it to stay out of `__abstractmethods__`. An empty non-abstract method on an ABC is `B027`, `pass` and `...` are equally empty, a bare `return` is `PLR1711`, and CLAUDE.md forbids `# noqa`.
- **Fix:** Gave the no-op an honest statement — a module logger and `logger.debug("close() is a no-op for %s", type(self).__name__)` — and recorded the reasoning in the docstring so nobody removes it.
- **Files modified:** `src/saneless/scanner/base.py`
- **Verification:** `ruff check` clean; `'close' not in ScannerBackend.__abstractmethods__` still holds; a stub that never overrides it can be instantiated and called.

**2. [Rule 3 - Blocking] Task 1's "no Phase 29" grep vs. plan 01's own prose**

- **Found during:** Task 1
- **Issue:** `grep -vi '^\s*#' base.py | grep -ci 'phase 29'` must return 0, but `PageSink.add`'s docstring — landed by plan 01 — said "back where Phase 29 found it", making the criterion fail on its own.
- **Fix:** Reworded to "back where M-08 measured it". Same claim, sourced to the finding rather than the phase, which is the more durable form anyway.
- **Files modified:** `src/saneless/scanner/base.py`
- **Verification:** the grep returns 0.

**3. [Rule 3 - Blocking] `sink.records` is a static error in the conftest factories**

- **Found during:** Task 3
- **Issue:** `<interfaces>` specifies `ScanBatch(pages=sink.records, …)`, but `records` is a property of `SpooledPageSink`, not of the `PageSink` ABC the parameter is typed as.
- **Fix:** The factories collect the records `sink.add` returns. Equivalent result, no suppression, and `StubScannerBackend.scan_pages` still overrides the ABC correctly.
- **Files modified:** `tests/conftest.py`
- **Verification:** both type checkers clean; a runtime check confirmed the records and the spooled files match.

**4. [Rule 2 - Doc truth] `_MAX_ADF_PAGES`' memory paragraph became false**

- **Found during:** Task 2
- **Issue:** The comment asserted "pipeline.py materialises pages with `list()`" and that bounding memory is future work — both untrue once the backend sinks each page.
- **Fix:** Reworded to state that the cap and the memory bound are independent, and that raising the cap is not a memory decision. **Flagged for plan 29-11**, which owns the doc-truth sweep, in case it also lists this site.
- **Files modified:** `src/saneless/scanner/sane_backend.py`

**5. [Minor, deliberate] EXIF strip moved before the crop on both paths**

- **Found during:** Task 2
- **Issue:** Not a plan instruction; noticed while moving the crop. Pillow copies `info` into a cropped image, so stripping after a crop would need repeating.
- **Fix:** Strip first, with a comment saying why.
- **Files modified:** `src/saneless/scanner/sane_backend.py`

---

**Total deviations:** 5 auto-fixed (3 blocking, 1 doc-truth, 1 minor)
**Impact on plan:** No scope change. Three are collisions between the plan's own prose and its grep-proxy criteria or this repo's lint rules; two are truth repairs caused directly by this plan's change.

## Issues Encountered

- **The plan is not independently committable, and that is a structural finding, not a local one.** Recorded in full above. The generalisable lesson: a phase's plan decomposition is a *context-window* decomposition, and this repo's commit gate imposes its own *commit* decomposition. Where a contract change spans both, the two must be reconciled at planning time.
- **ruff's bandit rules read "pass" as "password".** `pass_label="b"` is `S106` and `_PASS_B_LABEL` is `S105`. This bit plan 29-03 (see its summary); it is worth knowing before anyone writes `pass_label=` as a keyword argument in `src/`.

## TDD Gate Compliance

**This plan has no `test(...)` RED commit, and both TDD tasks were executed without one.** Stated plainly rather than papered over:

- Task 2's own `<behavior>` block assigns its tests elsewhere — *"Behaviour plan 04's tests assert; state each as a docstring-level contract now"* — so writing them here would have duplicated and pre-empted plan 04.
- Task 1's `<behavior>` is executable as its `<verify>` command, and RED **was** observed before the change: `AssertionError: ['self', 'device_id', 'settings']`. GREEN after.
- Every behavioural claim in both tasks was proven at runtime against the existing `tests/fake_sane.py` double before committing (3-page ADF calls `sink.add` exactly 3×, returns a 3-tuple with sequences `[1,2,3]` and files that exist; flatbed calls it exactly once; `images_of` reads back distinct per-page content in record order; `spooling_in_turn` raises on overrun; an un-overridden `close()` works). Those checks were ad-hoc, in the scratchpad, and are **not** committed — plans 04-06 own the real tests.

The RED→GREEN commit pair the gate looks for therefore does not exist for this plan. Plans 04, 05 and 06 are where this contract acquires its test coverage.

## Verification

Scoped gates, all green at the commit:

- `uv run ruff check .` and `uv run ruff format --check .` — clean (55 files)
- `uv run ty check src/saneless/scanner` — All checks passed
- `uv run pyrefly check src/saneless/scanner src/saneless/spool.py` — 0 errors
- `uv run pytest tests/test_spool.py -x -q` — 20 passed
- `inspect.signature(ScannerBackend.scan_pages).parameters` — `['self', 'device_id', 'settings', 'sink']`

Every acceptance criterion across the three tasks passes, except `grep -c 'pass_label="b"'` which belongs to plan 03 (see its summary, deviation 2).

**Expected red until plan 29-06**, reported honestly rather than as a pass: `uv run pytest` (**228 failed, 1722 passed**), `uv run ty check` (full, **208 diagnostics**), `uv run pyrefly check src tests` (**210 errors**), `uv run prek run --all-files`. Every one of those 210 errors is in `tests/`; `src/` is clean. Plans 04 and 05 migrate the test modules and plan 06 closes the interval.

## Known Stubs

None. Nothing here renders placeholder data. `StubScannerBackend` is a test double by design, not a stub standing in for unwritten behaviour.

## Threat Flags

None beyond the plan's own `<threat_model>`. T-29-05 (`_MAX_ADF_PAGES` untouched, per-page disk check from plan 01), T-29-06 (the byte-count formula, with its mode-`"1"` caveat recorded so the floor is not silently loosened) and T-29-07 (`Scanner error on page N` names only the page number and device) are all as registered.

## User Setup Required

None.

## Next Phase Readiness

- The contract plans 03-06 are written against is in place and proven: `ScanBatch.pages: tuple[PageRecord, ...]`, `scan_pages(device_id, settings, sink)`, `ScannerBackend.close()`.
- **Plan 29-03 is already complete** in the same commit — see `29-03-SUMMARY.md`. Wave 3 has no source work left; its executor should verify rather than re-execute.
- **Plan 29-04** can now use `spooling`, `spooling_in_turn`, `StubScannerBackend` and `images_of` from `tests.conftest`, and `SaneBackend._scan_adf_pages` now takes `(dev, sink, crop, timeout_per_page=…)`.
- **Plan 29-07** must find `ThreadPoolExecutor`, `_next_page_with_timeout` and the cancel behaviour exactly as it left them — they were deliberately untouched.
- **Plan 29-11** should check whether it also lists the `_MAX_ADF_PAGES` comment this plan corrected (deviation 4).
- **HARD-01 is not complete.** `requirements-completed` is empty and `REQUIREMENTS.md` was not touched: the memory bound is real in `src/`, but nothing proves it until plan 04's weakref high-water-mark test and plan 06's green suite.

## Self-Check: PASSED

- Files: `src/saneless/scanner/base.py`, `src/saneless/scanner/sane_backend.py`, `tests/conftest.py` — all FOUND and modified in `06337cd`.
- Commit: `06337cd` — FOUND in git log, with `ty type checker (src)` and `pyrefly type checker (src)` both Passed.

---
*Phase: 29-geometry-memory-and-timeouts*
*Completed: 2026-09-16*
