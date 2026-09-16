---
phase: 29-geometry-memory-and-timeouts
plan: 08
subsystem: pdf
tags: [img2pdf, pikepdf, qpdf, memory, assembly, ru_maxrss, subprocess]

# Dependency graph
requires:
  - phase: 29-geometry-memory-and-timeouts
    provides: "plan 01's pikepdf runtime dependency and SpooledPageSink; plan 03's assemble_pdf(records, ...) signature; plan 05's test_pdf.py fixtures (spool_dir/output_dir/spool_pages, _embedded_streams)"
provides:
  - "assemble_pdf converts one page per img2pdf.convert call, streaming each to its own single-page PDF, and merges them with pikepdf.Job(['qpdf', '--empty', '--pages', ...])"
  - "HARD-01's memory half on the assembly side: peak RSS measured flat in page count, in a child process, at two page counts"
  - "a merge-equivalence proof: same page count, same exact MediaBox per page, same embedded image stream bytes as a single-convert PDF"
  - "a duplex regression proof: two records legitimately sharing PageRecord.sequence still produce two distinct pages"
affects: [29-09, 29-11]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Bound a library that buffers a whole document by calling it once per unit and merging natively, rather than by handing it a stream"
    - "Prove a memory bound by peak ru_maxrss in a child process at two input sizes, with the threshold derived from the input's decoded size rather than hard-coded"
    - "Make test pages incompressible (random pixels) so that 'decoded size' is an honest stand-in for 'stored size' in a memory budget"
    - "Launch a measurement child the test_atomic_write.py way: literal argv, per-run values in the environment, shell=False, explicit timeout"

key-files:
  created: []
  modified:
    - src/saneless/pdf.py
    - tests/test_pdf.py

key-decisions:
  - "The single-page scratch PDFs are named from each record's POSITION in `records`, not from `PageRecord.sequence`: sequences are per acquisition pass, so a manual-duplex interleave has two records numbered 1 and a sequence-named scratch file merges one page twice. Verified: the duplex test fails under sequence naming."
  - "The `pdf_bytes is None` guard and `test_convert_returning_none_becomes_pdf_error` are deleted together; convert returns None by design once outputstream= is passed"
  - "No new `except` clause for pikepdf: `Job.run()` raises ordinary `Exception` subclasses and the existing broad boundary already covered them (EXC-01 unaffected)"
  - "The memory child is launched through a literal `/bin/sh -c` argv with the interpreter in the environment, because `sys.executable` in an argv is what takes the call off ruff's S603 allow-list and this project adds no suppressions"

patterns-established:
  - "A memory-bound assertion states its arithmetic in the test body: decoded page bytes x extra pages, so the threshold moves with the fixture instead of rotting"
  - "Prove a RED interval for a performance test by temporarily restoring the old implementation and recording the number it produced, not by asserting the old shape was worse"

requirements-completed: []  # HARD-01's assembly-memory half lands here; the requirement also needs 29-04's acquisition half

# Metrics
duration: ~75min
completed: 2026-09-15
---

# Phase 29 Plan 08: Bounded PDF Assembly Summary

**`assemble_pdf` now converts one page per `img2pdf.convert(..., outputstream=...)` call and merges the single-page PDFs with qpdf through `pikepdf.Job`, which measured flat at 72.7–73.3 MiB peak RSS for both 4 and 16 pages where the single-convert implementation grew by 99 MB over the same twelve extra pages — with the merged PDF byte-for-byte equivalent to the single-convert one.**

## The Measurement, Reproducibly

`tests/test_pdf.py::TestAssemblyMemory::test_peak_memory_is_flat_in_page_count`. The child spools N pages of **random** RGB pixels at 1200×1200 through a real `SpooledPageSink`, assembles them, and prints `resource.getrusage(resource.RUSAGE_SELF).ru_maxrss`. Random pixels are load-bearing: deflate cannot compress noise, so the spooled PNG lands at 4.327 MB against a 4.32 MB decoded page — within 0.2% — which is what makes "the decoded size of the extra pages" an honest stand-in for what a linear assembly would be holding.

