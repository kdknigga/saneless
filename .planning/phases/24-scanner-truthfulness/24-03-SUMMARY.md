---
phase: 24-scanner-truthfulness
plan: "03"
subsystem: scanner
tags: [scanner, sane, error-handling, dos, security, tdd, refactor]
requires:
  - "24-01 — tests/fake_sane.py, the faithful python-sane 2.9.2 double"
provides:
  - "_next_page_with_timeout — the per-page executor/future/timeout block, extracted"
  - "_acquire_pages — the ADF loop, exception ladder, cap and cleanup, extracted"
  - "_MAX_ADF_PAGES = 500 — the per-pass bound on the ADF iteration loop"
  - "ScanError carrying the real SANE text for every non-StopIteration fault"
  - "corrected W-01 accept rationale in 21-SECURITY.md"
affects:
  - "24-04 — owns turning the sane_hardware ten-page test green; it is still RED here, deliberately"
  - "24-05 — inherits scan_pages at 11/12 branches; must split before adding"
  - "24-05 — still owns the SaneDevice.resolution int/float Protocol lie, untouched here"
  - "28 — EXC-01/EXC-03 wording and boundary translation build on these now-truthful messages"
  - "29 — HARD-01/HARD-02 own the memory bound this cap deliberately does not provide"
tech-stack:
  added: []
  patterns:
    - "Split a function to create branch headroom BEFORE adding to it, as its own behaviour-neutral commit"
    - "Recognise the one signal the library actually gives; never infer meaning from 'it failed on iteration zero'"
    - "Reach a not-yet-existing constant through the module, so RED is a test failure and not a collection error"
key-files:
  created:
    - .planning/phases/24-scanner-truthfulness/24-03-SUMMARY.md
  modified:
    - src/saneless/scanner/sane_backend.py
    - tests/test_scanner.py
    - .planning/phases/21-vocabulary-and-contracts/21-SECURITY.md
decisions:
  - "The cap fires on the page PAST the cap, not at it -- a legitimate maximal stack only learns it is finished when the next probe raises"
  - "A third helper (_feed_ended) was extracted in Task 1 for branch headroom, then deleted in Task 2 when D-03 removed its entire reason to exist"
  - "New tests drive scan_pages through the monkeypatched module seam, never passing FakeSaneDev to _acquire_pages directly, which would surface 24-05's Protocol mismatch"
  - "Three of the plan's grep probes were arithmetically unachievable as written; the intent of each was verified with a corrected probe and recorded"
metrics:
  tasks: 3
  commits: 5
  tests_added: 10
  tests_deleted: 1
  suite: "823 passed, 33 deselected (baseline was 814 passed, 33 deselected)"
  commit_span: "11m35s (10:06:05 -> 10:17:40)"
completed: 2026-09-13
---

# Phase 24 Plan 03: Truthful ADF Errors and a Bounded Feeder Loop Summary

A jam, an open cover, a busy device and an I/O error each now surface as `ScanError` carrying the scanner's own words instead of telling the operator to load paper, and the ADF loop that Phase 21 wrongly recorded as bounded is now actually bounded — by `_MAX_ADF_PAGES = 500`, which documents honestly that it bounds iteration and not memory.

## What Was Built

### Task 1 — the split, as its own behaviour-neutral commit

Commit `2a4b209`. **No test file was edited in this commit** (`git show --stat` is one file), which is what makes it provable that behaviour did not change.

`_scan_adf_pages` sat at exactly **12 of ruff's `PLR0912` limit of 12**. Since CLAUDE.md forbids both raising the limit and suppressing the rule, tasks 2 and 3 could not have added a single branch to it. Three module-level private functions were extracted, following the `_set_geometry`/`_maybe_crop` precedent Phase 18 established in this same file:

| Helper | Carries |
|---|---|
| `_next_page_with_timeout` | The executor/future/timeout block, moved verbatim |
| `_acquire_pages` | The loop, the exception ladder, validate-and-skip, and the `finally` |
| `_feed_ended` | The post-first-page end-of-feed decision (deleted again in Task 2) |

`_scan_adf_pages` survives as a thin delegator because two existing tests call it directly. It changed from a generator function to a plain function returning the generator; the two are indistinguishable to every caller, and its docstring's `Yields:` became `Returns:` to match.

