---
phase: 24-scanner-truthfulness
plan: "08"
subsystem: scanner
tags: [scanner, capabilities, constraints, cli, auto-profiles, fakes, tdd, d-17]
requires:
  - "24-01 — tests/fake_sane.py, the shared fake this plan finishes migrating to"
  - "24-02 — auto_profiles' pick_closest_resolution and the single classifier"
  - "24-07 — ScanBatch and the changed ScannerBackend ABC"
provides:
  - "sane_backend._constraint() — the one place any option constraint is parsed"
  - "sane_backend._OptionConstraint — presence, word list and range as separate facts"
  - "DeviceCapabilities.resolution_range — a faithful (min, max, step) float triple"
  - "pick_closest_resolution(resolution_range=...) — clamped into the range and snapped onto its step"
  - "cli._echo_capabilities() — renders whichever shape the device reported, never a blank value"
  - "fake_sane knobs: load_feeder(), report_sources(), set_page_delay(), declared option attributes"
  - "tests/test_pipeline.py — a manual duplex run driven through a real SaneBackend over the shared fake"
affects:
  - "25 — manual duplex inherits the pipeline-over-SaneBackend pattern added here"
  - "29 — HARD-01/03/04 touch the same acquisition path; nothing here pre-empts them"
  - "30 — APPL-03 can render resolution support as a list or a range"
tech-stack:
  added: []
  patterns:
    - "Two different facts get two fields, and neither is derived from the other"
    - "Parse device-supplied data in exactly one place; a third copy is a defect's third life"
    - "Faithful-then-coerce-at-use, rather than coerce-at-read"
    - "A failure appearing during a fake swap is a finding, not a migration nuisance"
    - "Configure the shared fake through its own knobs; never subclass it per test file"
key-files:
  created:
    - .planning/phases/24-scanner-truthfulness/24-08-SUMMARY.md
  modified:
    - src/saneless/scanner/base.py
    - src/saneless/scanner/sane_backend.py
    - src/saneless/auto_profiles.py
    - src/saneless/cli.py
    - docs/reference/cli-commands.md
    - tests/fake_sane.py
    - tests/test_scanner.py
    - tests/test_pipeline.py
    - tests/test_auto_profiles.py
    - tests/test_cli.py
    - tests/test_sane_hardware.py
decisions:
  - "resolution_range is a faithful tuple[float, float, float]; coercion to whole dpi happens at the point of use in _snap_into_range"
  - "CLI format: `  Resolution range: 1 to 1200 dpi in steps of 1`, printed only when a range was reported"
  - "A step of 0 clamps without snapping -- that is SANE's documented continuous range, not a divide-by-zero guard"
  - "_resolve_source's pre-existing raise on an unreadable source constraint is preserved, not quietly changed"
  - "test_iterator_deleted_before_cancel deleted: its own comment conceded the __del__ probe was GC-dependent"
requirements: [SCNR-06, SCNR-07]
metrics:
  tasks: 3
  commits: 5
  tests_added: 23
  suite: "905 passed, 35 deselected (baseline was 882 passed, 34 deselected)"
  hardware_suite: "5 passed (baseline was 4 passed)"
  browser_suite: "30 passed"
  completed: 2026-09-13
---

# Phase 24 Plan 08: The Constraint the Device Actually Gave, and One Definition of python-sane Summary

A range-reporting scanner's answer now survives into `DeviceCapabilities` instead of becoming an empty list, reaches both the operator and the profile generator, and the three hand-written doubles that let four defects ship green are gone — leaving exactly one definition in the repository of what python-sane does.

## The Headline: Two Different Facts, Neither Derived From the Other

This is Q6, and it was the decision most at risk of being "simplified" into a bug.

A SANE device constrains its resolution option with **either** a word list **or** a `(min, max, step)` range, never both. So `resolutions` and `resolution_range` are two different facts rather than two spellings of one. At most one is ever populated, and **neither is computed from the other**:

- No list is synthesised by expanding a range. That was rejected explicitly: printing invented DPIs would be saneless's fiction rather than the device's answer, in a phase named truthfulness.
- No range is inferred from a list, which would claim the device accepts every value between the listed ones.

Because at most one is populated, the two cannot disagree — which satisfies the "one fact must not have two independently-settable representations" constraint literally, while keeping every existing consumer working.

Measured against real `test:0`, under the `sane_hardware` marker:

```
capabilities.resolution_range == (1.0, 1200.0, 1.0)
capabilities.resolutions == []
```

