---
phase: 24-scanner-truthfulness
plan: "05"
subsystem: scanner
tags: [scanner, sane, resolution, source-classification, protocol, tdd, refactor]
requires:
  - "24-01 — tests/fake_sane.py, the shared python-sane 2.9.2 double"
  - "24-04 — scan_pages inherited at 11/12 branches, and the live Protocol lie"
provides:
  - "_resolve_source — the option scan, source validation and Auto fallback, extracted"
  - "_configure_device — source-first option assignment, returning the read-back resolution as int"
  - "SaneDevice.resolution: float — the Protocol finally declaring what the device returns"
  - "classifier-driven Auto recognition, case- and whitespace-insensitive"
  - "D-01 recorded in base.py as a settled DECLINE, protected by a test"
  - "FakeSaneDev.assignments and narrow_resolution_for_source() — option-order observability"
affects:
  - "24-06 — consumes _configure_device's returned resolution for UNIT_PIXEL geometry; inherits 8 branches of headroom in scan_pages"
  - "24-07 — carries the read-back resolution out of the backend via ScanBatch; rejected_pages left local and untouched here"
  - "25 — manual duplex is untouched; _is_manual_duplex and worker.py deliberately not modified"
tech-stack:
  added: []
  patterns:
    - "Split a function to create branch headroom BEFORE adding to it, as its own test-free commit"
    - "Ask the device what it is scanning FROM before telling it HOW -- a source change reloads every descriptor"
    - "Never trust a device's acceptance of a value; read it back and say so when it differs"
    - "Narrow object with isinstance rather than casting, when a dynamic __getattr__ is honestly typed"
key-files:
  created:
    - .planning/phases/24-scanner-truthfulness/24-05-SUMMARY.md
  modified:
    - src/saneless/scanner/sane_backend.py
    - src/saneless/scanner/base.py
    - tests/test_scanner.py
    - tests/fake_sane.py
decisions:
  - "The fake's narrowing knob is a METHOD, not a constructor keyword -- __init__ was already at ruff's five-argument maximum and PLR0913 counts keyword-only args (verified empirically)"
  - "The narrowing models the reload HAZARD, not a measured device: it leaves the prior value un-revalidated, which is the only way assignment order becomes observable"
  - "_configure_device's return value is deliberately not bound in scan_pages yet -- binding an unused local trips F841; 24-06 is its first consumer"
  - "Correcting the Protocol to float forced three test doubles off the same lie, because two doubles disagreeing about a type is exactly M-32"
metrics:
  tasks: 3
  commits: 5
  tests_added: 18
  suite: "845 passed (baseline was 827 passed)"
  hardware_suite: "4 passed (baseline was 4 passed)"
  scanner_module: "139 passed (baseline was 121 passed)"
completed: 2026-09-13
---

# Phase 24 Plan 05: Source-First Options and the Last Ad-Hoc Source Comparison Summary

The scanner now asks the device what it is scanning *from* before telling it *how*, reads back the resolution the device actually chose instead of assuming it got what it asked for, and the last string comparison that could silently ignore the operator's routing choice is gone.

## What Was Built

### Task 1 — the split, as its own behaviour-neutral commit

Commit `42b34a3`. **No test file is in this commit** — `git show --stat` is one file, 87 insertions, 30 deletions — which is what makes it provable that behaviour did not change.

`scan_pages` sat at **11 of ruff's limit of 12**, inherited unchanged through plans 24-03 and 24-04. Two module-level helpers were extracted, following the `_set_geometry`/`_maybe_crop` precedent in the same file:

| Helper | Carries |
|---|---|
| `_resolve_source(raw_options, requested)` | The option scan, the source validation and the `Auto` fallback. Returns `(effective_source, has_source_option)` |
| `_configure_device(dev, settings, effective_source, *, has_source_option)` | The option assignment block — the function tasks 2 and 3 then modify |

**The presence-versus-constraint distinction is preserved deliberately.** `has_source_option` is set on option *presence*, independently of whether the constraint parses as a list. A device can expose `source` with a constraint this code cannot read, and it must still be assigned. A helper returning only the parsed constraint would have collapsed the two and silently stopped setting the source on such a device — a behaviour change smuggled into a "pure refactor".

