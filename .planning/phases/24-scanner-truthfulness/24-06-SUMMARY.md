---
phase: 24-scanner-truthfulness
plan: "06"
subsystem: scanner
tags: [scanner, sane, geometry, units, enum, crop-fallback, tdd]
requires:
  - "24-01 — tests/fake_sane.py, the shared python-sane 2.9.2 double"
  - "24-05 — _configure_device's read-back resolution, previously unbound"
provides:
  - "_set_geometry presence-checked against the device's own option list (D-09)"
  - "GeometryUnit — a total enum over all seven SANE unit codes, behind match + assert_never (D-10)"
  - "_units_per_mm — one scale factor serving both conversion and tolerance"
  - "_area_matches — clamped scan area detected on read-back with a tolerance (D-19)"
  - "SaneDevice.area — declared read-only, as python-sane actually exposes it"
  - "_maybe_crop routed to the read-back resolution rather than the requested one"
  - "build_option_table() and FakeSaneDev.set_page_size() in the shared fake"
  - "TYPE_FIXED 16.16 rounding in the fake, which makes the tolerance testable"
affects:
  - "24-07 — owns ScanBatch; rejected_pages deliberately left local here"
  - "24-08 — MockSaneDev and _FakeSaneDevice each gained an area property; both are still slated for deletion"
tech-stack:
  added: []
  patterns:
    - "A presence check, not an exception handler, is what detects a missing option -- the library stores unknown names silently"
    - "One scale factor serves both the conversion and its tolerance, so a unit change cannot desynchronise them"
    - "Compare device read-backs with a tolerance, never equality, when the option is TYPE_FIXED"
    - "Extract a helper to stay inside a lint budget rather than suppressing the rule"
key-files:
  created:
    - .planning/phases/24-scanner-truthfulness/24-06-SUMMARY.md
  modified:
    - src/saneless/scanner/sane_backend.py
    - tests/fake_sane.py
    - tests/test_scanner.py
    - docs/how-to/configure-scan-profiles.md
decisions:
  - "Area tolerance is 1.0 mm, scaled into the device's own units by the same _units_per_mm factor used for the conversion"
  - "The fake now rounds every TYPE_FIXED value to SANE's 16.16 grid -- derived from the SANE specification, not measured, and labelled as such"
  - "_geometry_scale extracted to stay inside ruff's PLR0911 six-return budget without suppressing it"
  - "The redundant `paper_size == \"full\"` check folded into the PAPER_SIZES_MM lookup, where \"full\" is already deliberately absent"
  - "GeometryUnit is an IntEnum -- the project's first integer-valued member set, because SANE unit codes are integers"
metrics:
  tasks: 3
  commits: 6
  tests_added: 25
  suite: "870 passed (baseline was 845 passed)"
  hardware_suite: "4 passed (baseline was 4 passed)"
completed: 2026-09-13
---

# Phase 24 Plan 06: Presence-Checked Geometry, Real Units, and the Clamp Nobody Saw Summary

The geometry path now tells the truth three times over: it writes a scan area only on a device that says it has the options, scales it by the unit the device reports instead of assuming millimetres, and notices when the device quietly shrank the area it accepted.

## What Was Built

### Task 1 — the crop fallback becomes reachable (D-09)

RED `27404ad`, GREEN `89d471a`.

`_set_geometry` now reads the device's option list and confirms all four of `tl-x`, `tl-y`, `br-x` and `br-y` are present **before** assigning anything.

**An exception handler could never have caught this, and that is the whole finding.** The real `SaneDev.__setattr__` stores an unrecognised option name straight into `__dict__` and returns — no device call, no validation, no raise (`sane.py:188`, read directly). So `dev.br_y = 297.0` *succeeds* on a scanner with no scan-area options, `_set_geometry` returned `True`, `_maybe_crop` never ran, and `paper_size` was silently ignored. That is M-15.

The old `_NoGeometryDevice` double **raised** on exactly the assignment the real library stores — the precise inverse of the contract — which is why M-15 shipped with a green test guarding an unreachable fallback. It is deleted, and the tests that used it now drive a `FakeSaneDev` whose option table simply omits the geometry options.

Two spellings are kept deliberately distinct, because a single set used for both sides would be wrong on one of them:

