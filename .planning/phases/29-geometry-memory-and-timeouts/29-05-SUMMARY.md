---
phase: 29-geometry-memory-and-timeouts
plan: 05
subsystem: testing
tags: [pytest, pikepdf, img2pdf, spool, records, ordering, blank-page, duplex]

# Dependency graph
requires:
  - phase: 29-geometry-memory-and-timeouts
    provides: "plan 01's SpooledPageSink, plan 02's sink contract and the tests/conftest.py seam, plan 03's records through pages/pdf/pipeline"
  - phase: 23-honest-outcomes-and-never-lose-a-scan
    provides: "the preservation guard and the failed/ directory these suites already assert on, left untouched for plan 09"
provides:
  - "tests/test_pages.py, tests/test_pdf.py, tests/test_pipeline.py and tests/test_outcomes_e2e.py green on the record contract"
  - "HARD-01's ordering half: a twelve-page job read back out of the assembled PDF, page by page, by byte identity"
  - "a duplex interleave proof built to fail a sorted glob, with both mutations run and caught"
  - "the D-03 no-second-encode proof over a real spool and a separate output directory"
  - "empty-page tests that judge stored statistics, with the measurement end kept under test through a real sink"
affects: [29-06, 29-08, 29-09]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Read a spooled page back through the PDF's raw /FlateDecode stream vs. the PNG's IDAT payload, not through decoded pixels"
    - "Stand in for assemble_pdf with a callable that calls the real one somewhere durable, because the workspace is a TemporaryDirectory"
    - "Assert the negative (the filesystem order differs) before the positive (the document order), so a glob-based implementation fails"

key-files:
  created: []
  modified:
    - tests/test_pages.py
    - tests/test_pdf.py
    - tests/test_pipeline.py
    - tests/test_outcomes_e2e.py

key-decisions:
  - "The twelve-page equivalence is byte identity between each PDF page's raw stream and one specific spooled PNG's IDAT payload -- not a page count, not a pixel comparison"
  - "The interleave test asserts the filesystem/document order difference first; without that assertion it would pass a sorted glob"
  - "The two Pillow-save error tests move to the read side, because assemble_pdf performs no save any more; the CMYK save failure now belongs to the spool"
  - "One local dispatching callable covers the two passes that disagree on resolution, which spooling_in_turn's single resolution cannot express"

patterns-established:
  - "A test that needs a page's contents during a pipeline run takes its copy inside a stand-in, never after run_pipeline returns"
  - "Spool labels come from pipeline._SPOOL_LABEL_A/_B, never a re-spelled literal"

requirements-completed: []  # HARD-01's ordering half is proven here; its memory half is 29-04's and the green suite is 29-06's

# Metrics
duration: ~60min
completed: 2026-09-15
---

# Phase 29 Plan 05: Records Through the Pipeline, PDF, Pages and Outcome Suites Summary

**All four suites now drive the sink/record contract through `tests/conftest.py`, and HARD-01's ordering half is proven twice over: a twelve-page job read back out of its own assembled PDF by byte identity, and a duplex interleave built so that a `sorted()` or a `glob()` fails it — both verified by mutation.**

## How the Twelve-Page Content Equivalence Is Asserted

Recorded exactly, because the plan's `<output>` asks for it and success criterion 1 is graded on it.

`test_twelve_page_order_survives_into_the_assembled_pdf` runs a real twelve-page simplex job through `run_pipeline`. The only thing stood in for is `assemble_pdf`, and the stand-in **calls the real `assemble_pdf`**, changing nothing but the destination directory — the pipeline assembles inside a `TemporaryDirectory` that is gone by the time `run_pipeline` returns, so the PDF and the pages it embedded have to be taken while the job is still running. What is read back afterwards is therefore the production artefact.

Four assertions, in this order:

| # | Assertion | What it rules out |
|---|---|---|
| 1 | `[record.sequence for record in records] == list(range(1, 13))` | the records are not 1..12, i.e. the order being matched is not the order the device fed |
| 2 | `len(set(page_bytes)) == 12` | the twelve spooled pages are not distinct, which would make assertion 4 satisfiable by twelve copies of one page |
| 3 | `len(streams) == 12` | the PDF does not have twelve pages |
| 4 | `streams == [_png_idat(page) for page in page_bytes]` | **the equivalence itself** |