## Field Type and Where Coercion Happens — Recorded As the Plan Asked

`resolution_range` is `tuple[float, float, float] | None`, keeping the **floats the device reported**. The members really are floats: `(1.0, 1200.0, 1.0)`, measured.

**Coercion to whole dpi happens at the point of use, not the point of reading** — in `auto_profiles._snap_into_range`, whose final line is `round(clamped)`. Nothing rounds off the device's answer on the way in. A test asserts every member of the stored range is still a `float`.

The field is defaulted and sits in the defaulted block, after `raw_options`. Verified:

```
[('sources', False), ('resolutions', False), ('modes', False), ('raw_options', True), ('resolution_range', True)]
```

Moving it up would reorder the dataclass and break positional construction at the 22 call sites across `src/` and `tests/`.

## The CLI's Chosen Range Format — Recorded As the Plan Asked

```
  Resolution range: 1 to 1200 dpi in steps of 1
```

Rendered with `:g`, so a whole-numbered bound prints without a trailing `.0`. It is printed **only** when a range was reported; a list-reporting device prints `  Resolutions: 150, 300, 600` exactly as before, and gains no invented range.

Every label in the capability block is now printed only when there is something to put after it. A label with nothing following it is the N-01 symptom the operator actually sees: it reads as "this scanner offers none", when the truth was that saneless had not read what the scanner offered. A test asserts no rendered line ends at its separator with an empty value.

## One `_constraint()`, Replacing Both Copies

Both duplicated parsing blocks are gone. Measured:

| Probe | Result |
|---|---|
| `grep -c 'isinstance(constraint, list)' sane_backend.py` | **1**, inside `_constraint` |
| `grep -c '^def _constraint' sane_backend.py` | **1** |

`_constraint()` handles all three documented shapes: an unconstrained option (`None`), a `(min, max, step)` range, and a word list.

**The presence-versus-constraint distinction survived the dedup**, which was the single most likely thing to be lost. `_OptionConstraint` reports `present` separately from `values` and `span`, because they answer genuinely different questions: a device may expose a `source` option whose constraint saneless cannot read, and it must still be recognised as *having* that option. A helper returning only the parsed constraint would have collapsed the two and silently stopped saneless assigning the source on such a device. Four parametrised regression guards pin it, and they are labelled in-file as guards rather than claimed as RED, because they already held.

**T-24-27 is mitigated.** The constraint object is device-supplied, so a tuple of the wrong arity or with non-numeric members yields neither field rather than a guessed value, and never raises out of `get_capabilities`.

## `pick_closest_resolution` Honours the Range

It used to return the target unchanged whenever the word list was empty — which is exactly what a range-reporting device produces. So `test:0` got 300 not because it offered 300 but because nothing had been read at all, and a device whose ceiling sat below 300 was asked for a resolution it had never advertised.

Clamping alone would not have been enough: a value inside the span but off the step grid is still one the device never offered, and SANE would silently substitute for it. The parametrised test asserts, for every case, that the result is inside `[min, max]` **and** reachable by the step.

| Range | Target | Result |
|---|---|---|
| `(1.0, 1200.0, 1.0)` | 300 | 300 — inside the range, not the old fallback coincidence |
| `(1.0, 200.0, 1.0)` | 300 | 200 — the maximum, honoured |
| `(400.0, 1200.0, 100.0)` | 300 | 400 — the minimum |
| `(40.0, 1200.0, 100.0)` | 300 | 340 — snapped onto the grid |

The snap case is **deliberately not a tie** (2.6 steps above the minimum, not 2.5). A tie would have silently encoded Python's banker's rounding as though it were a decision about scanners.

A step of `0` clamps without snapping. That is not a divide-by-zero guard dressed up as behaviour — it is SANE's documented meaning for a continuous range.

## D-17 Completed: Four Real Findings From the Swap

`MockSaneDev`, `MockSaneModule` and `_FakeSaneDevice` are deleted. `grep -rn 'class MockSaneDev\|class MockSaneModule\|class _FakeSaneDevice\|class _NoGeometryDevice' tests/` returns nothing, and no class anywhere outside `tests/fake_sane.py` defines `multi_scan`, `get_options`, `snap` or `sane_signature`.

The plan warned that a failure after the swap is a real finding, not a migration nuisance. **Four appeared, and all four were defects the doubles had been hiding.**

### 1. "ADF never calls snap" was simply false

