---
phase: 29-geometry-memory-and-timeouts
plan: 04
subsystem: scanner
tags: [testing, weakref, memory, sink, records, pillow, fake-sane, hardware]

# Dependency graph
requires:
  - phase: 29-geometry-memory-and-timeouts
    provides: "plan 02's switched scan_pages/ScanBatch contract and the tests/conftest.py seam (spooling, StubScannerBackend, images_of)"
  - phase: 29-geometry-memory-and-timeouts
    provides: "plan 03's spool convention and the _SPOOL_LABEL_A / _SPOOL_LABEL_B constants"
  - phase: 24-scanner-truthfulness
    provides: "tests/fake_sane.py, the one double for python-sane 2.9.2"
provides:
  - "FakeSaneDev.issued_pages / live_page_images() / high_water_live_pages -- D-08's proof instrument"
  - "tests/test_scanner.py green against the sink/record contract (242 tests)"
  - "TestPeakPageMemory: HARD-01's measured bound, asserted two ways"
  - "the one-image-per-sink-call rule, proven with a PageSink subclass"
  - "tests/test_sane_hardware.py driving real libsane through a SpooledPageSink"
  - "the measured high-water value plan 07 and the verifier compare against: 2"
affects: [29-05, 29-06, 29-07, 29-10, 29-11]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Weakref issue log on a test double: measure who holds a page, never RSS or tracemalloc"
    - "A recording sink that subclasses the ABC and delegates to the real one, never a mock"
    - "A named record for parametrize columns when a new fixture pushes a test past PLR0913"

key-files:
  created: []
  modified:
    - tests/fake_sane.py
    - tests/test_scanner.py
    - tests/test_sane_hardware.py

key-decisions:
  - "The memory-bound tests do NOT use load_feeder(): a loaded feeder keeps a strong reference to every page, so the counter would report the test's own retention. Measured 12 for a 12-page run that way, 2 with the fake's generated pages"
  - "Both memory assertions were confirmed load-bearing by an accumulating-sink mutation, which drives the mark to 3 and 12"
  - "The white/black survival tests assert the record's mean and stddev rather than reopening the page: those are the exact two statistics the deleted content policy keyed on"
  - "Two acceptance greps (WeakSet, MagicMock) contradicted the prose of the same task; the prose was honoured and the wording changed, following 29-02/29-03's precedent"

patterns-established:
  - "One sink per acquisition pass in tests too: page_sink and second_pass_sink, each in its own tmp_path subdirectory"
  - "A test that scans directly through _scan_adf_pages supplies the identity crop, _uncropped"

requirements-completed: []  # HARD-01 is proven here but the suite closes at 29-06

# Metrics
duration: ~75min
completed: 2026-09-15
---

# Phase 29 Plan 04: Scanner Tests on the Sink/Record Contract Summary

**`tests/test_scanner.py` is green on the new contract across all 90 `scan_pages` call sites, the hardware suite drives real libsane through a `SpooledPageSink`, and HARD-01's memory bound is proven by a weakref counter that reads 2 for a 3-page scan and 2 for a 12-page one.**

## The Measured Number Plan 07 and the Verifier Compare Against

| Run | `high_water_live_pages` |
|---|---|
| 3-page ADF scan, fake's generated pages | **2** |
| 12-page ADF scan, fake's generated pages | **2** |
| 12-page scan through an *accumulating* sink (mutation) | 12 |
| 3-page scan through an accumulating sink (mutation) | 3 |
| 12-page scan whose pages came from `load_feeder()` | 12 |

The first two rows are what `test_live_page_images_stays_bounded` asserts. The last three are why both assertions are load-bearing and why `load_feeder()` is not used — see Deviation 3.

## Performance

- **Duration:** ~75 min
- **Completed:** 2026-09-15
- **Tasks:** 3
- **Files modified:** 3 (831 insertions, 248 deletions)

## Accomplishments