Assertion 4 is the one that matters. `streams[i]` is what `pikepdf` reads out of PDF page *i* with `read_raw_bytes()` — raw, not decoded. `_png_idat(page_bytes[i])` is the concatenated IDAT payload of the PNG that record *i* names, parsed straight out of the file the spool wrote. img2pdf passes a suitable PNG's zlib stream into a `/FlateDecode` image object untouched, so those two byte strings are equal **iff PDF page *i* is that spooled file**. This is a stronger claim than a pixel comparison: it says the PDF's page *is* the page, not that it looks like it. Measured in this run: twelve pages, twelve distinct streams, every one equal, sizes 628-650 bytes.

The test's docstring states this equivalence in full, so it cannot silently weaken into "there are twelve pages" — which is exactly what the plan asked for.

## The Interleave Proof, and Both Mutation Checks

`test_interleave_records_never_recovers_order_from_the_filesystem` spools a 3+3 manual-duplex job into **one** directory through two sinks, `a-` and `b-`, exactly as `_scan_manual_duplex` does. The filename order is therefore `a-0001, a-0002, a-0003, b-0001, b-0002, b-0003`, while the document order is `a-0001, b-0003, a-0002, b-0002, a-0003, b-0001`. The test asserts they **differ** before it asserts what the document order is, then asserts the record identities, then that nothing on disk was renamed or moved and every record's `path` is unchanged.

The plan's acceptance criterion asks for a statement that this was checked against a glob-based implementation. It was, and so was the other direction:

| Mutation | Applied to | Result |
|---|---|---|
| `_interleave_duplex` rebuilds its list from `sorted(spool.glob("*.png"))` | `src/saneless/pipeline.py` | **Caught** — fails on the first assertion, `assert on_disk != document_order` |
| `assemble_pdf` rebuilds `image_paths` from `sorted(spool.glob("*.png"))` | `src/saneless/pdf.py` | **Not caught by the twelve-page test** — see deviation 6; this is the mutation the duplex test exists for |
| `assemble_pdf` emits `reversed(records)` | `src/saneless/pdf.py` | **Caught** — the twelve-page byte-identity assertion fails at index 0 |

Both source files were restored with `git checkout -- <file>` immediately after each check; `git status` was clean of `src/` before every commit, and the three commits touch `tests/` only.

## Performance

- **Duration:** ~60 min
- **Completed:** 2026-09-15
- **Tasks:** 3
- **Files modified:** 4

## Task Commits

| Task | Commit | What |
|---|---|---|
| 1 | `fd871d6` | `test(29-05): migrate the pages and PDF suites to records` |
| 2 | `e53b704` | `test(29-05): drive the pipeline suite through the conftest sink seam` |
| 3 | `bdd23b6` | `test(29-05): prove page order comes from the records, never the filesystem` |

## Accomplishments

