---
phase: 29-geometry-memory-and-timeouts
plan: 09
subsystem: api
tags: [pipeline, preservation, spool, shutil, exceptions, error-categories, duplex, pdf]

# Dependency graph
requires:
  - phase: 29-geometry-memory-and-timeouts
    provides: "plans 01-03's SpooledPageSink, PageRecord and the per-job <workspace>/spool/ directory"
  - phase: 29-geometry-memory-and-timeouts
    provides: "plan 06's green tree and the conftest spooling/spooling_in_turn test seam"
provides:
  - "HARD-02: a mid-batch failure keeps the pages already spooled as an unfiltered (partial) PDF under <data_dir>/failed/"
  - "the raised exception keeps its own type, so ErrorCategory and the CLI exit code are unchanged"
  - "D-10's uniform rule across pass B, an empty pass B, a flip-wait timeout and a broken flip prompt -- all keep the fronts"
  - "a cancel and a flip abort preserve NOTHING, by an explicit ScanCancelledError re-raise"
  - "a PdfError moves the spooled page files into failed/<job-keyed-name>/"
  - "_warn_if_failed_dir_growing counts those directories and sums them recursively, still never raising"
  - "the pipeline.py:1080 gap comment is deleted"
affects: [29-11, 30]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "a mutable _SpoolLedger passed down into the acquisition helpers so a guard one level up can read what a failed pass spooled"
    - "a narrow preservation guard that copies _preserving's move-plus-message shape but re-raises in its own window's register"
    - "failed/ holds two artefact kinds: preserved PDFs and job-keyed page directories"

key-files:
  created: []
  modified:
    - src/saneless/pipeline.py
    - tests/test_pipeline.py
    - tests/test_outcomes_e2e.py
    - tests/test_cli.py
    - .planning/phases/29-geometry-memory-and-timeouts/29-VALIDATION.md

key-decisions:
  - "The partial-scan guard is its own helper, not a call to _preserving: _preserving maps a foreign exception to PaperlessError, which around acquisition would blame paperless-ngx for a jammed feeder."
  - "ScanCancelledError is re-raised in its own except clause, before the broad one, because it is an ordinary Exception and a cancel must keep nothing."
  - "A foreign exception escaping acquisition is re-raised as ScanError, and one escaping assembly as PdfError -- the window's own register, mirroring _preserving's PaperlessError choice."
  - "Each acquisition pass registers its sink with the ledger BEFORE the pass runs, because the case the registration exists for is the pass that never returns."
  - "The preserved partial is assembled at the last resolution a pass actually reported, falling back to the profile's requested resolution when no pass completed."
  - "The assembly-failure directory name is Path(build_pdf_filename(job_id, title)).stem, so it inherits that function's sanitiser and uniqueness argument without touching pdf.py (plan 08 owns it this wave)."
  - "Page files move one at a time to explicit destination paths rather than moving the spool directory wholesale, so shutil.Error stays unreachable (T-29-35) and a pre-existing destination cannot nest one job's pages inside another's."
  - "_deliver was extracted from run_pipeline because the new guard pushed it over ruff's PLR0915 cap -- the same reason _finish_duplex_mismatch exists. No suppression."

patterns-established:
  - "Preservation guards report both failures in one message when preserving itself fails, and always re-raise type(exc)(msg) from exc for a SanelessError."
  - "Anything that must be unique across a whole job is keyed on build_pdf_filename's output, never on PageRecord.sequence (which is per-pass)."

requirements-completed: [HARD-02]

# Metrics
duration: 78min
completed: 2026-09-15
---

# Phase 29 Plan 09: Keep the Pages a Mid-Batch Failure Leaves Behind

**A jam on page 40 of a 50-sheet stack now keeps the 39 sheets already fed, as an unfiltered `(partial)` PDF under `failed/`, with the count and the path in an error that is still the scanner's own exception type — while an operator's cancel keeps nothing.**

## Performance