- **`FakeSaneDev` records every page it hands out.** `issued_pages: list[weakref.ref[Image.Image]]`, `live_page_images()` and `high_water_live_pages`, all three declared as bare class-body annotations and initialised through `self.__dict__` because `__setattr__` models SANE option validation. No constructor parameter was added — `__init__` is at `PLR0913`'s ceiling, so the knob is a method, following `set_page_delay`.
- **The high-water mark is sampled inside `snap()`, immediately before the page is returned**, so the page being handed over is counted. That is what makes 2 honest rather than 1: `_acquire_pages`' loop variable still references page *k-1* while page *k* is read.
- **`live_page_images()`'s docstring records both measured rejections** — `tracemalloc` sees 460 bytes of a 26 MB Pillow image because the pixels are malloc'd in C, and a hash-based weak set raises `TypeError` because `Image` defines `__eq__` without `__hash__` (confirmed at runtime: *"cannot use 'weakref.ReferenceType' as a set element (unhashable type: 'Image')"*). It also states what the counter does **not** see, so nobody reads 2 as an allocation ceiling.
- **All 90 `scan_pages` call sites take a real `SpooledPageSink`.** A `page_sink` fixture builds one under `tmp_path/spool-a`, a `second_pass_sink` builds the pass-B one under `tmp_path/spool-b`, and both go through `_page_sink_for`, which imports `_SPOOL_LABEL_A`/`_SPOOL_LABEL_B` from `pipeline` rather than re-spelling the literals. `min_free_space_mb` is 0, with the reason stated where the constant is defined.
- **No test reconstructs page order from the filesystem.** `grep -c 'sorted(.*glob\|\.glob('` returns 0 in both files; order comes from `batch.pages` and `record.sequence` only (D-02).
- **48 `.pages` assertions migrated to the cheapest honest form.** Record fields where the claim is about geometry or statistics (`size`, `sequence`, `mean`, `stddev`, `path`), `len()` where it is about count, and `images_of(batch)` only where it is genuinely about the bytes on disk — the two EXIF tests, which now prove the *spooled PNG* carries no EXIF, which is the file `assemble_pdf` embeds.
- **`MockBackend` fills the caller's sink**, so it still models the ABC it exists to test rather than building its own `pages` tuple.
- **`TestPeakPageMemory`** holds four tests: the bound asserted two ways, a direct demonstration that the counter falls as references are released (so a steady reading really does mean retention), the one-image-per-sink-call proof, and the 1..12 sequence check with a pairwise-distinctness read-back.
- **`_RecordingSink` subclasses `PageSink`** and delegates to a real `SpooledPageSink`. It proves `add` ran exactly 12 times, each with one `PIL.Image.Image`, that the device snapped exactly 12 times, and that `batch.pages` is byte-for-byte the tuple of records `add` returned, in order.
- **The hardware suite runs against real libsane.** `uv run pytest -m sane_hardware` — **5 passed**. The ten-page test now also asserts `sequence` 1..10 and that every spooled file exists; the black-page test asserts the records' statistics *and* reads the pages back through `images_of`, because those say different things.

## Task Commits

| Task | Commit | What |
|---|---|---|
| 1 | `b9d1230` | `test(29-04): record issued page images on the fake with weakrefs` |
| 2 | `35438bb` | `test(29-04): migrate the scanner suite to sinks and records` |
| 3 | `be92aca` | `test(29-04): prove the memory bound and migrate the hardware suite` |

Deliberately left alone, per the plan's out-of-scope list: the timeout wording and `set_page_delay(2.0)`/`timeout_per_page=0.1` pair (TEST-02, plan 07 replaces the sleep with an Event gate), `cancel`/wedge behaviour, the SANE init guard and `sane.exit` tests (plan 10), and every over-claiming test name (TEST-04 / Phase 32).

## Files Created/Modified

- `tests/fake_sane.py` — `weakref` imported; two class-body annotations, two `__init__` initialisations, `live_page_images()`, and five lines inside `snap()`. `start()`, `cancel()`, `close()` and `FakeSaneModule` untouched, as plan 07 requires.
- `tests/test_scanner.py` — the migration seam (`_NO_FREE_SPACE_RESERVE`, `_page_sink_for`, `_uncropped`, `page_sink`, `second_pass_sink`), 90 call sites, 48 assertions, `MockBackend`, `_AssignmentFailure`, `_RecordingSink`, `_feeder_of` and `TestPeakPageMemory`.
- `tests/test_sane_hardware.py` — `_page_sink_for`, three `scan_pages` sites and both `.pages` assertions.

## Decisions Made