No context dataclass was needed: `_configure_device` takes four arguments, one under ruff's `PLR0913` limit.

### Task 2 — source-first, and the resolution read back (D-11)

RED `b209ad8`, GREEN `92bf7fd`.

`_configure_device` now assigns **source → mode → resolution**. `sane.py:188-213` reloads every option descriptor when a `set_option` reports `INFO_RELOAD_OPTIONS`, and a source change does exactly that, so setting the source last let a resolution validated against the platen's constraint be stranded under a feeder's narrower one. Geometry still follows all three, via the caller's existing `_set_geometry`.

The resolution is then read back with `int(dev.resolution)`, and a mismatch logs a WARNING naming both values with lazy `%s` interpolation:

```
Scanner substituted resolution: requested %s dpi, device reports %s dpi
```

`%s` rather than `%d` is load-bearing for the test: `%d` would print `1200` even for a float, so it could not tell whether `int()` had been applied. With `%s`, the assertion that the message contains `1200` and **not** `1200.0` genuinely proves the conversion happened.

**`SaneDevice.resolution` is declared `float`** — the type the real device and the shared fake both return. It had been `int` since Phase 18. Fixed at the Protocol with no cast, no `# type: ignore` and no annotation workaround.

### Task 3 — the classifier recognises Auto, and D-01 is settled (Q8, D-01)

RED `6c861e6`, GREEN `8bea15a`.

`scan_pages` binds `source_kind = classify_source(effective_source)` once and tests `source_kind is SourceKind.AUTO`. The *decision* the branch makes stays config-driven through `auto_source_mode`; only the *recognition* moved to the single classifier.

`base.py`'s comment no longer defers the UNKNOWN question to this phase. It records the answer: **DECLINE**.

## The Discretion Item Was Examined and Changed — With the Evidence

24-CONTEXT.md left the `auto_source_mode` override to the planner's judgement with the note that "it may be correct as-is". **It was not correct**, and this is the measurement that decided it:

| Source string | old `== "Auto"` | `classify_source` | Old routing with `auto_source_mode = "adf"` |
|---|---|---|---|
| `"Auto"` | True | `AUTO` | feeder — correct |
| `"auto"` | **False** | `AUTO` | **single page — the whole stack lost** |
| `" AUTO "` | **False** | `AUTO` | **single page — the whole stack lost** |
| `"Automatic Document Feeder"` | False | `FEEDER` | feeder — correct, override irrelevant |

A device reporting its source in lowercase classifies as `AUTO`, so `uses_feeder` is False and it takes the single-page path — which means it never reaches the override at all. `auto_source_mode = "adf"` was silently ignored and a ten-sheet stack returned one page, with no error. That is the exact user-visible failure C-06 exists to eliminate, surviving in a second place.

Both mis-spelled cases are now asserted, and both were **shown failing** in the RED commit:

```
FAILED TestAutoSourceRecognition::test_auto_is_recognised_whatever_its_spelling[auto]
FAILED TestAutoSourceRecognition::test_auto_is_recognised_whatever_its_spelling[  AUTO  ]
```

The `["Auto"]` case passed throughout, which is what shows this was a *recognition* defect and not a routing one.

## D-01 Is Declined, Not Deferred

`base.py`'s comment previously said the safer default "belongs to Phase 24, not here". It now reads as an answer:

- `SourceKind.UNKNOWN` keeps single-page routing (`uses_feeder` is False).
- C-06's "if the device exposes a `source` option, treat anything that is not the flatbed entry as multi-page" default is **DECLINED**, not deferred a third time.
- The residual risk is accepted knowingly: a genuinely new feeder name yields a one-page PDF until someone adds a token to `_FEEDER_TOKENS`. Visible to the operator, fixed by one tuple entry.
- The opposite failure — treating a flatbed as a feeder and re-scanning the platen — is named as the expensive one and is bounded by `_MAX_ADF_PAGES` (plan 24-03).
- It closes with "This is a settled answer. Do not re-open it as an unmade decision."