The real `_SaneIterator.__next__` calls `start()` **and then** `snap()` once per sheet. The deleted double's `multi_scan()` returned `iter(list)` and touched neither, so `assert_not_called()` passed for a reason that has nothing to do with python-sane. Five tests asserted this falsehood.

The feeder path is now identified by its true call sequence — `["start", "snap"] * 3 + ["start"]`, ending in the probe that discovers the feeder is empty — which genuinely distinguishes it from a flatbed scan's `["start", "snap"]`. The new assertion is strictly stronger than the one it replaces.

### 2. Letter's 215.9 mm does not read back exactly

`SANE_Fixed` is a 16.16 fixed-point integer, so 215.9 mm is not representable and returns differing in the low bits **without the device having clamped anything**. The old double stored floats verbatim, so `assert dev.br_x == 215.9` passed. It now asserts `pytest.approx(..., abs=1e-4)`.

This is precisely the reason D-19 compares scan areas with a tolerance rather than for equality — and the double had been concealing the evidence for that decision.

### 3. `test_full_no_geometry` passed only by coincidence

It wrote `-1.0` to the four geometry options as a sentinel and read `-1.0` back. A real device clamps to the option's range, so on the faithful fake that sentinel returns `0.0` — the test would have passed or failed for reasons unrelated to what it claimed to check. It now asserts against the device's own assignment log that no geometry option was assigned at all, which is the actual claim.

### 4. The faithful fake did not satisfy the `SaneDevice` protocol

Both `ty` and `pyrefly` rejected `FakeSaneDev` where `SaneDevice` was expected: the fake serves options through `__getattr__`, exactly as the real library does, so `br_x` reads as `object` rather than `float`. **The real python-sane object would fail the same structural check.** The deleted doubles satisfied the protocol only by declaring concrete typed attributes python-sane does not have — M-32's "fake kinder than the library", caught by the type checkers rather than by a test.

Fixed by declaring the served options on the fake with the types the real device returns. They are annotations only: no value is assigned, so lookup still falls through to `__getattr__` and runtime behaviour is unchanged. No cast, no suppression.

### The fake's new knobs

All are **methods**, not constructor keywords, because `__init__` already carries ruff's five-argument maximum and this project forbids suppressing `PLR0913` — the same reason `set_page_size` and `narrow_resolution_for_source` are methods. Nothing was subclassed.

| Knob | Purpose |
|---|---|
| `load_feeder(pages)` | Exact page images, for tests about the integrity checks themselves. Also rewinds the feeder — the physical act of a stack taken out and put back. |
| `report_sources(sources)` | Narrow the source constraint, for names outside the default table. |
| `set_page_delay(seconds)` | A slow scanner, replacing a hand-rolled blocking iterator that was a device double of its own. |
| declared option attributes | Lets the fake be handed to production code expecting `SaneDevice`. |

### One test deleted, deliberately

`test_iterator_deleted_before_cancel` built a `TrackingIterator` with a `__del__` probe, hosted on a device double. Its **own comment** conceded the deletion "may or may not appear depending on GC", so the only thing it ever asserted was that `cancel` had run — which its neighbour asserts. Nothing was lost but the double; the `del iterator` it nominally guarded is still in the backend's own `finally`. The rationale is recorded in the surviving test's docstring, not only here.

## SCNR-07: A Duplex Defect Can Now Surface in Pipeline Tests

`tests/test_pipeline.py` drives a manual duplex run through a **real `SaneBackend`** over the shared fake, imported package-qualified as `from tests.fake_sane import ...` (the bare form raises `ModuleNotFoundError` at collection). The other `MagicMock` sites are untouched — the requirement is one duplex path, and converting all of them would be churn.

The two passes share one device handle, and `scan_pages` opens, drains and closes per pass — so the feeder is genuinely empty when pass B begins. Rather than inventing a second device or a background thread, the stack is reloaded from the **`AWAITING_FLIP` status callback**, which the pipeline emits immediately before it waits on the flip event. That is exactly the moment the operator's flip happens, so the test models the real sequence rather than working around it.

It asserts six interleaved pages, `SUCCESS`, that the source was assigned to the device on **both** passes, and that six sheets were snapped — facts a `MagicMock(spec=ScannerBackend)` cannot witness.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 — Bug in my own test] The non-numeric range case could not be routed through a device**