- **The memory tests use the fake's generated pages, not `load_feeder()`.** See Deviation 3. This is the single most important decision in the plan: the instruction as written would have produced a test that asserts `12 <= 2` and fails, or — worse, had the bound been written loosely — one that passes while measuring nothing about the backend.
- **The white- and black-page survival tests assert `record.mean` / `record.stddev`, not a reopened image.** Their own docstrings name "mean 255.0 / stddev 0.0" as the statistic the deleted policy keyed on, and a stddev of 0 with a mean of 255 says every greyscale pixel is 255 — the same claim as the old `getextrema()`, from the number the pipeline will actually judge, with no second decode.
- **The EXIF tests read back through `images_of`.** Here the file genuinely matters: a strip that only cleaned an in-memory copy would leave the orientation tag in the PNG `img2pdf` parses.
- **`_AssignmentFailure` is a `NamedTuple`.** The sink pushed `test_option_assignment_failure_names_option_and_value` to six parameters, one past `PLR0913`. Bundling the four parametrize columns into a named record is the in-repo answer (`_DuplexMismatch`, `_DeliveryContext`, `_AcquisitionContext`), and it reads better than four positional columns.
- **`_uncropped` is a named module function, not a lambda.** `_acquire_pages` takes the per-page crop as a closure; a test driving acquisition directly has no geometry to bind, so it supplies the identity — annotated, so both type checkers see the `Callable[[Image.Image], Image.Image]` it has to be.
- **Both fixtures spool into their own subdirectory.** `spool-a` and `spool-b`, so a two-acquisition test cannot have one pass's files answer for the other's.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Task 1's `WeakSet` grep vs. the docstring the same task mandates**

- **Found during:** Task 1
- **Issue:** The action requires the docstring to record "constructing a `weakref.WeakSet` of Pillow images raises `TypeError`, which is why it is not used", while the criterion requires `grep -c 'WeakSet' tests/fake_sane.py` to return 0. Writing the sentence breaks the grep.
- **Fix:** Kept the whole explanation, including the exact `TypeError` message, and reworded the identifier to "``weakref``'s hash-based *set* container" (there is exactly one, so nothing is ambiguous), plus "must stay one — do not 'tidy' it into a set". One in-line comment was reworded the same way.
- **Files modified:** `tests/fake_sane.py`
- **Verification:** the grep returns 0; the criterion's intent is proven directly at runtime — `weakref.WeakSet([Image.new("RGB", (2, 2))])` raises *"cannot use 'weakref.ReferenceType' as a set element (unhashable type: 'Image')"*.

**2. [Rule 3 - Blocking] Task 3's `MagicMock` count vs. the docstring the same task mandates**

- **Found during:** Task 3
- **Issue:** Identical shape. The action says to subclass `PageSink` "(not `MagicMock`)", and the criterion requires `grep -c 'MagicMock' tests/test_scanner.py` not to increase above its pre-task value of **0**. Explaining the choice broke the count.
- **Fix:** Reworded to "rather than being one of ``unittest.mock``'s auto-specced doubles". The argument — a mock returns whatever it is given, a subclass is a type error the moment `add` changes — is unchanged.
- **Files modified:** `tests/test_scanner.py`
- **Verification:** the grep returns 0; `_RecordingSink` really does subclass `PageSink`, which both checkers confirm.

