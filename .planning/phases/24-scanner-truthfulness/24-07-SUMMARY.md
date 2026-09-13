---
phase: 24-scanner-truthfulness
plan: "07"
subsystem: scanner
tags: [scanner, pipeline, pdf, dpi, dataclass, abc, tdd, sweep]
requires:
  - "24-04 — rejected_pages, the local integrity-rejection counter this plan promotes"
  - "24-05 — _configure_device's read-back resolution, the second fact this plan carries out"
  - "24-06 — actual_resolution bound at the scan_pages call site, with two consumers already"
provides:
  - "ScanBatch — a frozen three-field record: pages, actual_resolution, pages_rejected"
  - "ScannerBackend.scan_pages returning ScanBatch instead of yielding images"
  - "an eager SaneBackend.scan_pages that closes the device before it returns"
  - "_acquire_pages returning (pages, rejected) rather than yielding"
  - "_DuplexMismatch — the named record replacing the bare (fronts, backs) tuple"
  - "_duplex_resolution, _rejected_pages_warning, _join_warnings"
  - "tests/conftest.py scan_batch() — the one ScanBatch builder every stub shares"
  - "assemble_pdf fed the device's read-back dpi at both call sites"
affects:
  - "24-08 — MockSaneDev and _FakeSaneDevice are still slated for deletion; untouched here"
  - "25 — manual duplex inherits _DuplexMismatch and the pass-A resolution rule"
  - "29 — HARD-01 still owns ordered per-page records; ScanBatch is deliberately too small to pre-empt it"
  - "30 — APPL-03 renders pages_removed as blank-page removal; the rejection count is kept out of it"
tech-stack:
  added: []
  patterns:
    - "Change the ABC rather than smuggle a fact past it -- list() discards a generator's return value"
    - "Enumerate the blast radius in the tree you are on; a recorded count from an earlier wave is stale"
    - "A named record beats a positional tuple the moment a third field appears"
    - "One warning field, many possible warnings: join them rather than let one overwrite another"
key-files:
  created:
    - .planning/phases/24-scanner-truthfulness/24-07-SUMMARY.md
  modified:
    - src/saneless/scanner/base.py
    - src/saneless/scanner/sane_backend.py
    - src/saneless/scanner/__init__.py
    - src/saneless/pipeline.py
    - src/saneless/pdf.py
    - tests/conftest.py
    - tests/test_scanner.py
    - tests/test_pipeline.py
    - tests/test_outcomes_e2e.py
    - tests/test_cli.py
    - tests/test_web.py
    - tests/test_web_state_rendering.py
    - tests/test_browser.py
    - tests/test_sane_hardware.py
decisions:
  - "ScanBatch is frozen=True, the codebase's first frozen value object in the scanner layer, because it reports what a device already did"
  - "ScanBatch lives in scanner/base.py, not pipeline.py -- Phase 21 D-03 forbids the scanner package depending on job vocabulary"
  - "Manual duplex takes pass A's resolution and logs a WARNING naming both when the passes disagree"
  - "The rejection count is surfaced as a WARNING plus the ScanResult warning field; no job-store column, no migration"
  - "PATTERNS.md's 89 could not be reproduced as a line count; the sweep was driven by enumeration and verified by three suites plus both type checkers"
metrics:
  tasks: 3
  commits: 6
  tests_added: 12
  suite: "882 passed, 34 deselected (baseline was 870 passed, 34 deselected)"
  hardware_suite: "4 passed (baseline was 4 passed)"
  browser_suite: "30 passed"
completed: 2026-09-13
---

# Phase 24 Plan 07: One Route Out of the Scanner, and the MediaBox That Follows It Summary

The two facts the backend had been measuring since plans 24-04 and 24-05 — the resolution the device actually used, and the sheets it could not read — now have exactly one way out of it, and the PDF's declared page size finally comes from the first of them instead of from a number the device may never have honoured.

## The Headline: the MediaBox Now Agrees With the Pixels

Phase 23 made the profile's requested resolution authoritative for `img2pdf.get_fixed_dpi_layout_fun`. Plan 24-05 then measured what SANE actually does with a request: **5000 dpi comes back as 1200.0, and 0 comes back as 1.0, with no error and no signal to the caller.** Those two facts together were a live defect — a device that substitutes produced a page cropped at one resolution and a MediaBox declaring another, which re-opened part of OUTC-06, Phase 23's headline fix.