Reproduce with `uv run pytest tests/test_pdf.py -k "memory or flat" -x -q` (3.9 s).

### Peak RSS, this machine, five repeats of the committed implementation

| Run | 4 pages (KiB) | 16 pages (KiB) | Growth |
|-----|---------------|----------------|--------|
| 1 | 73,196 | 73,092 | −0.11 MB |
| 2 | 73,016 | 73,088 | +0.07 MB |
| 3 | 72,740 | 72,996 | +0.26 MB |
| 4 | 73,300 | 73,104 | −0.20 MB |
| 5 | 72,992 | 73,232 | +0.25 MB |

**Noise band: 72,740–73,300 KiB, a 560 KiB (0.77%) spread; observed growth −0.20 MB to +0.26 MB.** The asserted budget is 12 × 1200 × 1200 × 3 = 51,840,000 bytes (49.4 MiB), roughly **90× the noise band** and about **200× the largest observed growth**. There is no flakiness risk at this margin, so the page size did not need widening.

### The same test against the two rejected shapes

Measured by temporarily restoring each implementation, running the test, and reverting — so these are this test's own numbers, not a transcription of the research table.

| Strategy | 4 pages | 16 pages | Growth | Verdict |
|----------|---------|----------|--------|---------|
| **per-page convert + `pikepdf.Job` merge (committed)** | 72.7–73.3 MiB | 72.7–73.3 MiB | ≤ 0.26 MB | **passes** (budget 49.4 MiB) |
| single `convert` → `write_bytes` (before this plan) | — | — | **99.1 MB** | fails: `103,976,960 < 51,840,000` is false |
| single `convert(..., outputstream=f)` (D-03 as originally written) | 86,888 KiB | 146,232 KiB | **58.0 MB** | fails the same budget |

The third row is the point of CONTEXT.md's amendment: `outputstream=` alone is still linear, and this test catches it — though with a thinner margin (58.0 MB against a 49.4 MB budget, 1.17×) than the single-convert shape (2.0×). Worth knowing if the fixture is ever shrunk.

## Equivalence: the merged PDF is not a different document

`test_merge_matches_a_single_convert_pdf` assembles three pages (two A4 300 DPI, one half-size) both ways and asserts:

- `_media_boxes(merged) == _media_boxes(reference)` — **exact** floats, not rounded, because this compares two PDFs against each other rather than against a nominal paper size. Observed `(0.0, 0.0, 595.2, 841.92)` on both.
- `_embedded_streams(merged) == _embedded_streams(reference)` — the raw `/FlateDecode` stream per page.

And `test_merge_produces_one_page_per_record_in_record_order` closes the loop back to the spool: each page's embedded stream equals `_png_idat_payload(record.path)`, the concatenated IDAT payload of the spooled PNG itself. That is D-03's "the spooled PNG is exactly what img2pdf embeds", asserted on the bytes rather than on the pixels — a re-encode would satisfy a pixel comparison and fails this one.

## The Duplex Bug the Plan's Naming Would Have Shipped

The plan's `<action>` said to name each single-page scratch PDF `f"{record.sequence:04d}.pdf"`. **`PageRecord.sequence` is assigned per acquisition pass** (`spool.py:147`, one `_sequence` per sink), and a manual-duplex job builds two sinks. After the interleave, records 1 and 2 of the document are both `sequence == 1`: their *file names* differ (`a-0001.png`, `b-0001.png`), their numbers do not.

With sequence naming, the back page overwrites the front's scratch PDF and `singles` lists `0001.pdf` twice, so the merge emits one page twice and drops the other. Implemented with the **position in `records`** instead — which is the document order, unique by construction, still nothing sorted and nothing globbed (D-02).

Proven, not assumed: `test_merge_survives_duplex_records_sharing_a_sequence_number` builds four records numbered `[1, 1, 2, 2]` through two real sinks, and it **fails** under sequence naming (verified by temporarily switching the implementation) and passes under position naming.