| Purpose | Spelling | Where |
|---|---|---|
| Presence lookup | **hyphenated** `tl-x` | `_REPORTED_GEOMETRY_OPTIONS`, matched against `get_options()` |
| Assignment | **underscore** `dev.tl_x` | the attribute writes themselves |

The bare `except Exception` now names what it swallowed. A device that reports the options and then refuses them is a different fault from one that never had them, and "does not support geometry options" hid which had happened.

### Task 2 — GeometryUnit, a total enum over all seven codes (D-10, N-03)

RED `984372a`, GREEN `8ac567e`.

The scale factor comes from **index 5** of the option tuple, which the arithmetic previously ignored entirely. A device reporting `UNIT_PIXEL` was handed A4 as 210 *pixels* — about 18 mm at 300 dpi — and returned a sliver of the page with no error.

`GeometryUnit` is an `IntEnum` carrying all seven SANE codes, dispatched by `match` + `assert_never`:

| Unit | Behaviour |
|---|---|
| `UNIT_MM` | millimetres written directly |
| `UNIT_PIXEL` | converted at the **read-back** resolution |
| `UNIT_NONE`, `UNIT_BIT`, `UNIT_DPI`, `UNIT_PERCENT`, `UNIT_MICROSECOND` | WARNING naming the unit, fall through to the crop |

**There is no centimetre or inch member.** SANE defines no such code; a dead branch for a unit no device can send would misrepresent what was measured. The enum's docstring carries Phase 21 D-08's reasoning verbatim in substance: a mapping missing a member draws no diagnostic from either `ty` or `pyrefly`, while the same enum in a `match` is caught by both, at edit time.

**The read-back resolution is now bound at the call site.** Plan 24-05 left `_configure_device`'s return value deliberately unbound because binding an unused local trips `F841`. This plan is its first genuine consumer, and a test proves the consumption is real: a 5000 dpi request clamped to 1200 produces pixel geometry matching **1200**, not 5000.

### Task 3 — the clamp nobody could see (D-19)

RED `b58fef4`, GREEN `71e5847`.

After assigning the four values, `dev.area` is read back and compared with what was requested. A device can accept all four assignments and still quietly shrink the area: writing A4's 210 mm to a device whose `br-x` range is `(0.0, 200.0, 1.0)` stores 200.0, with no error and no `INFO_INEXACT` the caller can see. The area is now logged with **both** values and returns `False`, so the crop produces a correctly sized page anyway.

`area` joins the `SaneDevice` Protocol as a **read-only property**, which is what python-sane actually exposes — `__setattr__` rejects the name outright. Declaring it a property makes that true for the type checkers too, rather than only at runtime.

`_maybe_crop` now takes the **read-back** resolution instead of `settings`. The crop arithmetic and the device's real sampling rate have to agree, or a clamped dpi yields M-16's cut-off page even when the fallback runs exactly as intended. A test pins this: a device whose feeder ceiling is 75 dpi, asked for 300, produces a page cropped at **75** dpi (620x876), not at the 300 requested (which would have been 2480x3507).

## The Tolerance, and Why It Is Not an Equality Test

**`_AREA_TOLERANCE_MM = 1.0`**, converted into the device's own units by the *same* `_units_per_mm` factor used for the conversion — so a `UNIT_PIXEL` device gets `resolution / 25.4` units of tolerance automatically, and a unit change cannot desynchronise the two.

The reasoning, recorded in the constant's own comment:

- SANE geometry options are `TYPE_FIXED`, a **16.16 fixed-point integer**, so a length that is not a multiple of 1/65536 cannot be represented exactly and reads back differing in the low bits **although the device clamped nothing at all**. Letter's 215.9 mm is exactly such a length. An equality test would send every letter-sized scan down the crop path for no reason.
- One millimetre is far below anything a user could notice on a paper size, while the clamping this must catch is measured in **tens** of millimetres (210 → 200, measured). Three orders of magnitude separate the two, so the exact figure is not delicate.

This is asserted, not asserted-about: `test_a_fixed_point_round_trip_is_within_tolerance` checks `dev.br_x != 215.9` **and** `== approx(215.9)`, so the test fails if the round trip ever becomes exact and the tolerance stops being load-bearing.

## Additions to the Shared Fake

`FakeSaneDev.__init__` is at ruff's five-argument `PLR0913` ceiling, which wave 24-05 established empirically. New configuration therefore goes through a module function and a method, following that precedent exactly:

| Addition | Kind | Purpose |
|---|---|---|
| `build_option_table(geometry_range=, geometry_unit=, omit=, geometry_settable=)` | public module function | `omit` reproduces a device with no scan-area options (D-09); `geometry_unit` makes a device report something other than millimetres (D-10); `geometry_settable=False` reports the options but refuses them, so the swallowed exception can be asserted |
| `FakeSaneDev.set_page_size(width, height)` | method | The default 200x300 page is smaller than any paper size at a realistic dpi, so `crop_to_paper_size` clamps to the image and changes nothing. Proving the crop *ran* needs a page larger than the crop box |
| `_to_sane_fixed()` applied to every `TYPE_FIXED` value | behaviour | SANE's 16.16 grid, which is what makes the tolerance testable at all |

**The fixed-point rounding is derived from the SANE specification, not from an execution, and says so** in both the module docstring and the function's own docstring. RESEARCH measured the `(0.0, 200.0, 1.0)` clamp returning exactly 200.0, which is consistent with it (200 is exactly representable) but does not on its own demonstrate it. It is labelled as specification-derived so a later reader does not mistake it for a measurement — the same discipline 24-05 applied to `narrow_resolution_for_source`.

It changes shared infrastructure, so it was checked for regression rather than assumed safe: every existing `TYPE_FIXED` value in the suite (resolutions 0/150/300/1000/1200/5000, geometry 200/210/297) is exactly representable, and the suite went 865 → 868 passed + 2 intended failures with no collateral damage.

## `test:0`'s 200 mm Range Changed No Hardware Test — Checked, Not Assumed

The plan flagged that `test:0`'s own `(0.0, 200.0, 1.0)` geometry range would now send a paper-size scan down the crop path, and asked whether that changed any `sane_hardware` test.

**It changed none.** `tests/test_sane_hardware.py` contains **zero** occurrences of `paper_size` or `geometry` — no hardware test requests a paper size, so none reaches `_set_geometry`'s clamp detection at all. The hardware suite is **4 passed**, identical to the baseline.

Worth recording for a future plan: a hardware test that *did* request A4 on `test:0` would now correctly take the crop path and log the clamp. That is the right outcome, and it is currently unexercised against real hardware.

## Deviations from Plan

### 1. [Rule 3 — Blocking] PLR0911: seven returns where ruff allows six

- **Found during:** Task 2 verification. `ruff check` reported `PLR0911 Too many return statements (7 > 6)` in `_set_geometry`, and task 3 was about to add an eighth.
- **Fix, without suppression:** extracted `_geometry_scale`, which collapses the three ways a device can rule geometry out — an option it does not report, a code that is not a SANE unit, and a unit that is not a length — into one decision. Each cause still logs its own WARNING naming what was wrong, so the three stay distinguishable in the log despite sharing a return value.
- **Second reduction:** the explicit `paper_size == "full"` check was redundant. `"full"` means *no constraint* and is deliberately absent from `PAPER_SIZES_MM` (documented in `paper_sizes.py`), so a single `.get()` answers both "is a constraint wanted?" and "is it a size we know?". Behaviour is identical for `"full"` and for an unknown size; the comment records why.
- **Final state:** `_set_geometry` is 5 returns and 6 of 12 branches.

### 2. [Rule 1 — self-inflicted, twice] My own comments broke two probes

This is the fifth and sixth occurrence of this pattern in the phase, and both were caught by running the probes rather than by assuming.

- **`_NoGeometryDevice`:** the criterion is 0 in `tests/test_scanner.py`. After deleting the class I had written "the condition ``_NoGeometryDevice`` claimed to model" into a new docstring; two further pre-existing mentions survived from wave 1. Count read **3**.
- **`dict[GeometryUnit`:** the criterion is 0 in the backend. `GeometryUnit`'s own docstring explained why the dispatch is *not* a `dict[GeometryUnit, ...]` — quoting the exact string the probe counts. Count read **1**. The same docstring quoted `UNIT_CM` and `UNIT_INCH` verbatim against a criterion of 0.
- **Resolution — reworded, never deleted.** Every mention now describes the behaviour instead of naming the dead symbol ("the geometry-less double", "a mapping keyed on the enum", "a backend cannot report centimetres or inches"). The reasoning is fully preserved; the literals are gone. This was the right fix rather than a probe-satisfying hack, because naming a class that no longer exists is a genuine dangling reference. All three counts are now **0**, across every `.py` file in `tests/`, not just the one the criterion names.