- **Duration:** ~78 min
- **Tasks:** 3 of 3
- **Files modified:** 5
- **Suite:** 1988 passing (from 1965 at the base commit), 73 deselected

## Accomplishments

- **HARD-02 delivered.** `_preserving_partial_scan` wraps acquisition, assembles whatever reached the spool into a partial PDF, moves it out of the workspace before the `TemporaryDirectory` unwinds, and re-raises `type(exc)(msg) from exc` so `classify_error` still returns the same category and the CLI still returns the same exit code.
- **D-10 applied uniformly.** A fault in either manual-duplex pass, an empty pass B, a flip-wait timeout and a broken flip prompt all keep pass A's fronts (and any partial backs) under the `(fronts)` / `(backs)` names the mismatch recovery already uses. A flip abort keeps nothing.
- **The `pipeline.py:1080` gap comment is gone,** deleted rather than reworded, and replaced at the abort path by a comment that states the cancel exclusion as policy.
- **`PdfError` no longer costs the operator the pages.** The spooled page files move into `failed/<job-keyed-name>/`, and `_warn_if_failed_dir_growing` counts that new artefact kind and adds its recursive size — inside the existing `try`, so it still never raises.
- **Nothing is uploaded on any partial path,** asserted directly rather than inferred.

## Task Commits

1. **Task 1: preserve the pages a mid-batch failure leaves behind** — `9514f40` (test, RED) → `a87cad1` (feat, GREEN)
2. **Task 2: keep the fronts when pass B or the flip fails** — `d0720a4` (test, RED) → `4300061` (feat, GREEN)
3. **Task 3: keep the page files when assembly fails, and count them** — `5f21ee0` (test, RED) → `1bfc22c` (feat, GREEN)

No REFACTOR commits: nothing needed cleaning up after GREEN. The `_deliver` extraction was part of task 1's GREEN because ruff's `PLR0915` cap is a gate, not a tidy-up.

## The exact message wording (plan 11 quotes these)

Recorded verbatim, as the plan's `<output>` requires. `{exc}` is the original exception's own `str()`.

**1. Partial scan preserved** (`_preserving_partial_scan`, `pipeline.py`):

```python
f"{exc}. The {pages} page(s) scanned before the error "
f"were preserved at {preserved}"
```

`{pages}` is the total across every pass that reached the spool; `{preserved}` is the destination paths joined by `", "` in pass order. A real one reads:

```
Scanner error on page 4: Document feeder jammed. The 3 page(s) scanned before the error were preserved at /data/failed/20260916-013255-4c8627dd-jammed-stack-partial.pdf
```

**2. Assembly failure, page files preserved** (`_preserving_page_files`, `pipeline.py`):

```python
f"{exc}. The {len(moved)} spooled page file(s) "
f"were preserved at {destination}"
```

A real one reads:

```
Could not assemble 1 page(s) into /tmp/x.pdf: No space left on device. The 1 spooled page file(s) were preserved at /data/failed/20260916-014030-4c8627dd-scan-2026-09-16
```

**3. Preservation itself failed** — both guards, the existing `_preserving` shape unchanged:

```python
f"{exc}. The scan could NOT be preserved to {failed_dir}: {keep_exc}"   # partial scan
f"{exc}. The scan could NOT be preserved to {destination}: {move_exc}"  # page files
```

**Artefact names.** The title suffixes in the source are `(partial)`, `(fronts)` and `(backs)`, but `build_pdf_filename` runs the whole title through `sanitise_title_for_filename`, whose allow-list drops brackets. On disk they therefore read `…-jammed-stack-partial.pdf`, `…-pass-b-jam-fronts.pdf`, `…-pass-b-jam-backs.pdf`. Documentation must quote the on-disk form, not the bracketed one.

## Files Created/Modified