## What Changed in `src/saneless/pdf.py`

- Deleted the `if pdf_bytes is None: raise PdfError(...)` guard. `convert` returns `None` by design once `outputstream=` is passed, so keeping it would have turned every page into a `PdfError`.
- Reinstated `tempfile.TemporaryDirectory(dir=str(output_dir))` as the scratch space for the single-page PDFs (four-digit zero-padded names, `.pdf`). It is cleaned up on the success and the failure path alike — `test_merge_leaves_no_single_page_pdf_behind` and the existing `test_no_second_encode_survives_a_failed_assembly` both hold.
- Moved `from pathlib import Path` out of `TYPE_CHECKING`: it is a runtime import now.
- Kept verbatim: the `(x_dpi, y_dpi)` 2-tuple comment and the 1860×2631 pt monster it names; the empty-input guard and its message; the `dpi`-is-supplied-by-the-caller paragraph; `except PdfError: raise` with its comment; and `except Exception as exc:` with its "narrow in *span*, broad in *type*" justification — extended, not replaced, to name `pikepdf.Job.run()` and `pikepdf.PdfError`, and to record that **no new `except` clause was needed**, so EXC-01 is unaffected.
- Recorded the measured figures (1395 MB / 787 MB / flat 131 MB) and the accepted ~12 ms-per-page GIL cost in the function docstring, in the repo's `Measured, not assumed:` register with a closing "that is a settled answer".

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 — Bug] Scratch PDFs named from list position, not `record.sequence`**

- **Found during:** Task 1, while reading `SpooledPageSink.add`
- **Issue:** The plan mandated `f"{record.sequence:04d}.pdf"`. Sequence numbers are per acquisition pass, so a manual-duplex interleave supplies duplicates and the scratch files collide — one page merged twice, one lost.
- **Fix:** `for position, record in enumerate(records, start=1)` and `f"{position:04d}.pdf"`. The plan's stated intent ("in document order by construction and nothing sorts or globs it") is better served by position than by sequence.
- **Files modified:** `src/saneless/pdf.py`, `tests/test_pdf.py` (regression test added)
- **Commit:** `4a00dbb`

**2. [Rule 3 — Blocking] The memory child is launched through a literal argv plus the environment**

- **Found during:** Task 2, at `uv run ruff check .`
- **Issue:** The plan's prose asked for `subprocess.run([sys.executable, str(script), ...])`. Ruff's `S603` allow-list only accepts an argv of string literals (verified against `--stdin-filename tests/probe.py`: a literal list passes, anything containing `sys.executable` is flagged). CLAUDE.md forbids `# noqa` and forbids disabling rules, so the plan's literal shape cannot land.
- **Fix:** Adopted the in-repo precedent, `test_atomic_write._run_in_mount_namespace`: every argv element is a literal (`["/bin/sh", "-c", 'exec "$SANELESS_TEST_PYTHON" "$SANELESS_TEST_SCRIPT"']`), `sys.executable` and the per-run values travel in the environment, `shell=False`, `capture_output=True, text=True, check=False`, explicit `timeout=20`. Nothing is interpolated into the shell string, so T-29-33's "non-shell `subprocess.run` with an explicit timeout" still holds, and the plan's own instruction to follow `test_atomic_write.py`'s pattern is satisfied more literally than its argv sketch was.
- **Files modified:** `tests/test_pdf.py`
- **Commit:** `99fb4d6`

### Acceptance Criteria Not Met Literally

**`grep -c 'pikepdf.Job' src/saneless/pdf.py` returns 4, not 1.**

Exactly one is code — `pdf.py:286`, the single construction. The other three are docstring prose that the same task's `<action>` **mandated**: "Record the accepted cost: `pikepdf.Job.run()` holds the GIL…" and "extend that justification's list of what it covers with pikepdf and qpdf exceptions". The `<action>` is normative, so the prose stayed and the criterion's *intent* — one Job, constructed once — is proven directly instead, by `test_merge_runs_exactly_one_qpdf_job`, which asserts `len(argvs) == 1` against a recording stand-in and additionally checks the argv shape and that every input path it names lives under `output_dir` (T-29-29). Every other grep criterion in both tasks passes exactly as written.

