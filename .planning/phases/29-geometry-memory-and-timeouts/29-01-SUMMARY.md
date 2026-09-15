---
phase: 29-geometry-memory-and-timeouts
plan: 01
subsystem: scanner
tags: [pillow, png, pikepdf, dataclass, abc, spool, memory]

# Dependency graph
requires:
  - phase: 24-scanner-truthfulness
    provides: "ScanBatch as the one channel for what the backend measured, and D-05's rule that blank-page policy belongs to the pipeline"
  - phase: 23-honest-outcomes-and-never-lose-a-scan
    provides: "_check_disk_space and the min_free_space_mb contract the per-page check extends"
provides:
  - "PageRecord: frozen dataclass of per-page facts (1-based sequence, path, size, mode, greyscale mean, stddev)"
  - "PageSink: the ABC seam the backend hands each acquired page to"
  - "SpooledPageSink: the one place a page is materialised -- per-page free-space check, PNG write at Pillow default level 6, greyscale stats measured once, first-page thumbnail at spool time"
  - "generate_thumbnail without a full-size copy"
  - "pikepdf declared as a runtime dependency"
affects: [29-02, 29-03, 29-04, 29-05, 29-08, 29-09]

# Tech tracking
tech-stack:
  added: [pikepdf]
  patterns:
    - "ABC for seams the project implements itself (following pipeline.FlipCoordinator's stated rule)"
    - "Frozen dataclass for a report of what already happened (following ScanBatch)"
    - "OSError translated to ScanError at the module boundary, message in a variable, chained with from"

key-files:
  created:
    - src/saneless/spool.py
    - tests/test_spool.py
  modified:
    - src/saneless/scanner/base.py
    - src/saneless/pages.py
    - pyproject.toml
    - uv.lock

key-decisions:
  - "PageSink is an ABC, not a typing.Protocol -- the sink is a seam this project implements itself, which is the rule pipeline.FlipCoordinator already states"
  - "PNG compression is Pillow's default 6, obtained by omitting the argument; level 1 is recorded in the module docstring as the throughput fallback, not as a config key"
  - "The per-page free-space check uses size x band count, never len(image.tobytes()), which copied the whole page to measure it"
  - "Two runtime-behaviour tests are written through setattr / a type[PageSink] binding because the direct spellings are static errors and this project forbids type-ignore suppressions"

patterns-established:
  - "Page spool: one sink per acquisition pass, pass_label-NNNN.png naming, document order from PageRecord.sequence and never from the filesystem"
  - "Per-page disk guard: page decoded size plus the min_free_space_mb assembly reserve, reported in the same wording as the up-front check"

requirements-completed: []  # HARD-01 is only partially delivered here; see Next Phase Readiness

# Metrics
duration: 12min
completed: 2026-09-15
---

# Phase 29 Plan 01: Page-Record Contract and Page Spool Summary

**A frozen `PageRecord` + `PageSink` ABC in `scanner/base.py` and a concrete `SpooledPageSink` that writes each page to disk once, measures it once, and never lets an `OSError` escape — landed purely additively, with the whole tree still green.**

## Performance

- **Duration:** ~12 min
- **Started:** 2026-09-15T23:40Z
- **Completed:** 2026-09-15T23:52Z
- **Tasks:** 3
- **Files modified:** 6 (2 created, 4 modified)

## Accomplishments

- `PageRecord` — a frozen dataclass of facts, never verdicts: `sequence`, `path`, `size`, `mode`, `mean`, `stddev`, and deliberately no blank-page flag, so the profile's thresholds stay the pipeline's business (D-02, Phase 24 D-05).
- `PageSink` — an `ABC` with one abstract `add(image) -> PageRecord`, documenting why it is not a `typing.Protocol` and naming D-01's two rejected alternatives (a generator, a contract-free callback).
- `SpooledPageSink` — per-page free-space check against the decoded page size plus the `min_free_space_mb` assembly reserve, PNG write at Pillow's default level 6, one greyscale conversion per page, the first page's thumbnail fired at spool time, and every failure surfaced as a `ScanError` naming the page number and the spool path with no partial file left behind (D-05, D-06, D-07).
- `generate_thumbnail` no longer makes a full-size copy: `ImageOps.contain` produces the small result directly, and the EXIF strip survives unchanged.
- `pikepdf` moved from the dev group into `[project] dependencies`, so exactly one declaration survives and plan 08's mandatory runtime import fails at install time rather than at scan time.

## Task Commits