Both `dpi=` call sites are now fed from the device:

```
grep -c 'dpi=profile.resolution' src/saneless/pipeline.py   ->  0
```

and the test that proves it was shown failing first: a profile asking for 600 against a device that reports 300 asserts on the `dpi` argument `assemble_pdf` actually received.

## What Was Built

### Task 1 — ScanBatch, the ABC, and an eager backend

RED `15d8378`, GREEN `df1bf78`.

`scan_pages` yielded `Image` only. That is why neither fact could leave the backend: **a generator's return value is discarded by `list()`**, and all three pipeline call sites did exactly that. The fix was necessarily the ABC, not a second channel around it.

`ScanBatch` carries three fields and nothing else:

```
['pages', 'actual_resolution', 'pages_rejected']     frozen=True
```

It lives in `scanner/base.py` rather than `pipeline.py`, because Phase 21's D-03 forbids the scanner package depending on the job vocabulary and these are scanner-layer facts.

`_acquire_pages` stopped yielding and now returns `(pages, rejected_pages)`; `SaneBackend.scan_pages` builds the list inside the `with self._open_device(...)` block and returns the batch outside it.

**The deterministic close is recorded as a consequence, not a goal.** The generator held the handle open until it was drained or garbage-collected; an eager return releases it when the function returns. Close-while-reading and cancel semantics were deliberately **not** folded in — those are Phase 29's HARD-03/HARD-04 and touch these same lines.

### Task 2 — the sweep, by enumeration

Commit `7d00552`.

`tests/conftest.py` gained `scan_batch()`, the single builder every stub shares, and the `mock_scanner` fixture was fixed once so every consumer that does not build its own mock followed for free. Then: 68 `list(...scan_pages(...))` sites unwrapped, 25 `MagicMock` stub assignments switched to a batch, six concrete implementers rewritten, and `_scan_adf_pages`'s two call sites taught to unpack a tuple.

### Task 3 — the pipeline uses what the device chose

RED `57c6d7a`, GREEN `56cc57c`.

`_scan_simplex` returns the `ScanBatch`. `_scan_manual_duplex` returns a `ScanBatch` when the passes agree and a **`_DuplexMismatch`** when they do not — a named record replacing the bare `(fronts, backs)` tuple, because the recovery path now needs the dpi and the rejection count too and a four-element positional tuple would make every call site remember an order. No second result type was invented: the agreeing case reuses `ScanBatch`.

## frozen=True Versus a Plain Dataclass — Stated, Not Inherited

There is **no `frozen=True` precedent in this codebase**: `DeviceInfo`, `DeviceCapabilities`, `ScanSettings`, `PipelineRequest` and `pipeline.ScanResult` are all plain `@dataclass`, so copying the nearest analog verbatim would have produced a mutable object. The choice was made deliberately and is recorded in the class's own docstring.

`ScanBatch` is **frozen**. It is a report of what a device has already done, and nothing downstream has any business rewriting it afterwards — a stale or hand-set DPI reaching `assemble_pdf` is precisely T-24-26. `_DeliveryContext`, added in Phase 23, is the one existing frozen record in `pipeline.py` and is the same kind of object, which is the closest thing to a precedent there is.

The object is also **deliberately too small to grow**. Its docstring names HARD-01 by ID (2 mentions) and says a per-page fact belongs there and not here, so a later plan extends it on purpose rather than by drift. It carries no geometry field either, because D-19 reuses the crop fallback instead of reporting the area back out.

## The Reference Count — Accounted, With the Discrepancy Explained

The plan and PATTERNS.md both carry **89** as the measured blast radius. **That number could not be reproduced as a line count in the tree this plan ran on, and the orientation brief was right to flag it as stale.** What was measured here, before any edit:

| Measurement | Before | After |
|---|---|---|
| `grep -rn 'scan_pages'` over `src tests` (naive substring) | **135** | 150 |
| `grep -rnw 'scan_pages'` (word boundary) | **119** | 132 |
| `_multi_scan_pages` lines in `test_scanner.py` | 14 | 16 |

Two separate things drive the divergence, and both are worth recording:

1. **A naive grep over-counts.** `_multi_scan_pages` — the legacy double's page list, 14 lines — *contains* `scan_pages` as a substring, so it matches `grep 'scan_pages'` but not `grep -w`. Those lines have nothing to do with the ABC.
2. **PATTERNS' 89 is a sum of category rows, not a line count.** Its own rows (3 production + 2 ABC/impl + 23 mock sites + 25 stub assignments + 6 concrete classes + ~40 assertions) add to ~99, and they count *occurrences within categories*, which neither matches nor was intended to match a `grep | wc -l`.

Two of PATTERNS' specific claims were also already false in this tree, exactly as the brief warned: `tests/test_web.py:63` **does** subclass `ScannerBackend` (`class StubScanner(ScannerBackend)` at line 48), so the checkers *did* flag it; and `test_cli.py:234`'s `FailScanner` raises before returning, so it needed no change at all.

**The sweep was therefore driven by enumeration and verified by execution, not by hitting a target number.** The decisive evidence is that all three suites are green and both type checkers are clean — 66 of the 71 post-change `ty` diagnostics were in `test_scanner.py` alone, and every one is now resolved.

## The Two Duck-Typed Sites — How They Were Actually Found

The plan named `test_cli.py:234` and `test_web.py:63`. **Only one of those was still duck-typed**, and it was not the one the plan expected to matter:

| Site | Reality in this tree | How it was caught |
|---|---|---|
| `test_cli.py:126` `MockSaneBackend` | Duck-typed, returned `iter([img])`. **No checker diagnostic.** | Enumerated by grep; would have failed at runtime on `.pages` |
| `test_cli.py:240` `FailScanner` | Duck-typed, but raises `ScanError` before returning anything | Enumerated by grep; **confirmed correct by running the test**, not assumed |
| `test_web.py:48` `StubScanner` | **Subclasses** `ScannerBackend` — the checkers flagged it | `ty` / `pyrefly` |

So the genuinely invisible site was `test_cli.py:126`, which the plan did not single out, while the site it did single out was already visible. That is the argument for enumeration stated better than the plan stated it: the *list* of invisible sites was itself stale, so only a fresh grep plus a green test run could settle it.

## How Manual Duplex Combines Two Resolutions

`_duplex_resolution(front, back)` takes **pass A's value**, and logs a WARNING naming both when they differ:

```
Manual duplex passes disagree on resolution: pass A reports %s dpi,
pass B reports %s dpi; assembling at %s dpi
```

Both passes run with identical settings against one device, so they should be identical in practice; a difference means the device changed its mind mid-job. Pass A wins because the choice between two equally plausible numbers is arbitrary — **which is exactly why it is logged rather than made silently**. Failing the run instead would discard a scan that completed, over a disagreement the crop fallback already tolerates. A test drives 300 against 150 and asserts both numbers appear in a warning *and* that assembly used 300.

Rejections are **summed** across the two passes, not picked: a sheet lost on either pass is a sheet lost.

## How D-07's Count Is Surfaced — and Where It Is Not

The count travels in `ScanBatch.pages_rejected` and through **no second mechanism**, which was the point of choosing one object for both facts.

It reaches the user as a WARNING plus the `ScanResult.warning` field:

> `N page(s) could not be read by the scanner and were skipped. They were not removed for being blank; rescan those sheets.`

**It is not folded into `pages_removed`.** That field remains `len(images) - len(filtered)` — blank-page detection only — and now carries a comment saying so, because Phase 30's APPL-03 renders it to users as pages removed for being blank. Reporting an unreadable sheet through it would be a new small lie in the phase whose purpose is removing them. A test asserts the two move independently: two rejections with zero blank removals gives `pages_removed == 0` and a warning naming the 2.

This matters because `pages_scanned` is `len(images)`, which **already excludes** a skipped sheet — so before this, a ten-sheet stack with one corrupt page reported nine and nobody learned a page was lost (T-24-23).

`_join_warnings` exists so that a consume-directory fallback and an unreadable sheet — independent events that can both occur in one run — cannot overwrite each other in the single `warning` field.

**No schema change was made**, as the plan required: `grep -c 'ALTER TABLE\|user_version' src/saneless/job.py` is **6**, measured before the first edit and again after the last.

## RED States, Recorded Verbatim

**Task 1** — a collection error, the honest RED for a new type:

```
tests/test_scanner.py:21: in <module>
    from saneless.scanner.base import (
E   ImportError: cannot import name 'ScanBatch' from 'saneless.scanner.base'
```