- **`tests/test_pages.py` judges numbers.** Every `is_empty_page` case passes the measured mean and stddev directly; the images that existed only to produce those two numbers are gone. The dual-threshold boundaries are now asserted by exact value for the first time — `is_empty_page(250.0, 0.0)` is `False` and `is_empty_page(255.0, 5.0)` is `False`, so both comparisons are pinned as strict — and a case for each half of the AND was added.
- **The measurement end is still under test.** `TestStatisticsComeFromASpooledPage` spools real pages through a real `SpooledPageSink` and asserts a white page really does record mean 255.0 / stddev 0.0 and an inked one really does record `mean < 250` / `stddev > 5`. Without it the literals elsewhere in the file would be unfalsifiable.
- **`filter_empty_pages` is asserted over records**, by identity and in order, including that the surviving `sequence` values keep the gaps the discards left (`[1, 3, 5]`) and that **nothing is unlinked** from the spool.
- **`tests/test_pdf.py` assembles from a real spool.** A `spool_pages` factory fixture writes every page through a real sink into `tmp_path/spool`, and the PDF is written to `tmp_path/out` — deliberately a different directory, which is what makes "no PNG exists under the output directory" mean something.
- **D-03 is proven twice.** `test_no_second_encode_of_any_page` asserts no PNG of any name survives anywhere under `output_dir`, that the output directory holds nothing but the PDF, and that the spool is **byte-for-byte unchanged** afterwards. `test_no_second_encode_survives_a_failed_assembly` asserts the same after an img2pdf failure.
- **`test_page_order_is_the_record_order`** (in the PDF suite) assembles the same records forwards and backwards and compares the embedded streams: the spooled names are identical between the two assemblies, so only the record order can tell the PDFs apart.
- **All 36 `MagicMock(spec=ScannerBackend)` sites in `tests/test_pipeline.py` fill the pipeline's own sink.** 20 `return_value` assignments became `spooling([...])`; 12 two-pass sites became `spooling_in_turn(fronts, backs)`. `grep -c 'scan_pages.return_value'` returns 0 and `grep -c 'side_effect = \[spooling'` returns 0 — no list of callables anywhere.
- **`_interleave_duplex`'s own unit tests run over records spooled into a real `a-`/`b-` directory**, using `pipeline._SPOOL_LABEL_A/_B` rather than re-spelled literals.
- **`tests/test_outcomes_e2e.py`** stubs the scanner with `spooling_in_turn`, so the real `SpooledPageSink` writes its pages and the real records come back — every PDF that module assembles, preserves and uploads now embeds real files.

## Files Created/Modified

- `tests/test_pages.py` — rewritten: numbers for the rule, a real sink for the measurement, records for the filter. Thumbnail tests unchanged in intent (`generate_thumbnail` still takes an image).
- `tests/test_pdf.py` — the `spool_dir` / `output_dir` / `spool_pages` / `one_page` fixtures, the two `no_second_encode` tests, the record-order read-back, and the two read-side error tests. `TestSanitiseTitleForFilename` and `TestBuildPdfFilename` are untouched.
- `tests/test_pipeline.py` — the conftest seam at every site, six new shared helpers, and `TestPageOrderComesFromTheRecordsNeverTheFilesystem`.
- `tests/test_outcomes_e2e.py` — `_build_scanner` and two docstring paragraphs.

## Decisions Made

- **Byte identity, not pixels, for the twelve-page read-back.** A decoded-pixel comparison would still pass if assembly re-encoded each page, which is precisely what D-03 removed; byte identity fails that too. It also happens to be free.
- **The stand-in calls the real `assemble_pdf`.** A fake that merely recorded its arguments would prove the pipeline's ordering and nothing about the PDF. Redirecting the output directory is the smallest change that lets the artefact outlive the workspace.
- **The interleave proof is a unit test over a real spool, not a whole duplex run.** The filename-vs-document-order property is entirely `_interleave_duplex`'s, and a full run would have to patch assembly anyway to see the record list. `TestManualDuplex` already covers the run.
- **`output_dir` in the PDF suite is `tmp_path/out`, not `tmp_path`.** The spool has to live somewhere, and with one shared directory every "no PNG here" assertion would be about the spooled pages. `test_output_path` asserts `pdf_path.parent == output_dir` accordingly.
- **Nothing from plans 07, 08 or 09 was pulled forward.** No `-k merge` memory test, no daemon-thread or cancel test, no partial-scan preservation, no `failed/<job>/` directory. `test_convert_returning_none_becomes_pdf_error` is kept exactly as found, because plan 08 owns deleting that guard.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] The task's own test name contradicts its own `-k` selector**

- **Found during:** Task 3
- **Issue:** `<behavior>` names the test `test_twelve_page_scan_keeps_document_order` and then says "(name must contain `twelve_page_order`)" — which that name does not. The `<verify>` command and the acceptance criterion both select with `-k twelve_page_order`, so the literal name would have made the task's own gate report zero tests.
- **Fix:** Named it `test_twelve_page_order_survives_into_the_assembled_pdf`. Contains the required substring, and says what it asserts.
- **Files modified:** `tests/test_pipeline.py`
- **Verification:** `uv run pytest tests/test_pipeline.py -k twelve_page_order -x -q` — 1 passed.

**2. [Rule 1 - Behaviour truth] Two `PdfError` tests asserted a Pillow *save* that `assemble_pdf` no longer performs**