- `src/saneless/pipeline.py` — `_SpoolLedger`, `_preserve_partial_passes`, `_preserving_partial_scan`, `_preserving_page_files`, `_deliver`; `_warn_if_failed_dir_growing` extended; `_scan_simplex` and `_scan_manual_duplex` register their sinks; `run_pipeline` rewired; the gap comment deleted.
- `tests/test_pipeline.py` — three new classes (`TestPartialScanPreservation`, `TestPassBAndFlipFailuresKeepTheFronts`, `TestAssemblyFailureKeepsThePageFiles`, `TestFailedDirCountsPreservedPageDirectories`) and helpers; three existing tests updated for the new behaviour.
- `tests/test_outcomes_e2e.py` — `TestAPartialScanSurvivesTheWorker`; the `flip-timeout` case now expects one preserved PDF.
- `tests/test_cli.py` — two exit-code tests updated: the error lines now carry the preservation clause, and both still assert the exit code and the one-line shape.
- `.planning/phases/29-geometry-memory-and-timeouts/29-VALIDATION.md` — the four HARD-02 / SC2 rows marked delivered.

## Decisions Made

Beyond the frontmatter's list, two are worth the space:

**The guard spans acquisition only, and stops short of the duplex-mismatch delivery.** `_finish_duplex_mismatch` already carries its own `_preserving` guard over both halves; nesting the partial-scan guard around it would have preserved the same two halves twice, once as `(fronts)`/`(backs)` partials and once as the mismatch recovery's own. The `match` on the acquisition result was therefore moved *outside* the `with` block.

**`_drop_empty_pages` is deliberately outside both guards.** "All pages were blank" is a filtering verdict on a scan that completed, not a scan that was interrupted, and D-09/D-10 do not name it. Keeping the guard narrow also left `test_all_blank_pages_say_all_pages_were_blank` untouched.

## Deviations from Plan

### Acceptance criteria that could not hold as literally written

**1. [Rule 2 — corrected criterion] `grep -c 'No back pages were scanned in pass B'` returns 2, not 1**
- **Found during:** Task 2
- **Issue:** The criterion says the grep "returns 1 — the message is unchanged". It returned **2 at the base commit already**: once in `_scan_manual_duplex`'s `Raises:` docstring (line 1344) and once as the message literal (line 1442).
- **Resolution:** The criterion's *intent* — the message text is unchanged — is satisfied and directly proven by `test_pass_b_preserves_fronts_when_it_is_empty`, which asserts the raised message `startswith` the exact sentence. Neither occurrence was touched.
- **Verification:** `grep -nF 'No back pages were scanned in pass B' src/saneless/pipeline.py` shows both pre-existing sites.

**2. [Rule 2 — corrected criterion] the preserved PDF's name contains `partial`, not `(partial)`**
- **Found during:** Task 1
- **Issue:** The behaviour spec asks for "a PDF ... whose name contains `(partial)`". `build_pdf_filename` puts the title through `sanitise_title_for_filename`, whose allow-list replaces brackets, so a file name can never contain them.
- **Resolution:** The source literal is `(partial)` (so `grep -c '(partial)'` returns 3 ≥ 1), and the test asserts `"partial" in preserved[0].name` with a comment naming the sanitiser. This is exactly the precedent the duplex-mismatch test already sets, which asserts on `"fronts"`.
- **Verification:** `tests/test_pipeline.py::TestPartialScanPreservation::test_partial_scan_preserved_when_the_scanner_fails_mid_batch`.

### Auto-fixed issues

**3. [Rule 3 — blocking] `run_pipeline` exceeded ruff's `PLR0915` statement cap**
- **Found during:** Task 1
- **Issue:** Adding the guard took `run_pipeline` to 52 statements against a cap of 50. CLAUDE.md forbids a suppression.
- **Fix:** Extracted `_deliver(pdf_path, paperless, request, settings) -> tuple[ScanOutcome, str | None]`, carrying the upload, the poll and the existing `_preserving` guard with every comment intact. The plan anticipated this ("extract a helper the way `_finish_duplex_mismatch` already exists").
- **Files modified:** `src/saneless/pipeline.py`
- **Verification:** `uv run ruff check .` clean; all delivery tests unchanged and passing.
- **Committed in:** `a87cad1`