**3. [Rule 1 - Bug in the plan's own instruction] `load_feeder()` inverts the measurement it was prescribed for**

- **Found during:** Task 3
- **Issue:** The action says *"Drive the page count from the fake's `load_feeder()` so each page carries distinct content."* But `load_feeder()` stores the images in `FakeSaneDev._page_images` and `snap()` returns the stored object, so the **device keeps a strong reference to every page for the whole scan**. The weakref counter would then measure the test's own retention. Measured: `{3: 3, 12: 12}` with `load_feeder()`, against `{3: 2, 12: 2}` without. `high_water <= 2` would have failed outright, and the N-independence assertion with it.
- **Fix:** The memory tests use `FakeSaneDev(pages=N)` and let the fake generate its pages, which `_page_image(index, …)` already makes distinguishable by index — the property the instruction actually wanted. The distinctness is asserted rather than assumed: `test_a_twelve_page_scan_numbers_its_records_one_to_twelve` reads all twelve spooled pages back through `images_of` and checks they are pairwise distinct. The trap is recorded in `TestPeakPageMemory`'s docstring so it is not reintroduced.
- **Files modified:** `tests/test_scanner.py`
- **Verification:** the four measured rows in the table above.

**4. [Rule 2 - Doc truth] `test_the_batch_carries_exactly_three_fields`' docstring had become false**

- **Found during:** Task 2
- **Issue:** It said *"Phase 29's HARD-01 owns the ordered per-page record design, and a batch that grew page-level detail here would quietly pre-empt it."* HARD-01's design landed in plan 02, so the sentence reserves work that is already done.
- **Fix:** Reworded to state where the per-page detail went — `pages: tuple[PageRecord, ...]` — and why a fourth field on the batch would be a second channel for it. The test's assertion is unchanged.
- **Files modified:** `tests/test_scanner.py`
- **Note for plan 29-11:** the two remaining forward references in this file (`Phase 29's HARD-02` at the integrity-failure class, `HARD-03/HARD-04` at the close-on-return test) are still true and were left alone.

**5. [Rule 2 - Doc truth] Four docstrings described a return value that no longer exists**

- **Found during:** Task 2
- **Issue:** "yields image", "image is yielded at original size", "yields three pages", "yield in full", "MockBackend.scan_pages yields PIL Images" — all describe a method that now returns records and spools images.
- **Fix:** Reworded to say what each test now asserts. Test *names* were not touched (TEST-04 / Phase 32).
- **Files modified:** `tests/test_scanner.py`

**6. [Rule 3 - Blocking] The sink pushed one parametrized test past `PLR0913`**

- **Found during:** Task 2
- **Issue:** `test_option_assignment_failure_names_option_and_value` took four parametrize columns plus `monkeypatch`; adding `page_sink` made six.
- **Fix:** The four columns became one `_AssignmentFailure` `NamedTuple`. No suppression, no rule disabled.
- **Files modified:** `tests/test_scanner.py`

---

**Total deviations:** 6 auto-fixed (3 blocking, 2 doc-truth, 1 bug in the plan's own instruction)
**Impact on plan:** No scope change. Two are the now-familiar collision between a task's mandated prose and its grep-proxy criterion; one is a genuine defect in an instruction, caught by measuring rather than assuming; two are truth repairs caused by this contract switch; one is a lint ceiling the new fixture pushed a test over.

## Issues Encountered

- **A test double that retains what it hands out cannot be used to measure retention.** Deviation 3, stated generally because it will bite plan 07: any assertion about *who holds a page* has to account for the fake holding one too. `load_feeder()` is still the right tool wherever exact page content matters and memory does not — it is used unchanged by twenty-odd tests in this file.
- **Two of the three tasks had a grep criterion that its own action text could not satisfy.** That is now four occurrences across plans 02, 03 and 04. The pattern is always the same: a criterion greps for the *absence of an identifier* as a proxy for "the thing is not used", and the action asks the docstring to explain why it is not used. A criterion of the form `grep -c 'X' file` returning 0 should probably exclude comment and docstring lines, as plan 29-02 task 1's `phase 29` criterion already does with `grep -vi '^\s*#'`.
- **`grep -c 'scan_pages('` reports 92 while there are 90 call sites.** The other two lines are `MockBackend`'s `def scan_pages(` and its continuation. Recorded so a verifier does not read the gap as two un-migrated sites; `grep -c 'scan_pages([^)]*settings)'` returns **0**, which is the criterion that matters.

## TDD Gate Compliance

All three tasks are marked `tdd="true"`. All three commits are `test(...)` commits, because this plan's entire product is tests; there is no `feat(...)` commit and there should not be, since `src/` was completed by plans 02 and 03 and was **not touched here**.

RED was observed where it is observable:

- **Task 1:** the `<verify>` command is executable and failed before the change — `AssertionError: ['__getattr__', '__init__', …]` from the `'live_page_images' in names` assertion. GREEN after.
- **Task 2:** the whole file was red at the start of the task. `uv run pytest tests/test_scanner.py -k "TestFakeSaneContract or TestFakeSaneOptionReload or TestFakeFeederStartOrdering"` reported `TypeError: SaneBackend.scan_pages() missing 1 required positional argument: 'sink'` before, and 238 passed after.
- **Task 3:** the behaviour under test was already implemented, so a literal RED was not available — writing the test and watching it fail would have required breaking `src/`, which is out of scope. Instead **each assertion was proven load-bearing by mutation**, which is the stronger check: an accumulating sink (the exact regression HARD-01 forbids) drives the high-water mark to 3 and 12, failing `<= 2` and failing the N-independence equality. That mutation script was ad-hoc, run in the scratchpad, and is **not committed**.

## Verification

Scoped gates, all green at `be92aca`:

- `uv run pytest tests/test_scanner.py -q` — **242 passed**
- `uv run pytest tests/test_scanner.py -k live_page_images -x -q` — 2 passed
- `uv run pytest tests/test_scanner.py -k sink -x -q` — 1 passed
- `uv run pytest tests/test_sane_hardware.py --collect-only -q` — 5 collected, exit 0
- `uv run pytest -m sane_hardware -q` — **5 passed** against real libsane's `test` backend
- `uv run pytest tests/test_spool.py -q` — 20 passed (unchanged by this plan)
- `uv run ruff check .` and `uv run ruff format --check .` — clean, 55 files
- `uv run pyrefly check src tests/conftest.py tests/fake_sane.py tests/test_scanner.py tests/test_sane_hardware.py` — **0 errors**
- `uv run ty check src tests/conftest.py tests/fake_sane.py tests/test_scanner.py tests/test_sane_hardware.py` — **All checks passed**

Every acceptance criterion across the three tasks passes. Measured counts: `scan_pages([^)]*settings)` → 0, `MagicMock` → 0, `\.glob(` → 0 in both files, `match="timed out"` → 1, `WeakSet` → 0, `weakref.ref(` → 1, `high_water_live_pages` → 4 in each file, `def set_page_delay` → 1, `tracemalloc` in `test_scanner.py` → 1 and it is inside `TestPeakPageMemory`'s docstring, `list(range(1, 13))` → 1.

**Still expected red until plan 29-06**, reported as such and not as a pass:

| Gate | At plan 03 | Now |
|---|---|---|
| `uv run pytest -m "not browser and not sane_hardware"` | 228 failed / 1722 passed | **123 failed / 1831 passed** (of 1954) |
| `uv run pyrefly check src tests` | 210 errors, all in `tests/` | **108 errors, all in `tests/`** |
| `uv run ty check` (full) | 208 diagnostics | still red |

The remaining pyrefly errors are `test_pipeline.py` (49), `test_pages.py` (19), `test_pdf.py` (17), `test_cli.py` (6), `test_worker.py` (4) and ten across the web/lifespan stubs — every one of them owned by plan 05 or plan 06. `src/` contributes none, and neither do `conftest.py`, `fake_sane.py`, `test_scanner.py` or `test_sane_hardware.py`.

No `--no-verify`, no `SKIP=`, no `# type: ignore`, no `# noqa`, no disabled rule, no compatibility shim. Every commit passed the full pre-commit hook set, including both `src` type checkers.

## Known Stubs

None. `MockBackend` and `_RecordingSink` are test doubles by design, not placeholders standing in for unwritten behaviour, and both go through a real sink.

## Threat Flags

None beyond the plan's own `<threat_model>`. T-29-13 (the 12-page test uses the fake's 200x300 synthetic pages and spools into `tmp_path`, which pytest removes; the whole scanner file runs in well under the 60 s `pytest-timeout`), T-29-14 (the issue log is test-only and unreachable from `src/`) and T-29-15 (the bound is asserted both as `<= 2` and as equal for N=3 and N=12, so a regression that grows with page count fails even if the constant is later raised) are all as registered.