- **Found during:** Task 1
- **Issue:** `test_pillow_cmyk_png_save_becomes_pdf_error` matched on `"cannot write mode CMYK as PNG"` and `test_pillow_zero_size_save_becomes_pdf_error` on a `SystemError` from saving a 0x0 page. Both came from the per-page re-encode plan 03 deleted (D-03). A CMYK page can no longer even reach `assemble_pdf`: the spool refuses it first, which is `tests/test_spool.py`'s territory (plan 01), not this file's.
- **Fix:** Replaced with the equivalent real failures on the **read** side, neither monkeypatched: `test_an_unreadable_spooled_page_becomes_pdf_error` corrupts a spooled PNG and asserts a real `img2pdf.ImageOpenError` chained under a `PdfError` matching `"cannot read input image"`, and `test_an_unwritable_output_directory_becomes_pdf_error` points the output at a path under a regular file and asserts a real `OSError` cause with the target path named in the message. The second also gives the plan's own "unwritable output" case a test for the first time — it had none.
- **Files modified:** `tests/test_pdf.py`
- **Verification:** both pass; the boundary still translates every img2pdf class, both untyped raises and `KeyboardInterrupt` unchanged.

**3. [Rule 3 - Blocking] `spooling_in_turn` cannot express two passes at different resolutions**

- **Found during:** Task 2
- **Issue:** `spooling_in_turn(*page_lists, resolution=300)` carries one resolution for the whole job. `test_two_passes_disagreeing_on_resolution_say_so` exists precisely because the device reported 300 on pass A and 150 on pass B, and its assertions read a WARNING naming both numbers.
- **Fix:** Added `_spooling_at_each_resolution(*passes)` to `tests/test_pipeline.py` — one callable that counts its own calls and delegates to `spooling(pages, resolution=...)` per pass, following `_PassBGatedScanner`'s in-repo shape. This is what the plan's `<action>` instructs for exactly this case. `tests/conftest.py` was **not** touched.
- **Files modified:** `tests/test_pipeline.py`
- **Verification:** the test passes and still asserts a warning containing both `300` and `150`, with `dpi == 300` reaching assembly.

**4. [Rule 1 - Doc truth] `test_only_the_pdf_is_preserved_no_page_images` justified itself with a claim that is no longer true**

- **Found during:** Task 2
- **Issue:** Its docstring read "D-07: assemble_pdf already deleted every page PNG before returning". Since plan 03, assembly never writes a page PNG at all, so there is nothing for it to delete; what actually makes the assertion hold is that the spool lives inside the workspace and dies with it.
- **Fix:** Docstring corrected to say that, and to name plan 29-09 as the plan that will add a page directory beside the PDF. The assertion itself is unchanged.
- **Files modified:** `tests/test_pipeline.py`

**5. [Rule 1 - Doc truth] `TestExifStripped` asserted `.info` on objects that no longer have one**

- **Found during:** Task 2
- **Issue:** It read `mock_assemble.call_args[0][0]` and looped `assert "exif" not in img.info`. Those are `PageRecord`s now. Worse, the pipeline's per-page EXIF loop was deleted in plan 03, so the naive migration — read the files after `run_pipeline` returns — would have found nothing at all: the workspace is gone by then.
- **Fix:** The test now stands `assemble_pdf` in with `_reading_the_pages`, which opens each spooled page **during** assembly, and asserts no `exif` key survived onto the PNG the PDF embeds. That is the property the deleted loop protected, asserted where it is now decided, and the docstring says so.
- **Files modified:** `tests/test_pipeline.py`

### Observations, no change

**6. [Observation] The twelve-page test does not catch a sorted-glob mutation, and is not meant to**

- **Found during:** Task 3's mutation checks
- **Issue:** With `assemble_pdf` rebuilding `image_paths` from `sorted(spool.glob("*.png"))`, the twelve-page test still passes — for a simplex job the spool is `a-0001.png` … `a-0012.png`, whose sorted order *is* the record order. Zero padding makes lexicographic and numeric order agree.
- **Fix:** None. This is exactly why the plan pairs the two tests, and the duplex interleave test does catch that mutation on the first assertion. Recorded so a verifier does not read the pair as redundant, or the twelve-page test as weaker than it is: reversing the record list does fail it.