`classify_source`'s behaviour, branch order and token list are **unchanged**. The safer default is **not** implemented. `test_an_unrecognised_source_takes_the_single_page_path` protects the decision with a test rather than only a comment, and sets `auto_source_mode = "adf"` precisely so that an unrecognised name is shown not to reach the Auto override either.

## Complexity Budget After This Plan — for 24-06 and 24-07

Measured with `uv run ruff check --config lint.pylint.max-branches=1`:

| Function | Branches | Limit | Headroom |
|---|---|---|---|
| `scan_pages` | **4** | 12 | **8** |
| `_acquire_pages` | 9 | 12 | 3 |
| `_resolve_source` | 6 | 12 | 6 |
| `get_capabilities` | 5 | 12 | 7 |
| `_set_geometry` | 3 | 12 | 9 |
| `_configure_device` | 2 | 12 | 10 |
| `_validate_page_image` | 2 | 12 | 10 |
| `_open_device` | 2 | 12 | 10 |

**`scan_pages` went from 11 to 4.** At `max-branches=8` the only function that flags is `_acquire_pages` at 9, which is pre-existing and was left at that count by plan 24-04; `scan_pages` does not appear, satisfying the criterion.

## What 24-06 and 24-07 Consume

`_configure_device` is a module-level private function with this shape:

```python
def _configure_device(
    dev: SaneDevice,
    settings: ScanSettings,
    effective_source: str,
    *,
    has_source_option: bool,
) -> int:
```

It returns **the resolution the device actually reports, as an `int`**.

**`scan_pages` does not bind that return value yet, and this is deliberate.** Binding it to a local that nothing reads would trip ruff's `F841`, and this project forbids suppressing it. Plan **24-06**'s `UNIT_PIXEL` geometry conversion is its first consumer and should bind it at the call site; plan **24-07**'s `ScanBatch` then carries it out of the backend. The value is proven to exist and to be correct by the warning-text tests, not merely asserted to.

It is **not** stamped into each image's `.info["dpi"]` — D-12 rejected that channel after measuring `crop_to_paper_size(...).info` as `{}` and a PNG round-trip degrading 300 to 299.9994.

**`ScanBatch` was not built, and `rejected_pages` was not promoted.** The local counter in `_acquire_pages` is untouched and still local, exactly as 24-04 left it for 24-07.

## New Test Infrastructure in the Shared Fake

Rather than create a fourth divergent double, `tests/fake_sane.py` gained two capabilities:

| Addition | Purpose |
|---|---|
| `assignments: list[str]` | Records every assignment the device actually received, in order. Unknown option names are **not** recorded, because the real library stores those with no device call at all — logging them would invent one |
| `narrow_resolution_for_source(source, constraint)` | Arms a narrower resolution range that selecting a given source switches on |

**The narrowing models the hazard, not a measured device, and says so.** It swaps in the narrower range and deliberately does **not** re-validate the value already stored. That is the only way assignment order becomes observable: re-clamping on reload would make the old order pass too. Assumption **A5** records that the real `test:0` backend does **not** clamp this way — setting `source` after `resolution` left 1200.0 intact — so this cannot be reproduced against real hardware and was modelled on purpose. This is documented in the fake's module docstring so a later reader does not mistake it for a measurement.

Four contract tests assert the new rows, honouring `TestFakeSaneContract`'s stated rule that a row cannot be added to the fake without also being asserted.

## Deviations from Plan

### 1. [Rule 3 — Blocking] The fake's knob is a method, not a constructor keyword

- **Plan text:** "plan 24-01 gave the fake constructor knobs for this, and if the specific knob is missing, add it to `tests/fake_sane.py`".
- **Issue:** `FakeSaneDev.__init__` already carries exactly five keyword-only parameters, and ruff's `PLR0913` limit is 5. A sixth would violate it, and CLAUDE.md forbids both raising the limit and suppressing the rule.
- **Verified empirically rather than assumed:** a six-keyword-only-argument probe run through `ruff --isolated` reported `PLR0913 Too many arguments in function definition (6 > 5)`. Ruff does count keyword-only arguments.
- **Fix:** the knob is the method `narrow_resolution_for_source(source, constraint)`. This satisfies the plan's actual instruction — put it in `tests/fake_sane.py` rather than subclassing the fake in the test module — without disturbing the constructor or any of the ~15 existing call sites. The reason is recorded in the method's own docstring.