The feeder-empty message was hoisted to a module constant `_FEEDER_EMPTY_MESSAGE`, which also keeps ruff's `EM` rules satisfied at the raise sites.

**Measured result: `_acquire_pages` at 9 branches**, versus the criterion's ceiling of 9. `uv run ruff check --config 'lint.pylint.max-branches=9'` reported nothing for it.

### Task 2 — `StopIteration` is the only feeder-empty signal (D-03)

RED `f160519`, GREEN `d351739`.

Deleted, per M-11:

- the `try`/`except` around `dev.multi_scan()`. The real method is a one-line `return _SaneIterator(self)` and **cannot raise**, so the guard was unreachable — and it was one of the two places mislabelling a real fault as an empty feeder.
- the `if page_num == 0: raise FeederEmptyError(...)` special case and the `"out of documents"`/`"no docs"` substring fallback, along with `_feed_ended`, the helper that had just been created to hold them.

The ladder is now three clauses: `StopIteration` breaks; `ScanError` propagates untouched; anything else becomes `ScanError(f"Scanner error on page {n}: {exc}")` raised `from exc`, one-based to match the timeout path's existing convention.

**`except FeederEmptyError` is gone and is not needed.** `FeederEmptyError` subclasses `ScanError`, so the earlier `except ScanError` clause already catches it — the separate clause in the original code was unreachable. Clause order is load-bearing and is now commented as such.

The zero-page `FeederEmptyError` survives as the **only** path to "No paper detected in feeder", and the docstring says so explicitly, with the reason, so that nobody re-adds the special case.

### Task 3 — `_MAX_ADF_PAGES` and the W-01 correction (D-04)

RED `47039c7`, GREEN `3a0d82c`.

`_MAX_ADF_PAGES: int = 500` sits beside `_MIN_PAGE_BYTES`, with a comment carrying all three things D-04 requires:

1. **What it bounds** — python-sane stops only on one exact string, so on non-feeder hardware `start()`/`snap()` keep succeeding and the loop never terminates on its own. The per-page timeout is no defence: a scan that succeeds satisfies it every iteration.
2. **What it does not bound** — memory. At A4/300 dpi colour a page is ~26 MB, so 500 pages is ~13 GB, and `pipeline.py` still materialises with `list()`. The comment says in terms that this **must not be described as a memory bound**. That is Phase 29's HARD-01/HARD-02.
3. **Its scope** — per `scan_pages()` call, therefore **per-pass, not per-job**, because Phase 25's two manual-duplex passes call `scan_pages` separately.

The value is assumption A1 from RESEARCH, recorded at LOW confidence and noted as cheap to revise precisely because the error names the cap.

**The off-by-one that matters.** The overrun is detected on the page *past* the cap, not at it. A legitimate 500-sheet stack only discovers it is finished when the next probe raises end-of-feed, so a check at `page_num == cap`, or at the top of the loop, would reject a full hopper. `test_a_maximal_stack_is_not_off_by_one` is the discriminating test: it drives exactly 500 sheets and asserts all 500 come back.

## The W-01 Rationale — the Exact Text It Now Carries

Corrected in all four places the withdrawn claim had consequences:

| Location | Now reads |
|---|---|
| Register row 5 (`:42`) | "Unbounded on non-feeder hardware; **bounded from Phase 24 by `_MAX_ADF_PAGES`** (`sane_backend.py`, plan 24-03). The risk itself remains accepted by explicit user decision (D-11 AMENDED, `e6de95e`); the register's original one-page bound was false as written and is withdrawn" |
| Required action (`:121-123`) | "**Required action — DISCHARGED 2026-09-13 by Phase 24, plan 24-03**", with both bullets struck through and answered, plus a new "**What the cap does not do**" paragraph restating the memory and per-pass caveats |
| Accepted Risks Log (`:155`) | "No longer documentation-only: **unbounded on non-feeder hardware; bounded from Phase 24 by `_MAX_ADF_PAGES`** (plan 24-03) … Residual: the cap bounds iteration, not memory", with the revisit owner moved to **Phase 29 (HARD-01/HARD-02)** |
| Evidence (`:82`) | Reworded to "Neither a single page nor any page at all is guaranteed" — same meaning, without the quoted phrase |