**Task 2's "fill in the SC1 / D-03 row of `29-VALIDATION.md`" needed no edit.**

The row already reads `| SC1 / D-03 | … | 29-08 T1+T2 | ❌ W0 | ⬜ pending |` — the planner filled it when the plan was written. The criterion ("the SC1 / D-03 row names plan 29-08") is satisfied as the file stands. `29-VALIDATION.md` is also outside this plan's `files_modified` and outside this executor's ownership, so its `File Exists` / `Status` columns are left for phase tracking.

## Threat Model Follow-Through

| Threat ID | Disposition | Where it landed |
|-----------|-------------|-----------------|
| T-29-29 | mitigate | Every argv element is a literal or a file the call just wrote inside its own `TemporaryDirectory` under `output_dir`; asserted in `test_merge_runs_exactly_one_qpdf_job` via `Path(single).is_relative_to(output_dir)`. A code comment says so at the construction site. |
| T-29-30 | mitigate | This plan; measured above. |
| T-29-31 | accept | Recorded in the `assemble_pdf` docstring with the ~12 ms/page figure and the 500-page ≈ 6 s consequence. |
| T-29-32 | accept | The only PDFs qpdf reads are the single-page files this function just wrote. |
| T-29-33 | mitigate | `shell=False`, literal argv, explicit `timeout=20`, nothing interpolated. |

## Verification

| Command | Result |
|---------|--------|
| `uv run pytest tests/test_pdf.py -q` | 88 passed |
| `uv run pytest tests/test_pdf.py -k merge -x -q` | 6 passed (criterion asked ≥ 2) |
| `uv run pytest tests/test_pdf.py -k "memory or flat" -x -q` | 1 passed (criterion asked ≥ 1) |
| `uv run pytest -m "not browser and not sane_hardware"` | **1972 passed**, 73 deselected, 47.6 s |
| `uv run ruff check . && uv run ruff format --check .` | clean |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |
| `uv run prek run --all-files` | exit 0 |
| `uv run prek run --stage pre-push --all-files` | exit 0 |

No suppressions were added; no hook was bypassed; no file outside `src/saneless/pdf.py` and `tests/test_pdf.py` was touched.

## TDD Gate Compliance

| Gate | Commit | Evidence |
|------|--------|----------|
| RED (task 1) | `67ebbf3` `test(29-08)` | 3 failed, 83 passed — the three shape assertions (`convert` per page, one `pikepdf.Job`, the pikepdf failure path) all failed against the single-convert implementation |
| GREEN (task 1) | `4a00dbb` `feat(29-08)` | 87 passed |
| RED (task 2) | verified before `99fb4d6` | with the single-convert body temporarily restored: `AssertionError: peak grew 103,976,960 bytes … not less than 51,840,000` |
| GREEN (task 2) | `99fb4d6` `test(29-08)` | 88 passed; full suite 1972 passed |

The equivalence tests (`test_merge_matches_a_single_convert_pdf`, `test_merge_produces_one_page_per_record_in_record_order`) passed in the RED commit too, and that is by design: they assert the output must *not* change, so passing before and after is the claim.

## Known Stubs

None.

## Threat Flags

None. No new network endpoint, auth path, file-access pattern or schema at a trust boundary. The one new native call surface (`pikepdf.Job`) is covered by T-29-29 and T-29-32 above.

## Self-Check: PASSED

- `src/saneless/pdf.py` — FOUND
- `tests/test_pdf.py` — FOUND
- `.planning/phases/29-geometry-memory-and-timeouts/29-08-SUMMARY.md` — FOUND
- commit `67ebbf3` — FOUND
- commit `4a00dbb` — FOUND
- commit `99fb4d6` — FOUND