## User Setup Required

None. The hardware tests need `libsane-dev`, which was already present in this environment and in both CI jobs.

## Next Phase Readiness

- **Plan 29-05** can migrate `test_pipeline.py`, `test_pages.py`, `test_pdf.py` and `test_outcomes_e2e.py` without touching anything here. `tests/conftest.py` was not modified by this plan.
- **Plan 29-06** inherits 123 failures across six files and 108 pyrefly errors, none of them in `src/`, `conftest.py`, `fake_sane.py`, `test_scanner.py` or `test_sane_hardware.py`.
- **Plan 29-07** must find `start()`, `cancel()`, `close()` and `FakeSaneModule` exactly as it left them — only `snap()` gained five lines, and they append to the issue log without changing what `snap()` returns or when. `set_page_delay` is untouched and `test_page_timeout_raises_scan_error` still matches on `"timed out"`. **The number to compare against is 2**, for any page count. `TestSaneBackendPerPageTimeout` now calls `_scan_adf_pages(dev, sink, crop, timeout_per_page=…)`, with `_uncropped` as the crop.
- **Plan 29-10** owns the init-guard tests in `TestSaneBackendInit` and `TestSaneBoundary`; both were left exactly as found.
- **Plan 29-11** should note deviation 4 (one forward reference corrected here) and the two that are still true.
- **HARD-01 is proven but not closed.** `requirements-completed` is empty and `REQUIREMENTS.md` was not touched: the bound now has a test that fails if it regresses, but the requirement's own gate is a green suite, which is plan 06's.

## Self-Check: PASSED

- Files: `tests/fake_sane.py`, `tests/test_scanner.py`, `tests/test_sane_hardware.py` — all FOUND and modified.
- Commits: `b9d1230`, `35438bb`, `be92aca` — all FOUND in git log, each with `ty type checker (src)` and `pyrefly type checker (src)` Passed.
- No file outside this plan's `files_modified` was touched: `git diff --stat 53f3912..HEAD` lists exactly those three.
- No deletions: the three commits are 831 insertions and 248 deletions, all line-level within those files.

---
*Phase: 29-geometry-memory-and-timeouts*
*Completed: 2026-09-15*