**7. [Observation] `tests/test_pdf.py`'s two `temp_files_cleaned` tests could not survive as written**

- **Found during:** Task 1
- **Issue:** Both asserted `list(tmp_path.rglob("*.png")) == []`. `tmp_path` is now where the spool lives, so that assertion could only be made true by having no spooled pages — i.e. by deleting the thing under test.
- **Fix:** Rescoped to the output directory and renamed to the `no_second_encode` pair, which is what they were always proxying for. Their intent (no scratch PNG survives, on success or on failure) is preserved and strengthened with the byte-unchanged spool assertion.

---

**Total deviations:** 7 (5 auto-fixed, 2 observations)
**Impact on plan:** No scope change. One is a collision between the plan's own prose and its own `-k` selector; one is a limitation of the conftest seam the plan itself anticipated; three are truth repairs caused directly by the contract switch; two are recorded observations about what the tests do and do not catch.

## Issues Encountered

- **Anything that reads a page's *contents* during a pipeline run has to do so inside a stand-in.** The spool lives in the per-job `TemporaryDirectory`, so `mock_assemble.call_args` after `run_pipeline` returns gives records whose files are gone. Two helpers exist for this — `_reading_the_pages` (copies the pages) and `_AssemblingSomewhereDurable` (calls the real assembly into a durable directory) — and plan 29-09, which adds preservation tests to this same file, will want them.
- **`spooling_in_turn` is per-job, not per-pass, for resolution.** Deviation 3. Worth knowing before any later plan needs two passes that disagree on anything `spooling_in_turn` holds as a single keyword.

## TDD Gate Compliance

All three tasks are marked `tdd="true"`, and the RED→GREEN shape is real but is not two commits per task, for two different reasons.

**Tasks 1 and 2 are migrations.** RED was measured at the base commit, before any edit, and is on the record: `uv run pytest tests/test_pages.py tests/test_pdf.py tests/test_pipeline.py tests/test_outcomes_e2e.py -q` gave **87 failed, 118 passed**. A separate `test(...)` commit of the already-failing tests would have committed nothing but the failure that was already there.

**Task 3's tests are new, and could not be watched fail against a missing implementation**, because `src/` was completed in plan 03 — they passed the moment they were written. Writing them against a deliberately absent feature was not available. What *was* available, and is the honest equivalent, is mutation: each test was run against a source tree mutated to hold the defect it exists to catch, and each caught it. The table under "The Interleave Proof" records all three mutations, including the one that is deliberately not caught and why. `src/` was restored after each and every commit in this plan touches `tests/` only.

## Verification

Plan gates, all green at `bdd23b6`:

- `uv run pytest tests/test_pages.py tests/test_pdf.py tests/test_pipeline.py tests/test_outcomes_e2e.py -q` — **216 passed**
- `uv run pytest tests/test_pipeline.py -k "twelve_page_order or interleave_records" -x -q` — **2 passed**
- `uv run pytest tests/test_pdf.py -k no_second_encode -x -q` — **2 passed**
- `uv run ruff check .` — All checks passed; `uv run ruff format --check .` — 55 files already formatted
- `uv run pyrefly check src tests/conftest.py tests/test_pages.py tests/test_pdf.py tests/test_pipeline.py tests/test_outcomes_e2e.py` — **0 errors**
- `uv run ty check src tests/test_pages.py tests/test_pdf.py tests/test_pipeline.py tests/test_outcomes_e2e.py` — All checks passed

Acceptance greps:

| Criterion | Required | Actual |
|---|---|---|
| `grep -c 'is_empty_page(Image\|is_empty_page(img\|is_empty_page(page)' tests/test_pages.py` | 0 | 0 |
| `grep -c 'assemble_pdf(\[Image\|assemble_pdf(images' tests/test_pdf.py` | 0 | 0 |
| `grep -c 'scan_pages.return_value' tests/test_pipeline.py` | 0 | 0 |
| `grep -c 'side_effect = \[spooling\|side_effect=\[spooling' tests/test_pipeline.py` | 0 | 0 |
| `grep -c 'spooling' tests/test_pipeline.py` | ≥ 30 | 39 |
| `grep -c 'pikepdf' tests/test_pipeline.py` | ≥ 1 | 5 |
| `grep -c 'list(range(1, 13))' tests/test_pipeline.py` | ≥ 1 | 1 |
| `grep -c 'sorted(' tests/test_pipeline.py` | ≥ 1 | 6 |