The field-name assertion resolves `ScanBatch` at import, so its absence fails collection rather than an assertion.

**Task 3** — 4 failed, 1 passed:

```
FAILED TestTheDpiTheDeviceActuallyChose::test_the_pdf_is_assembled_at_the_resolution_the_device_chose
FAILED TestTheDpiTheDeviceActuallyChose::test_the_duplex_mismatch_recovery_also_uses_the_actual_dpi
FAILED TestTheDpiTheDeviceActuallyChose::test_two_passes_disagreeing_on_resolution_say_so
FAILED TestRejectedPagesAreNotBlankPages::test_rejected_pages_are_reported_without_touching_the_blank_count

assert result.warning is not None
E  AssertionError: assert None is not None
E   +  where None = ScanResult(outcome=SUCCESS, pages_scanned=3, pages_removed=0,
                               pages_uploaded=3, warning=None).warning
```

`test_a_clean_scan_reports_zero_for_both_counts` **passed in RED by design**: a clean run already reports zero for both counts and no warning, so it is the control proving the other four fail on behaviour rather than on a broken fixture. If it ever fails, the surfacing has begun inventing warnings out of nothing.

## Deviations from Plan

### 1. [Rule 3 — Blocking] Task 1 had to touch the three pipeline call sites

- **Plan:** task 3 owns "unpack the ScanBatch at all three pipeline call sites"; task 1's criterion is that `ty` and `pyrefly` exit 0 for `src/`.
- **Conflict:** the moment the ABC changed, `pyrefly` reported 3 errors in `src/saneless/pipeline.py` — `ScanBatch is not assignable to parameter iterable`. Task 1 could not be `src`-clean while leaving them.
- **Resolution:** task 1 made the **minimal** change — `list(scanner.scan_pages(...))` became `scanner.scan_pages(...).pages` — which is behaviour-identical and leaves task 3 its actual work of threading the two facts through to `assemble_pdf`. The two tasks stay independently reviewable.

### 2. [Acceptance-probe correction] The measured 89 is not reproducible as a line count

Recorded in full under "The Reference Count" above. The criterion asks for the totals to be "accounted for against PATTERNS.md's measured 89, with any discrepancy explained" — the accounting is that 89 is a category sum, a naive grep additionally over-counts by 14 lines of `_multi_scan_pages`, and the real verification is the three green suites plus two clean type checkers.

### 3. [Plan-text correction] The named duck-typed pair was half wrong

`test_web.py` subclasses the ABC and was flagged by the checkers; `test_cli.py:240`'s `FailScanner` raises and needed no change. The genuinely checker-invisible site was `test_cli.py:126`, which the plan did not name. Detailed above.

### 4. [Deliberate] `_scan_adf_pages`'s two call sites were fixed in task 1, not task 2

`tests/test_scanner.py:1329` and `:1342` call the helper directly. Its shape changed in task 1, so its callers changed with it rather than being left broken across a commit boundary. They are not `scan_pages` references and are outside task 2's sweep.

### 5. [Process] `ruff format` reflowed two files after the sweep

`conftest.py` and `test_outcomes_e2e.py` needed reformatting: `iter(` → `scan_batch(` is seven characters longer and pushed `test_outcomes_e2e.py:450` past the 88-column limit. Semantics-preserving; the full suite was re-run afterwards and reported the same 877 passed.

## The Self-Inflicted Probe Break Did Not Recur

Waves 24-04, 24-05 and 24-06 each broke an acceptance probe by quoting, in an explanatory comment, the exact string the probe counts — six occurrences across the phase. This plan carried the same hazard in an obvious place: the new comment above `assemble_pdf` exists specifically to explain that the profile's requested resolution is no longer authoritative, against a criterion of `grep -c 'dpi=profile.resolution'` = 0.

It was written from the start as "the one the profile asked for" rather than the literal. Final count: **0**. No documentation was deleted to satisfy a probe.

## Verification

| Check | Result |
|---|---|
| `uv run pytest -m "not browser and not sane_hardware" -q` | **882 passed**, 0 failed (baseline 870) |
| `uv run pytest -m sane_hardware -q` | **4 passed**, 0 failed (baseline 4) |
| `uv run pytest -m browser -q` | **30 passed**, 0 failed |
| `uv run ruff check .` | exit 0 |
| `uv run ruff format --check .` | exit 0, 43 files |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |

Acceptance probes: `ScanBatch` fields `['pages', 'actual_resolution', 'pages_rejected']` and `frozen=True`; `Yields:` in `base.py` **0**; `dpi=profile.resolution` in `pipeline.py` **0**; `list(.*scan_pages(` in `src/` and `tests/` **none**; `scan_pages.return_value = iter` / `side_effect = [iter` **none**; `Phase 24 adds device read-back` in `pdf.py` **none**; `HARD-01` in `base.py` **2**; `pages_removed` in `sane_backend.py` **0**; `ALTER TABLE|user_version` in `job.py` **6**, unchanged.

`pdf.py`'s diff is confined to the docstring — every `+`/`-` line falls between the summary line and `Returns:`, and no line of `assemble_pdf`'s body appears in it.

All five code commits landed with hooks enabled. No `--no-verify`, no `SKIP=`, no `# noqa`, no `# type: ignore`, no stub, no rule disabled. The two pre-existing `# noqa` in `_ensure_sane` were not touched. Both TDD gate sequences are present: `test(...)` then `feat(...)` for task 1, and again for task 3.

## Known Stubs

None. Every path added is covered: the batch's three fields, the eager close, the clamped-resolution case, both dpi call sites, the two-pass disagreement, and the rejection count moving independently of the blank count.

## Threat Model Dispositions Honoured

| Threat | Disposition | How |
|---|---|---|
| T-24-22 | mitigate | Both dpi sites read the device's value. Asserted directly on the `dpi` argument `assemble_pdf` received, and shown failing first |
| T-24-23 | mitigate | `pages_rejected` reaches the caller through `ScanBatch` and lands in `ScanResult.warning`; a lost page is no longer absorbed by `pages_scanned = len(images)` |
| T-24-24 | mitigate | The count is kept out of `pages_removed`, enforced by a test asserting 2 rejections with 0 blank removals |
| T-24-25 | accept | Eager acquisition was already the effective behaviour — all three call sites wrapped the generator in `list()`. Made explicit, not introduced. Still bounded by `_MAX_ADF_PAGES` |
| T-24-26 | mitigate | `frozen=True`, so a stale or hand-set DPI cannot be written onto a batch downstream |
| T-24-SC | accept | No package-manager install occurred in this plan |

## Threat Flags

None. This plan adds no network endpoint, no auth path, no file-access pattern and no schema change. It removes a silent geometry lie and makes a silent data-loss event visible.

## Notes for Downstream Plans

- **24-08** deletes `MockSaneDev` and `_FakeSaneDevice`. Both were left in place here and only their `scan_pages`-related call sites were touched, exactly as the plan instructed. `MockSaneDev._multi_scan_pages` is still the seam several rejection tests use.
- **Phase 25** inherits `_DuplexMismatch` and the pass-A resolution rule. If manual duplex ever legitimately scans its two passes at different resolutions, `_duplex_resolution` is the one place that decides, and it already logs.
- **Phase 29's HARD-01 is deliberately unblocked, not pre-empted.** `ScanBatch` has no per-page structure and its docstring names HARD-01 as the design it must not become.
- **Phase 30's APPL-03** can render `pages_removed` as blank-page removal truthfully; the rejection count is a separate sentence in the warning field and needs its own presentation.
- **If a job-store column for the rejection count is ever wanted**, it was consciously not added here: Phase 22's migration ladder is closed and this phase's requirements include no schema change.

## Self-Check: PASSED

- `src/saneless/scanner/base.py` — FOUND
- `src/saneless/scanner/sane_backend.py` — FOUND
- `src/saneless/scanner/__init__.py` — FOUND
- `src/saneless/pipeline.py` — FOUND
- `src/saneless/pdf.py` — FOUND
- `tests/conftest.py` — FOUND
- `tests/test_scanner.py` — FOUND
- `tests/test_pipeline.py` — FOUND
- `tests/test_outcomes_e2e.py` — FOUND
- `tests/test_cli.py` — FOUND
- `tests/test_web.py` — FOUND
- `tests/test_web_state_rendering.py` — FOUND
- `tests/test_browser.py` — FOUND
- `tests/test_sane_hardware.py` — FOUND
- Commit `15d8378` — FOUND
- Commit `df1bf78` — FOUND
- Commit `7d00552` — FOUND
- Commit `57c6d7a` — FOUND
- Commit `56cc57c` — FOUND