**4. [Rule 1 — bug in a test, surfaced by the change] two CLI tests pinned error lines that now continue**
- **Found during:** Tasks 2 and 3
- **Issue:** `tests/test_cli.py::TestManualDuplexPrompt::test_a_broken_prompt_exits_1_with_the_prompt_failure` asserted the flip-failure line by exact equality, and `TestExitCodes::test_unassemblable_pdf_exits_4_with_one_line` asserted stderr equalled one exact line. Both now carry the preservation clause. **The behaviour under test — exit 1 and exit 4, one line, no traceback — is unchanged**, which is the whole point of preserving the exception type.
- **Fix:** Both assertions became a prefix check plus a positive assertion that the preservation clause is present, so the tests now pin *more* than they did. Exit codes and the one-line shape are still asserted exactly.
- **Files modified:** `tests/test_cli.py` (outside the plan's declared file set, but unowned by the two agents running in parallel this wave)
- **Verification:** both tests pass; full suite green.
- **Committed in:** `4300061`, `1bfc22c`

**5. [Rule 1 — bug] three `tests/test_pipeline.py` tests encoded the behaviour this plan removes**
- **Found during:** Task 2
- **Issue:** `test_an_empty_pass_b_fails_before_any_half_is_assembled` asserted `assemble_pdf` was never called (preservation now calls it), and anchored the message with `$`. `test_manual_duplex_broken_prompt_abort_is_a_scan_failure_not_a_cancel` anchored likewise and asserted `__cause__ is cause` (the chain is now one link longer). `test_manual_duplex_flip_wait_timeout_raises` wrote its preserved PDF into the suite's shared `/tmp` data directory.
- **Fix:** All three now isolate `data_dir`, pin the original sentence as a prefix, and — for the broken prompt — walk one link to reach the OSError, with a comment explaining that nothing was lost. Their original subjects (no *delivery*, still a `ScanError`, `scan_pages` called once) are all still asserted.
- **Files modified:** `tests/test_pipeline.py`
- **Verification:** `uv run pytest tests/test_pipeline.py -q` — 126 passing.
- **Committed in:** `4300061`

**6. [Rule 1 — bug in a new test] two job ids collided in the preserved directory name**
- **Found during:** Task 3
- **Issue:** `test_two_assembly_failures_make_two_directories` first used `job-pdf-first` / `job-pdf-second`. `build_pdf_filename` truncates the job id to its first **8** characters, so both collapsed to `job-pdf` and produced one directory. That is a property of `build_pdf_filename`, not of the new code.
- **Fix:** Ids that differ inside eight characters (`pdf-one` / `pdf-two`), matching the sibling test `test_two_preserved_scans_sharing_a_title_are_two_files`, with a docstring naming the constraint and noting production ids are uuid4s.
- **Files modified:** `tests/test_pipeline.py`
- **Verification:** the test now produces two directories with distinct names, two pages each.
- **Committed in:** `1bfc22c`

---

**Total deviations:** 2 corrected acceptance criteria, 4 auto-fixed (1 × Rule 3, 3 × Rule 1).
**Impact on plan:** No scope creep. The only file touched outside the declared set is `tests/test_cli.py`, and only because the plan's own behaviour change moved two error lines it pinned; that file is owned by neither parallel agent this wave.

## Issues Encountered

**Serena's project root is the main checkout, not this worktree.** The first `insert_before_symbol` call reported success while writing to `/home/kris/git/saneless/tests/test_pipeline.py`. The edit was reverted immediately with a matching `replace_content`, and `diff -q` confirmed the main checkout's file is byte-identical to the pristine worktree copy. Every subsequent edit used `Edit`/`Write` with worktree-absolute paths. **Any agent using Serena from a `.claude/worktrees/` worktree in this repo should verify the target file changed before trusting the tool's "OK".**

**`PageRecord.sequence` is per pass, not per document** (flagged by the 29-08 agent mid-execution). Nothing here keys on it: partials are assembled per pass into separately named PDFs, and the page-file move reuses the spool's own pass-labelled names (`a-0001.png`, `b-0001.png`), which are already unique within one spool directory. Checked deliberately rather than assumed.

## Deferred / noted, not fixed

- **`tests/test_worker.py::test_a_pass_a_failure_after_stop_claimed_the_flip_stays_a_failure`** now preserves a `(fronts)` PDF into the suite's *shared* `/tmp/saneless-test/data/failed`, because it uses the `default_settings` fixture without repointing `data_dir`. The test passes and its assertions are unaffected; only the scratch directory accumulates a file per run. Leaking into `_TEST_DATA` is a pre-existing pattern in that suite (that directory already held ~280 files from other tests), and `tests/test_worker.py` is outside this plan's file set, so it was left alone. A one-line `data_dir` repoint would fix it.
- **`build_pdf_filename` truncates the job id to 8 characters,** so two jobs whose ids share that prefix, run in the same second under the same title, collide. This is pre-existing and applies equally to the preserved PDFs Phase 23 already writes; the new page-directory move degrades gracefully (pages merge into one directory rather than nesting). `src/saneless/pdf.py` is plan 08's this wave, so it was not touched.

## Verification

| Check | Result |
|---|---|
| `uv run pytest -m "not browser and not sane_hardware"` | 1988 passed, 73 deselected |
| `uv run pytest tests/test_pipeline.py -k "partial_scan_preserved or cancel_preserves_nothing" -x -q` | 2 passed |
| `uv run pytest tests/test_pipeline.py -k pass_b_preserves_fronts -x -q` | 4 passed |
| `uv run pytest tests/test_pipeline.py -k failed_dir_counts_directories -x -q` | 3 passed |
| `uv run pytest tests/test_outcomes_e2e.py -k partial_scan_preserved -x -q` | 1 passed |
| `uv run ruff check .` / `uv run ruff format --check .` | clean / 55 files already formatted |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |
| `uv run prek run --all-files` | exit 0 |
| `uv run prek run --stage pre-push --all-files` | exit 0 |
| `git diff --name-only <base>` includes `src/saneless/pdf.py` | no |
| suppressions added (`# type: ignore`, `# noqa`, rule disables, `--no-verify`, `SKIP=`) | zero |

Acceptance greps over `src/saneless/pipeline.py` (`grep -cF`, so these are matching **lines**): `(partial)` 3, `were preserved at` 2, `ScanCancelledError` 8, `raise type(exc)(msg) from exc` 3, `pages_kept` 0, `The fronts are still lost here` 0, `(fronts)` 9, `(backs)` 10, `rglob` 1, `is_dir()` 1, `must never raise` 1.

## Self-Check: PASSED

All five modified/created files exist on disk; all six task commits are in `git log`; every verification command above was run from this worktree and its output read from redirected files (the shell hook misreports pytest summaries).

## Next Phase Readiness

- **Plan 11 (docs)** can quote the two message shapes above verbatim, and must use the **on-disk** artefact names (`…-partial.pdf`, `…-fronts.pdf`, `…-backs.pdf`), not the bracketed source spellings. The doc-truth sites Finding 11 lists for D-09/D-10 are now all true to change: `set-up-adf-duplex.md:95/97/162`, `troubleshoot-a-failed-scan.md:47-48` plus a new partial-scan row, `consume-directory-fallback.md:62` (`failed/` now also holds page directories), and `docker.md:52-54`.
- **Plan 10** is untouched by this: no SANE init guard and no entry-point `close()` were pulled forward.
- **Phase 30 / APPL-03** owns the structured form. No `pages_kept` attribute was added to any exception — the message is what is locked here.

---
*Phase: 29-geometry-memory-and-timeouts*
*Completed: 2026-09-15*