**Still red until plan 29-06, reported as such and not as a pass.** The whole suite is **141 failed, 1820 passed** (from 228/1722 at the base commit — the 87 this plan owned) and `uv run pyrefly check src tests` is **124 errors** (from 210). Every remaining failure and every remaining error is in a file another plan owns:

| File | Failures | pyrefly errors | Owner |
|---|---|---|---|
| `tests/test_scanner.py` | 105 | 99 | 29-04 |
| `tests/test_cli.py` | 24 | 6 | 29-06 |
| `tests/test_worker.py` | 12 | 4 | 29-06 |
| `tests/test_sane_hardware.py` | — (deselected) | 3 | 29-04 |
| web / browser / lifespan stubs | — | 12 | 29-06 |

This plan's four files contribute **zero** failures and **zero** type errors.

## Known Stubs

None. The two `assemble_pdf` stand-ins are test doubles by design: `_reading_the_pages` deliberately does not assemble anything (its test patches assembly out entirely, as the test it replaced did), and `_AssemblingSomewhereDurable` calls the real `assemble_pdf`.

## Threat Flags

None beyond the plan's `<threat_model>`.

- **T-29-16** (a weakened ordering test) is **mitigated as registered, and the mitigation was measured**: the interleave test asserts the order difference before the order itself, and a glob-based `_interleave_duplex` fails it on that first assertion.
- **T-29-17** (12-page assembly in a unit test) accepted: the pages are 120x160 synthetic, under `tmp_path`, and the whole suite of four files runs in 3.6 s.
- **T-29-18** (pikepdf parsing a PDF in tests) accepted: every PDF parsed here was produced seconds earlier by the same test from its own spooled pages; no untrusted document is opened.

## User Setup Required

None.

## Next Phase Readiness

- **Plan 29-06** inherits four green files. Its remaining work is `tests/test_cli.py`, `tests/test_worker.py` and the seven stub-scanner files; `tests/conftest.py`'s `StubScannerBackend` is what those need, and nothing here touched it.
- **Plan 29-08** inherits `tests/test_pdf.py` with the `spool_dir` / `output_dir` / `spool_pages` fixtures already in place, which is exactly the shape its `-k merge` memory test needs, and with `test_convert_returning_none_becomes_pdf_error` intact — that guard is 29-08's to delete, and its test with it. `_embedded_streams` in the same file is the idiom for "the merged PDF matches a single-convert PDF page for page".
- **Plan 29-09** adds its preservation tests to `tests/test_pipeline.py` and `tests/test_outcomes_e2e.py`. It will want `_AssemblingSomewhereDurable` and `_reading_the_pages` (the workspace-lifetime trap is the same one), `_duplex_spool` for a two-pass spool, and `_distinct_page` for pages a partial PDF can be told apart by. `test_only_the_pdf_is_preserved_no_page_images` is the test whose contract 29-09 changes.
- **HARD-01 is not complete.** `requirements-completed` is empty and `REQUIREMENTS.md` was not touched. The ordering half is proven here; the memory half is plan 04's weakref high-water-mark test, and the green suite is plan 06's.

## Self-Check: PASSED

- Files: `tests/test_pages.py`, `tests/test_pdf.py`, `tests/test_pipeline.py`, `tests/test_outcomes_e2e.py` — all FOUND and modified across the three commits.
- Commits: `fd871d6`, `e53b704`, `bdd23b6` — all FOUND on `worktree-agent-a31b4249f371b1f6c`, each with `ty type checker (src)` and `pyrefly type checker (src)` Passed and no hook bypassed.
- `src/` is unmodified: `git status --short` shows no `src/` entry after the mutation checks.

---
*Phase: 29-geometry-memory-and-timeouts*
*Completed: 2026-09-15*