### 3. [Acceptance-probe correction] `settings.resolution` is not 0 file-wide, and should not be

- **Plan probe:** "`grep -c 'settings.resolution'` is 0 **inside `_maybe_crop`'s call path**".
- **Measured:** 3 occurrences remain, all at lines 749-755 inside `_configure_device`, where the **requested** value legitimately belongs — it is the thing being compared against the read-back to detect a substitution.
- **Intent verified:** `_maybe_crop` and both its call sites now carry `actual_resolution`. The crop path contains no reference to the requested resolution.

### 4. [Process] Task 2's RED was measured before GREEN was written, but committed after

I wrote the task-2 tests, measured the RED, and then wrote the implementation before committing the RED. Recovered by staging **only** the two test files for `984372a`, leaving the source unstaged.

The commit is faithful: its parent `89d471a` has no `GeometryUnit`, so checking that tree out genuinely fails. This was confirmed independently rather than argued — prek stashed the unstaged source, ran `ty check src` and `pyrefly check src` against the **staged-only tree**, and both passed, proving the committed RED state is self-consistent without the enum. Tasks 1 and 3 followed the ordinary order.

### 5. [Deliberate, in lines already being touched] The stale `D-01` comment is retired

`scan_pages` carried `# Set scan area geometry for paper size constraint (D-01)`, where `D-01` is **Phase 18's** decision id, not this phase's D-01, which means something entirely different in the same subsystem. Wave 24-05 flagged it for a future planner rather than touching it. The line was being rewritten anyway, so it now names the decisions that actually govern it (D-09, D-10, D-11).

## RED States, Recorded Verbatim

So a later reader can confirm each gate genuinely failed first.

**Task 1** — 5 failures, and one deliberate pass:

```
TestPaperSizeCropFallback.test_crop_fallback_when_geometry_fails   assert (3000, 4000) == (2480, 3507)
TestPaperSizeCropFallback.test_adf_crop_fallback                   assert (3000, 4000) == (2480, 3507)
TestGeometryPresenceCheck.test_the_crop_fallback_produces_a_correctly_sized_page
TestGeometryPresenceCheck.test_the_missing_option_is_named_in_the_warning
TestGeometryPresenceCheck.test_a_rejected_geometry_assignment_logs_the_exception
```

`test_a_device_without_geometry_options_stores_br_y_silently` **passed** in RED, by design: it asserts the *premise* that the fake stores an unknown option rather than raising. If it ever fails, the fake has drifted back to modelling a library that does not exist and everything above it proves nothing.

**Task 2** — a collection error, which is the honest RED for a new type:

```
ImportError: cannot import name 'GeometryUnit' from 'saneless.scanner.sane_backend'
```

The `parametrize` decorator resolves `list(GeometryUnit)` at collection time, so the absent type fails the module import rather than an assertion. The alternative — parametrising over a literal list of codes — would have produced a tidier RED while violating the criterion that the parameter source be the enum itself.

**Task 3** — 2 failures:

```
test_a_clamped_area_falls_through_to_the_crop      assert []            (no clamp warning exists)
test_the_crop_uses_the_resolution_the_device_chose assert (2480, 3507) == (620, 876)
```

The second is the whole of the read-back-resolution change: the crop box was being computed at the dpi that was *asked for* rather than the one the device settled on.

## Verification

| Check | Result |
|---|---|
| `uv run pytest -m "not browser and not sane_hardware" -q` | **870 passed**, 0 failed (baseline 845) |
| `uv run pytest -m sane_hardware -q` | **4 passed**, 0 failed (baseline 4 passed) |
| `uv run ruff check .` | exit 0 |
| `uv run ruff format --check .` | exit 0 |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |
| `uv run prek run --stage pre-push --all-files` | **every hook Passed**, including both full type checkers |