### 2. [Rule 1 — self-inflicted] My own comment broke the `== "Auto"` probe

- **Found during:** Task 3 verification.
- **Issue:** I explained the defect with a comment quoting `effective_source == "Auto"` verbatim. Correct reasoning, but it put the forbidden literal back in the file and `grep -c '== "Auto"'` read **1** against a criterion of 0 — a criterion that exists precisely to detect the comparison surviving.
- **Fix:** reworded to "compared the source string for equality against the one exact spelling `Auto`". The full reasoning is preserved; the literal is gone. Count back to **0**. This is the third occurrence of this exact self-inflicted pattern in the phase (24-04 hit it twice).

### 3. [Acceptance-probe correction] The no-suppressions probe is unachievable as written

- **Plan probe:** "`grep -rn '# type: ignore\|# noqa' src/saneless/scanner/sane_backend.py` returns nothing."
- **Measured:** two matches, both **pre-existing** and both untouched by this plan — `# noqa: PLW0603` at line 48 and `# noqa: PLC0415` at line 50, inside `_ensure_sane`. Plans 24-03 and 24-04 both recorded leaving them alone.
- **Resolution:** the probe's *intent* — that this plan introduces no suppression — holds. The suppression count in the file is unchanged at 2, both in a function this plan never edited, and there is no `# type: ignore` anywhere. The Protocol correction was made properly rather than annotated around.

### 4. [Acceptance-probe correction] The plan's final verification grep has two legitimate hits

- **Plan verification:** "`grep -rn '== "Auto"\|"flatbed" in' src/` returns nothing."
- **Measured:** two matches, neither of which should be removed:
  - `src/saneless/scanner/base.py:95` — `if "flatbed" in lower:`, inside `classify_source` itself. This is the one place that rule is *supposed* to live; the plan explicitly forbids changing the classifier's behaviour or token list. A probe that required deleting it would contradict its own action text.
  - `src/saneless/auto_profiles.py:228` — a **docstring** describing the three ad-hoc rules that Phase 21 already *deleted* from that function (D-02/Q9). It is a record of removed code, not code.
- **Resolution:** the intent — no source-name recognition outside `classify_source` — is verified. The backend's counts are the decisive ones: `== "Auto"` is **0**, `SourceKind.AUTO` is **1**, `SourceKind` is **2**.

### 5. [Rule 3 — Blocking] Correcting the Protocol forced three test doubles and one assertion

Fixing `SaneDevice.resolution` to `float` surfaced real type errors, which were fixed properly as the plan instructed:

- **`MockSaneDev`** and **`_FakeSaneDevice`** are passed straight into `_scan_adf_pages(dev: SaneDevice, ...)`, so they are structurally checked against the Protocol. A settable Protocol attribute is invariant, so their `resolution: int` no longer matched. Both now declare `float`.
- **`_NoGeometryDevice`** is *not* structurally checked — it reaches the backend through the monkeypatched module seam, typed `Any` — but carried the identical lie. Corrected anyway: two doubles disagreeing about a type is precisely the M-32 failure this phase exists to end.
- **One assertion needed narrowing.** `FakeSaneDev.__getattr__` is honestly typed `-> object`, mirroring the real dynamic option lookup, and `1.0 <= dev.resolution <= 600.0` is not a defined operation on `object`. Both checkers flagged it. Resolved with `isinstance(resolution, float)` narrowing — **not** a cast and not a suppression — which doubles as an assertion of the very float-ness the Protocol now declares.

## An Observation Left Deliberately Untouched

`scan_pages` still carries the comment `# Set scan area geometry for paper size constraint (D-01)`. That `D-01` is **Phase 18's** decision id, not this plan's D-01, which now means something entirely different in the same subsystem. It is the same class of stale-reference hazard as the `D-04` this plan was asked to remove, but no criterion covers it and it is not a correctness issue, so it was not touched. Flagged here so a future planner can retire it deliberately rather than discover it confusing.

## Verification