The W-01 finding and its evidence are **not** deleted. `grep -c 'yields one page'` is now **1**, and it is line 63 — the verbatim quotation of the original register text at the top of the finding, kept deliberately because the history of the false rationale is the point.

## Complexity Budget After This Plan — for 24-05

Measured with `uv run ruff check --config lint.pylint.max-branches=1`:

| Function | Branches | Limit | Headroom |
|---|---|---|---|
| `_acquire_pages` | **8** | 12 | 4 |
| `_next_page_with_timeout` | ≤1 | 12 | plenty |
| `scan_pages` | **11** | 12 | **1** |
| `get_capabilities` | 5 | 12 | 7 |
| `_validate_page_image` | 4 | 12 | 8 |

**`scan_pages` is untouched by this plan and still sits at 11 of 12.** Plan 24-05 must split it before adding D-11's read-back, D-09's presence check or D-12's assembly — the same trap this plan hit, one function over.

## Deviations from Plan

### 1. [Rule 3 — Blocking] A third helper was needed to meet the branch-headroom criterion

- **Found during:** Task 1.
- **Issue:** The plan names exactly two helpers. Task 1's criterion requires `_acquire_pages` to pass at `max-branches=9`, but the two-helper split leaves the loop owning four `except` clauses plus both `if` tests from the first-page ladder.
- **Fix:** Extracted a third module-level helper, `_feed_ended`, carrying the page-0 test and the substring fallback. Measured 9 branches — at the ceiling, passing.
- **Honest caveat:** I did not measure the two-helper variant in isolation, so I cannot claim a number for it; I designed to the criterion rather than discovering the shortfall empirically.
- **Resolution:** Task 2 deleted `_feed_ended` outright, because D-03 removes its entire reason to exist. It lived for exactly one commit, which is the correct lifetime for scaffolding that exists to keep an intermediate commit green.

### 2. [Rule 1 — Plan self-conflict] `grep -c 'yields one page'` is 1 vs. "do not delete the evidence"

- **Found during:** Task 3.
- **Issue:** The phrase occurred **three** times, not two: the historical quotation (`:63`), a sentence inside W-01's **Evidence** (`:82`), and the Required-action bullet (`:122`). The criterion demands exactly 1, but the action text forbids deleting the finding's evidence.
- **Fix:** Rewrote `:82` as "Neither a single page nor any page at all is guaranteed" — the evidentiary point is preserved in full, only the quoted phrase is dropped, and the withdrawn wording is still quoted verbatim 19 lines above. Criterion and instruction both satisfied.

### 3. [Acceptance-probe corrections] Three grep counts were arithmetically unachievable as written

Each probe's **intent** was verified with a corrected probe; none of the underlying requirements was weakened.

| Plan probe | Stated | Actual | Why | Corrected probe |
|---|---|---|---|---|
| `grep -c 'multi_scan()'` | 1 | 5 | Four are docstrings and comments legitimately naming the method | `grep -c 'dev.multi_scan()'` = **1**, and no `try` wraps it |
| `grep -c 'FeederEmptyError'` | 2 | 6 | Four are `Raises:` docstring lines, which ruff's `D` rules want | `grep -c 'raise FeederEmptyError'` = **1** |
| `grep -c 'out of documents\|no docs'` | 0 | 0 then 1 | Held at **0** on Task 2's commit; Task 3's *mandated* constant comment reintroduces the literal in a comment | Both honoured in sequence; no behavioural string-sniffing remains (`error_str` = 0) |

### 4. [Behaviour-change test updates] Two existing tests encoded the deleted behaviour

- **`test_empty_feeder_out_of_documents_error` — deleted.** It drove `multi_scan()` itself into raising and asserted `FeederEmptyError`, pinning the behaviour of provably unreachable code. A comment stands in its place recording why. Zero-page coverage is unaffected: `test_empty_feeder_stop_iteration` and the new `test_out_of_documents_still_means_an_empty_feeder` both cover it.
- **`test_cancel_called_on_adf_error` — updated.** A mid-stack `RuntimeError("hardware error")` is now translated to `ScanError`; the test expects that, and its cancel/close assertions are untouched.

## SCNR-08's Ten-Page Test Is Still RED — Deliberately

**This is the headline check for this plan, and it passes.**