1. **Task 1: Declare PageRecord and the PageSink seam** — `9394829` (test, RED) → `e8e4196` (feat, GREEN)
2. **Task 2: TDD src/saneless/spool.py** — `9de6950` (test, RED) → `4812908` (feat, GREEN)
3. **Task 3: Declare pikepdf as a runtime dependency** — `ddd2f78` (chore)

No REFACTOR commit was needed for either TDD task: both implementations went in at their final shape.

## Files Created/Modified

- `src/saneless/spool.py` (new, 242 lines) — `SpooledPageSink`, the one place a page is materialised. Module docstring carries the M-08 memory argument, the measured PNG level-6 rationale, and the "the spool knows nothing about SANE" ownership sentence.
- `tests/test_spool.py` (new, 268 lines, 19 tests) — the `PageRecord`/`PageSink` contract plus D-02/D-05/D-06/D-07 behaviour.
- `src/saneless/scanner/base.py` — adds `PageRecord` and `PageSink`, both exported; `ScanBatch`, `ScannerBackend` and every existing signature untouched.
- `src/saneless/pages.py` — `generate_thumbnail` internals only; signature, base64-JPEG return and EXIF strip unchanged.
- `pyproject.toml`, `uv.lock` — `pikepdf>=10.5.1` moved from `[dependency-groups] dev` into `[project] dependencies`. No other package moved in the lock.

## Decisions Made

- **`PageSink` is an `ABC`.** 29-CONTEXT.md had already resolved this via pattern mapping, and `pipeline.FlipCoordinator`'s docstring states the rule in prose. Following the stated rule meant no doc-truth site had to change.
- **The spool splits `add()` into `_check_room_for` and `_write` helpers.** `add()` stays readable and each failure mode carries its own `Raises:` section; both helpers sit under ruff's `PLR0913` ceiling.
- **Two tests express runtime refusals indirectly.** `record.sequence = 2` and `PageSink()` are both flagged statically (ty and pyrefly respectively), and this project forbids `# type: ignore` / `# noqa`. The tests therefore go through `setattr(record, attribute, 2)` and a `type[PageSink]` binding, each with a comment saying why. The assertions themselves are unchanged: `FrozenInstanceError` and `TypeError`.
- **The write-failure test blocks the spool with a regular file** rather than a chmod, so it proves the `OSError` translation without depending on not running as root.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Two acceptance greps contradicted the task's own `<action>` text**

- **Found during:** Task 2 (and Task 1)
- **Issue:** The plan's acceptance criteria require `grep -v '^\s*#' src/saneless/spool.py | grep -c 'tobytes()'` and `… 'compress_level'` to return 0, `grep -v '^\s*#' src/saneless/pages.py | grep -c 'image.copy()'` to return 0 and `grep -c 'ImageOps.contain' src/saneless/pages.py` to return exactly 1. Those greps only strip `#` comment lines, not docstrings — so the rationale prose the `<action>` block asks for (explaining *why* `tobytes()`, `compress_level` and `image.copy()` are absent) breaks every one of them.
- **Fix:** Kept all the rationale, but moved the three token-bearing sentences from docstrings into adjacent indented `#` comments, and reworded the `pages.py` docstring to say "Pillow's `contain` operation" rather than repeating the call spelling. Nothing was deleted; every explanation the plan asked for is still in the file, one line lower.
- **Files modified:** `src/saneless/spool.py`, `src/saneless/pages.py`
- **Verification:** All four greps now return their specified counts.
- **Committed in:** `4812908`

**2. [Rule 3 - Blocking] Task 1's `is_blank` grep criterion is unsatisfiable as written**

- **Found during:** Task 1
- **Issue:** The `<action>` block requires the `PageRecord` docstring to "say explicitly that there is no `is_blank` field because `_drop_empty_pages` applies the profile's thresholds". The acceptance criterion requires `grep -v '^#' src/saneless/scanner/base.py | grep -c 'is_blank\|is_empty'` to return 0. The mandated sentence is what makes that grep return 1.
- **Fix:** Followed the `<action>` block, which is the normative instruction, and left the explanatory sentence in the docstring. The criterion's actual intent — that no verdict field exists — is proven directly instead: `fields(PageRecord)` is exactly `['sequence', 'path', 'size', 'mode', 'mean', 'stddev']`, and `tests/test_spool.py::TestPageRecordContract::test_carries_no_verdict_field` asserts neither name is a field.
- **Files modified:** `src/saneless/scanner/base.py`
- **Verification:** `uv run python -c "from dataclasses import fields; …"` prints the six expected names; the dedicated test passes.
- **Committed in:** `e8e4196`

**3. [Rule 2 - Missing Critical] Task 1's TDD test file was not assigned to a task**