Acceptance probes: `_NoGeometryDevice` in `tests/` = **0** (every `.py` file; the only hits anywhere were stale `__pycache__` bytecode compiled from pre-edit source); `"tl-x"` in backend = 2; `dev.tl_x` = 3; `sorted(u.value for u in GeometryUnit)` = `[0, 1, 2, 3, 4, 5, 6]`; `UNIT_CM|UNIT_INCH` = **0**; `assert_never` = 3; `dict[GeometryUnit` = **0**; `def area` = 1; `_AREA_TOLERANCE_MM` = 2; verbatim `"Otherwise, it crops the image after scanning."` in the how-to = **0**.

Branch budget after this plan, for 24-07: `_set_geometry` **6**, `_acquire_pages` 9 (pre-existing, untouched), `scan_pages` 4, `_resolve_source` 6, `get_capabilities` 5, `_units_per_mm` 4, everything else ≤ 4. Limit is 12.

All six commits landed with hooks enabled. No `--no-verify`, no `SKIP=`, no `# noqa`, no `# type: ignore`, no stub, no rule disabled. Both TDD gate sequences are present for all three tasks: `test(...)` then `feat(...)`, six commits alternating. No file was deleted by any commit.

## What Was Deliberately Not Built

- **`ScanBatch` was not created** — `/usr/bin/grep -rl 'ScanBatch' src/` returns nothing. It is 24-07's.
- **`rejected_pages` is still local** to `_acquire_pages` — it appears in no source file but `sane_backend.py` (verified with `/usr/bin/grep` rather than the shell's ugrep wrapper, whose exit status wave 20-02 recorded as unreliable).
- **No cm or inch branch**, per the interface contract: SANE defines no such unit code.
- **The actual geometry is not carried out in D-12's result object**, which the plan explicitly rejected. D-19 reuses the `return False` path this phase was already building and adds no new vocabulary.

## Known Stubs

None. Every code path added is reached by at least one test, including all five unconvertible-unit arms and the out-of-range unit code.

## Threat Model Dispositions Honoured

| Threat | Disposition | How |
|---|---|---|
| T-24-18 | mitigate | D-19's `dev.area` read-back with a 1 mm tolerance scaled into device units. The measured 210 → 200 clamp is now logged with both values and compensated by the crop |
| T-24-19 | mitigate | `GeometryUnit` construction is defensive: a code outside the seven is logged and treated as unconvertible rather than crashing the scan or scaling by a garbage factor. Asserted with code 99 |
| T-24-20 | mitigate | D-09's presence check. Absence was previously undetectable because the real `__setattr__` stores silently, so the failure was an uncropped full-bed scan reaching `assemble_pdf` |
| T-24-21 | mitigate | `match` + `assert_never`, plus a test parametrised over `list(GeometryUnit)` itself. A new member with no arm fails the suite at runtime and both checkers at edit time |
| T-24-SC | accept | No package-manager install occurred in this plan |

## Threat Flags

None. This plan adds no network endpoint, no auth path, no file-access pattern and no schema change. It removes two silent-substitution failures and makes a third visible.

## Notes for Downstream Plans

- **24-07** owns `ScanBatch`. The read-back resolution is now bound as `actual_resolution` in `scan_pages` and has two consumers (`_set_geometry` and `_maybe_crop`), so carrying it out of the backend no longer requires reviving an unused value.
- **24-08** deletes `MockSaneDev` and `_FakeSaneDevice`. Both gained an `area` property here because `_set_geometry` reads it back at runtime; that is one more reason they are redundant with `FakeSaneDev`.
- **The two geometry spellings must stay separate.** `_REPORTED_GEOMETRY_OPTIONS` is hyphenated and is for the option-list lookup only; assignment uses `dev.tl_x`. Deriving one from the other will be silently wrong on one side.
- **A hardware test requesting A4 on `test:0` would now take the crop path** and log the clamp. That is correct, and currently unexercised.

## Self-Check: PASSED

- `src/saneless/scanner/sane_backend.py` — FOUND
- `tests/fake_sane.py` — FOUND
- `tests/test_scanner.py` — FOUND
- `docs/how-to/configure-scan-profiles.md` — FOUND
- `.planning/phases/24-scanner-truthfulness/24-06-SUMMARY.md` — FOUND
- Commit `27404ad` — FOUND
- Commit `89d471a` — FOUND
- Commit `984372a` — FOUND
- Commit `8ac567e` — FOUND
- Commit `b58fef4` — FOUND
- Commit `71e5847` — FOUND