```
uv run pytest -m sane_hardware -q
2 passed, 1 failed
assert len(pages) == 10
E   assert 0 == 10
```

`_validate_page_image` was **not touched**, and the pure-black check was **not removed** — that is plan 24-04's work (D-05). The test fails exactly as 24-01 recorded it, with the same `assert 0 == 10` and the same ten "pure black … skipping" warnings. It remains the evidence that 24-04 changed behaviour rather than changing the test.

## Verification

| Check | Result |
|---|---|
| `uv run pytest -m "not browser and not sane_hardware" -q` | **823 passed**, 0 failed (baseline 814) |
| `uv run pytest tests/test_scanner.py -q` | **117 passed**, 0 failed (108 baseline, minus 1 deleted, plus 10 added) |
| `uv run pytest -m sane_hardware -q` | **2 passed, 1 failed** — unchanged, the documented RED |
| `uv run ruff check .` | exit 0 |
| `uv run ruff format --check .` | exit 0, 43 files |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |

Acceptance probes: `raise FeederEmptyError` = 1; `page_num == 0` = 1; `error_str` = 0; `dev.multi_scan()` = 1; `_MAX_ADF_PAGES` in backend = 4; non-comment `_MAX_ADF_PAGES: int = 500` = 1; `per-pass` in the comment = 1; `yields one page` in 21-SECURITY.md = 1; `_MAX_ADF_PAGES` in 21-SECURITY.md = 4.

All five commits landed with hooks enabled. No `--no-verify`, no `SKIP=`, no `# noqa`, no `# type: ignore`, no stub. The two `# noqa` in `sane_backend.py` (`:48`, `:50`) are pre-existing in `_ensure_sane` and were not touched.

## Known Stubs

None.

## Threat Model Dispositions Honoured

| Threat | Disposition | How |
|---|---|---|
| T-24-08 (**W-01**) | mitigate | `_MAX_ADF_PAGES = 500` bounds the loop in `_acquire_pages`; Phase 21's accepted risk 21-02/T-21-04 is discharged and its rationale corrected in the same commit |
| T-24-09 | accept (partial) | Recorded honestly in the constant's comment, in the security register, and here: **the cap bounds iteration, not bytes.** Deferred to Phase 29 HARD-01/HARD-02, not folded in |
| T-24-10 | mitigate | Four measured SANE faults surface as `ScanError` with their real text, asserted by a parametrised test over all four strings plus a `__cause__` assertion |
| T-24-SC | accept | No package-manager install occurred in this plan |

## Threat Flags

None. This plan adds no network endpoint, no auth path, no file-access pattern and no schema change. It **removes** a denial-of-service surface and makes an existing error path more truthful.

## Notes for Downstream Plans

- **24-04** must turn the `sane_hardware` ten-page test green by removing the pure-black check, and owns the CI marker step. Nothing here touched `_validate_page_image`.
- **24-05 inherits `scan_pages` at 11 of 12 branches.** Split it first. This plan hit exactly that wall one function over and the split had to be its own commit.
- **Finding 9's Protocol lie is still live and still unforced.** `SaneDevice.resolution` is declared `int` at `sane_backend.py` while the real device and the shared fake both return `float`. The new tests deliberately drive `scan_pages` through the monkeypatched module seam — where `sane` is typed `Any` — rather than passing `FakeSaneDev` straight into `_acquire_pages`, which would have forced the structural check and surfaced the mismatch that 24-05 owns. Fix the Protocol there; do not annotate around it.
- **Phase 28** inherits messages that are now *truthful* but not yet *well-typed*: `f"Scanner error on page {n}: {exc}"` still interpolates raw SANE text. M-17/EXC-01 boundary translation and N-06/EXC-03 wording are unchanged here by design.

## Self-Check: PASSED

- `src/saneless/scanner/sane_backend.py` — FOUND
- `tests/test_scanner.py` — FOUND
- `.planning/phases/21-vocabulary-and-contracts/21-SECURITY.md` — FOUND
- `.planning/phases/24-scanner-truthfulness/24-03-SUMMARY.md` — FOUND
- Commit `2a4b209` — FOUND
- Commit `f160519` — FOUND
- Commit `d351739` — FOUND
- Commit `47039c7` — FOUND
- Commit `3a0d82c` — FOUND