- **Found during:** Task 1 GREEN.
- **Issue:** I parametrised the malformed-constraint test over `("low", "high", "step")` and drove it through `FakeSaneDev`. It failed inside `fake_sane.py`'s `_default_values`, which coerces a range's members with `float()`.
- **Resolution:** **the fake was right and my test was wrong.** No real SANE backend can report a range of strings, so widening the fake to accept one would make it model a library that does not exist — the very drift D-17 exists to stop. The guard is defensive hardening against a malformed device, so it is now asserted directly against `_constraint`, which is the function that does the hardening. The two wrong-arity cases still run end to end through a device. The reasoning is recorded in the test's own docstring.

**2. [Rule 3 — Blocking] `PLR0912` and `RUF046` after the CLI change**

- **Found during:** Task 2 GREEN.
- **Issue:** The four new render branches pushed `devices` to 14 branches against ruff's limit of 12; and `int(round(x))` on a float is a redundant cast.
- **Fix:** Rendering was extracted into a module-level `_echo_capabilities()` — the house pattern, used by `_claim_slug` and `_snap_into_range` for the same reason. The limit is respected, not raised. `RUF046` fixed by dropping the redundant `int()`: `round()` on a float already returns one. **No rule disabled, no `noqa`.**

**3. [Rule 1 — Bug] A bulk rename caught a test that reaches no device**

- **Found during:** Task 3 migration.
- **Issue:** Migrating to the faithful fake required `mode="color"` → `mode="Color"`, because the fake carries the real device's list constraint. The bulk rename also hit `test_scan_settings_fields`, which constructs a `ScanSettings` value object and touches no device at all — so its assertion then contradicted its own input.
- **Fix:** Reverted that one test to lowercase on both sides, with a comment recording that it is a value-object test and therefore subject to no device's constraint.

### Corrections to the Plan's Own Claims

Recorded because a verifier checking the plan literally would otherwise read these as failures:

- **The plan's Task 3 instruction to "fix the stale slug reference in the comment at `tests/test_scanner.py:326`" describes something that does not exist.** Plan 24-02's summary already recorded this, and I re-derived it on the tree I ran on rather than trusting either document: `grep -rn 'adf-simplex\|flatbed-scan\|auto-scan' tests/ src/ docs/` returns **nothing**, before my changes and after. There was no stale slug to fix. This is the second wave in a row where a plan's stated reference inventory was wrong, exactly as the orientation brief warned.
- **The plan's line references for the two duplicated parsing blocks (`sane_backend.py:323-335` and `:460-466`) were stale** after plan 24-05's split; both copies were located by content instead. Their *content* matched the plan exactly, including the `len(opt) >= 9` versus `len(opt) < 9` difference and the `break`.

### Deliberate Non-Changes

**`_resolve_source` still raises on a device whose `source` constraint it cannot read.** With a non-list constraint, `available_sources` is empty and any requested source raises `ScanError`. This is arguably wrong — an unconstrained string option is a plausible device — but it is **pre-existing behaviour, out of this plan's scope, and no requirement names it**. Changing it in the last plan of the phase would be an unrequested behaviour change in the code path six waves of tests now depend on. It is recorded here rather than silently altered, and the regression guards assert the current behaviour deliberately: the raise is what witnesses that presence was detected at all.

**The word-list path still uses `int(r)` without hardening.** T-24-27's mitigation names tuples specifically. Hardening the list path would silently drop values and is not asked for; parity with the previous behaviour is kept on purpose.

## The Self-Inflicted Probe Break Did Not Recur

Waves 24-04, 24-05 and 24-06 each broke an acceptance probe by quoting, in an explanatory comment, the exact literal the probe counts as zero. This plan carried the hazard in two obvious places: a comment explaining the `isinstance(constraint, list)` dedup, and the docs row replacing "Show raw SANE options for each device".

Both were written defensively from the start. Final counts: the `isinstance` literal appears **1** time and it is the implementation inside `_constraint`; the old docs sentence appears **0** times. **No documentation was deleted to satisfy a count.**

## Tooling Note

**Serena's `replace_in_files` was unavailable for the whole plan** — `LanguageServerTerminatedException`, the same failure the orientation brief recorded nine previous agents hitting. Four bulk-rename calls failed with it, changing nothing. The mechanical portions of the migration were done with explicit, counted, ordered substitutions instead, and every one printed its replacement count for inspection; everything semantic was hand-edited. Recorded so a future wave does not spend time rediscovering it.

## Verification