| Check | Result |
|---|---|
| `uv run pytest -m "not browser and not sane_hardware" -q` | **845 passed**, 0 failed (baseline 827) |
| `uv run pytest -m sane_hardware -q` | **4 passed**, 0 failed (baseline 4 passed) |
| `uv run pytest tests/test_scanner.py -q` | **139 passed**, 0 failed (baseline 121) |
| `uv run ruff check .` | exit 0 |
| `uv run ruff format --check .` | exit 0, 43 files |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |

Acceptance probes: `_resolve_source`/`_configure_device` module-level defs = 2; `scan_pages` absent at `max-branches=8`; `resolution: float` = 1; `resolution: int` = 0; `== "Auto"` = 0; `SourceKind.AUTO` = 1; `SourceKind` = 2; `D-04` = 0; `belongs to Phase 24` in base.py = 0; `declined` in base.py = 1; `_MAX_ADF_PAGES` in base.py = 1; `_is_manual_duplex` in pipeline.py = 2 (unchanged — the file is not in any commit of this plan).

All five commits landed with hooks enabled. No `--no-verify`, no `SKIP=`, no `# noqa`, no `# type: ignore`, no stub. Both TDD gate sequences are present: `test(...)` then `feat(...)` for task 2 and again for task 3. No file was deleted by any commit (`git diff --diff-filter=D` against the base is empty).

## Known Stubs

None. `_configure_device`'s return value has no consumer yet, but it is not a stub: it is computed, verified by test, and documented above with its two named downstream consumers.

## Threat Model Dispositions Honoured

| Threat | Disposition | How |
|---|---|---|
| T-24-15 | mitigate | D-11's read-back. `int(dev.resolution)` is compared against the request and a mismatch logs a WARNING naming both, so a silent substitution — measured, `5000` becomes `1200.0` — can no longer reach Phase 23's authoritative page geometry unnoticed |
| T-24-16 | mitigate | Q8. Recognition of an Auto source goes through `classify_source`, so a lowercase or whitespace-padded `auto` can no longer skip the routing override and turn a stack into a one-page PDF |
| T-24-17 | accept | **D-01, explicitly and knowingly.** The safer default is declined, not deferred; the rationale, the accepted residual risk and `_MAX_ADF_PAGES` as the bound on the opposite failure are recorded in `base.py` itself, and a test pins the routing so reversing it flips a test |
| T-24-SC | accept | No package-manager install occurred in this plan |

## Threat Flags

None. This plan adds no network endpoint, no auth path, no file-access pattern and no schema change. It removes a silent data-completeness failure and makes an existing substitution visible.

## Notes for Downstream Plans

- **24-06** inherits `scan_pages` at **4 of 12 branches**. It should bind `_configure_device`'s returned `int` at the call site — it is currently unbound only to avoid `F841`. `FakeSaneDev`'s `geometry_range` constructor knob is still free for D-19's measured clamp, but note the constructor is now at ruff's five-argument ceiling, so any further knob must be a method.
- **24-07** owns `ScanBatch`. `rejected_pages` is still local in `_acquire_pages` and untouched; the read-back resolution is the second value it should carry out.
- **The Protocol lie is fixed and stays fixed.** `SaneDevice.resolution` is `float`, and three test doubles were corrected to match. Passing a new double straight into a `SaneDevice`-typed parameter will now be structurally checked — `FakeSaneDev` itself still does not satisfy the Protocol structurally, so continue driving it through the `FakeSaneModule` seam.
- **Phase 25** is untouched: `pipeline._is_manual_duplex` and `worker.py` were deliberately not modified.

## Self-Check: PASSED

- `src/saneless/scanner/sane_backend.py` — FOUND
- `src/saneless/scanner/base.py` — FOUND
- `tests/test_scanner.py` — FOUND
- `tests/fake_sane.py` — FOUND
- `.planning/phases/24-scanner-truthfulness/24-05-SUMMARY.md` — FOUND
- Commit `42b34a3` — FOUND
- Commit `b209ad8` — FOUND
- Commit `92bf7fd` — FOUND
- Commit `6c861e6` — FOUND
- Commit `8bea15a` — FOUND