- **Found during:** Task 1
- **Issue:** Task 1 is marked `tdd="true"` with a `<behavior>` block, but its `<files>` list names only `src/saneless/scanner/base.py` — there was nowhere for the RED test to live.
- **Fix:** Created `tests/test_spool.py` in task 1 with the `PageRecord`/`PageSink` contract tests, and extended the same file in task 2 with the `SpooledPageSink` tests. Task 2 already owned that file, so no new file entered the plan's footprint.
- **Files modified:** `tests/test_spool.py`
- **Verification:** RED confirmed (`ImportError: cannot import name 'PageRecord'`) before the implementation commit.
- **Committed in:** `9394829`

---

**Total deviations:** 3 auto-fixed (2 blocking, 1 missing critical)
**Impact on plan:** No scope change. Two are plan-internal contradictions between `<action>` prose and grep-proxy criteria; one supplies the test file a TDD task needed. Nothing was added to or removed from the plan's intended behaviour.

## Issues Encountered

- **`ImageOps.contain` upscales where `Image.thumbnail` did not.** A page smaller than `max_edge` now yields a 300 px thumbnail instead of staying at its original size. No test asserts thumbnail dimensions for a small image, and a real scan is always far larger than 300 px, so the change is invisible in practice. It is noted here because it is a genuine behaviour difference, not a pure optimisation.
- **`ImageOps.contain` was checked for aliasing.** Pillow 12.1.1 always returns a new image (verified empirically for the equal-size case too), so the `thumb.info.pop("exif", None)` that follows can never mutate the caller's page. Had it returned its argument, the EXIF strip would have reached back into the spooled image.

## TDD Gate Compliance

Both TDD tasks show the full RED → GREEN sequence in git log, with the RED commit's failure observed before implementation:

- Task 1: `9394829` (test) → `e8e4196` (feat)
- Task 2: `9de6950` (test) → `4812908` (feat)

No REFACTOR commit for either; neither implementation needed cleanup after going green.

## Verification

All of the plan's `<verification>` commands were run at the end of the plan:

- `uv run pytest tests/test_spool.py tests/test_pages.py -x -q` — 35 passed
- `uv run pytest -m "not browser and not sane_hardware" -q` — **1950 passed**, fully green
- `uv run ruff check .` and `uv run ruff format --check .` — clean
- `uv run ty check` and `uv run pyrefly check src tests` — 0 errors
- `uv run prek run --all-files` and `uv run prek run --stage pre-push --all-files` — all hooks passed

No `# type: ignore`, `# noqa`, `SKIP=` or `--no-verify` was used anywhere.

## Known Stubs

None. Nothing in this plan renders placeholder data; `SpooledPageSink` is fully wired internally and its behaviour is proven by tests. It simply has no caller yet — by design, because plan 02 owns the breaking signature switch.

## Threat Flags

None. The files this plan touched introduce no security surface beyond the plan's own `<threat_model>`: spool names are built from an integer counter and a fixed pass label (T-29-01), the per-page disk guard is in place (T-29-02), and the new `ScanError` messages name only the page number, the spool path and free/required MB (T-29-03).

## User Setup Required

None - no external service configuration required. `pikepdf` was already installed as an unconditional transitive dependency of `img2pdf`, so declaring it changes nothing at install time today.

## Next Phase Readiness

- The contract plan 02 switches `scan_pages` onto is in place and exported: `PageRecord`, `PageSink`, `SpooledPageSink(directory, pass_label, min_free_space_mb, thumbnail_callback=None)` and `SpooledPageSink.records`.
- **This plan is additive and nothing calls the spool yet.** That is deliberate: it is the last point in the phase where the whole tree is green without qualification. The deliberate RED interval starts in plan 02.
- **HARD-01 is not complete.** `requirements-completed` is left empty and `REQUIREMENTS.md` was not touched: plans 02-05 still have to move `ScanBatch`, `scan_pages`, `_interleave_duplex`, `_drop_empty_pages` and `assemble_pdf` onto records before the memory bound is real end to end.
- Plan 08 can now import `pikepdf` directly in `src/saneless/pdf.py` without adding a dependency.

## Self-Check: PASSED

- Files: `src/saneless/spool.py`, `src/saneless/scanner/base.py`, `src/saneless/pages.py`, `tests/test_spool.py`, `pyproject.toml`, `uv.lock` — all FOUND.
- Commits: `9394829`, `e8e4196`, `9de6950`, `4812908`, `ddd2f78` — all FOUND in git log.

---
*Phase: 29-geometry-memory-and-timeouts*
*Completed: 2026-09-15*