| Check | Result |
|---|---|
| `uv run pytest -m "not browser and not sane_hardware" -q` | **905 passed, 35 deselected**, 0 failed (baseline 882 / 34) |
| `uv run pytest -m sane_hardware -q` | **5 passed**, 0 failed (baseline 4) |
| `uv run pytest -m browser -q` | **30 passed**, 0 failed (baseline 30) |
| `uv run ruff check .` | exit 0 |
| `uv run ruff format --check .` | exit 0, 43 files |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |

Acceptance probes: `isinstance(constraint, list)` **1**; `^def _constraint` **1**; legacy double classes **none**; `from tests.fake_sane import` in `test_pipeline.py` **1**; bare `from fake_sane import` **none**; retired slugs **none**; old docs sentence **0**; `resolution_range` in `cli.py` **2** and `auto_profiles.py` **8**.

All five commits landed with hooks enabled. No `--no-verify`, no `SKIP=`, no `# noqa`, no `# type: ignore`, no rule disabled, no stub. The seven surviving `# noqa` in the repository are all pre-existing and all in files this plan never opened.

No tracked file was deleted: `git diff --diff-filter=D --name-only <base>..HEAD` is empty.

## TDD Gate Compliance

Tasks 1 and 2 each ran RED → GREEN as separate atomic commits. Task 3 is a migration with no new behaviour and is correctly a single `test(...)` commit.

| Task | RED | GREEN |
|---|---|---|
| 1 — one `_constraint()`, typed `resolution_range` | `056abb3` (9 failing) | `73511ff` |
| 2 — the range reaches auto-profiles and the CLI | `6f96358` (8 failing) | `4bd7775` |
| 3 — the three doubles collapse into one | — | `9f49360` |

Task 1's RED also included 5 tests that passed on arrival; they are labelled in-file as regression guards for the presence-versus-constraint distinction, not claimed as RED. Task 2's hardware assertion likewise passed on arrival, because Task 1 had already made it true — it verifies that work against real libsane rather than driving Task 2.

## Known Stubs

None. Every path added is covered: all three constraint shapes, the malformed-tuple guard, the four range cases, both CLI render shapes, the real-hardware range, and the duplex run through the real backend.

## Threat Model Dispositions Honoured

| Threat | Disposition | How |
|---|---|---|
| T-24-27 | mitigate | `_constraint` handles all three shapes without trusting the type; a tuple of wrong arity or with non-numeric members yields neither field and does not raise. Asserted at both the unit and the device level |
| T-24-28 | mitigate | `_snap_into_range` clamps into `[min, max]` and snaps onto the step, so a device cannot induce a resolution it did not offer. A zero step is handled as SANE's continuous range |
| T-24-29 | accept | Output is the device's self-description, to the operator's own terminal, which they explicitly asked to see. `sources` and `modes` were already printed |
| T-24-30 | mitigate | D-17 completed: exactly one definition of python-sane's behaviour, used by both `test_scanner.py` and `test_pipeline.py`. Four failures during the swap were investigated as findings and are documented above, not adjusted away |
| T-24-SC | accept | No package-manager install occurred. **`uv.lock` and `pyproject.toml` are byte-identical across this plan**, and `pyproject.toml`'s `[project]` section is unchanged across all eight plans of the phase — the phase added zero dependencies |

## Threat Flags

None. This plan adds no network endpoint, no auth path, no file-access pattern and no schema change. It removes a silent capability lie and deletes three test doubles.

## Notes for Downstream Plans

- **Phase 25** inherits the pipeline-over-`SaneBackend` pattern. If manual duplex needs a second such test, reload the feeder from the `AWAITING_FLIP` callback rather than inventing a second device handle.
- **`_resolve_source`'s raise on an unreadable source constraint** is documented above as a deliberate non-change. If a real device is ever found exposing an unconstrained `source`, that is the line to revisit.
- **Do not add a third resolution field.** If a future device shape appears, it belongs inside `_constraint` as a fourth branch, not as another field on `DeviceCapabilities`.
- **The fake is configured through its knobs.** If one is missing, add it to `tests/fake_sane.py`; subclassing it in a test module reintroduces exactly the drift this plan finished removing.

## Self-Check: PASSED

All eleven modified files exist; `24-08-SUMMARY.md` created. All five commit hashes resolve in `git log`:

- `056abb3` — FOUND
- `73511ff` — FOUND
- `6f96358` — FOUND
- `4bd7775` — FOUND
- `9f49360` — FOUND

`STATE.md` and `ROADMAP.md` were **not** modified, as instructed — the orchestrator owns those writes.
