---
phase: 24-scanner-truthfulness
plan: "01"
subsystem: test-infrastructure
tags: [testing, sane, fakes, pytest-markers, ci, tdd]
requires: []
provides:
  - "tests/fake_sane.py — FakeSaneError/FakeSaneDev/FakeSaneModule, the single definition of python-sane 2.9.2 semantics"
  - "tests/test_sane_hardware.py — marker-gated integration module driving the real SANE test backend"
  - "sane_hardware pytest marker, registered and deselected by default in CI and CONTRIBUTING"
  - "pyrefly search-path that makes tests/ importable as a package"
affects:
  - "24-02..24-08 — every plan that needs a faithful SANE double"
  - "24-04 — owns turning the RED ten-page assertion green (D-05) and adding the CI marker step"
  - "24-06 — passes geometry_range=(0.0, 200.0, 1.0) to reproduce D-19's clamp"
  - "24-08 — deletes the three legacy doubles"
tech-stack:
  added: []
  patterns:
    - "Fakes are concrete classes derived from the installed library source, not MagicMock"
    - "Contract tables are parametrised, so a new row cannot be added to the fake unasserted"
    - "Session-scoped pytest.MonkeyPatch.context() for process-global env that must precede library init"
key-files:
  created:
    - tests/fake_sane.py
    - tests/test_sane_hardware.py
  modified:
    - tests/test_scanner.py
    - pyproject.toml
    - .github/workflows/ci.yml
    - CONTRIBUTING.md
decisions:
  - "Marker named sane_hardware (Q4): 'integration' is too broad for Phase 29+, 'sane' collides with the module name"
  - "pyrefly search-path = ['src', '.'] — 'src' first, because '.' alone gives src/saneless two module identities"
  - "Groups are kept in the fake's option table, unlike the real library, so the documented group branch is reachable"
  - "The ten-page test is left as a plain failure, never registered as an expected one"
metrics:
  tasks: 3
  commits: 4
  tests_added: 38
  suite: "777 passed, 33 deselected (baseline was 742 passed, 30 deselected)"
completed: 2026-09-13
---

# Phase 24 Plan 01: Shared SANE Fake and Marker-Gated Integration Module Summary

Built one faithful python-sane 2.9.2 double derived from the installed library source, proved all five of its measured behaviours with a parametrised contract test, and stood up a marker-gated module that drives the real SANE `test` backend — with SCNR-08's ten-page assertion deliberately RED against the pure-black drop that plan 24-04 removes.

## What Was Built

### Task 1 — `tests/fake_sane.py` and its contract test

RED commit `d1a62ce`, GREEN commit `72dbde1`.

The fake exports exactly three public names — `FakeSaneError`, `FakeSaneDev`, `FakeSaneModule` — and is imported as `from tests.fake_sane import ...`. The bare `from fake_sane import ...` form is a collection error under pytest 9's importlib mode and is not used anywhere.

`FakeSaneError` subclasses `Exception` directly, mirroring the real MRO `(error, Exception, BaseException, object)`, which was confirmed against `_sane.error` on this machine.

All five measured rows of RESEARCH Finding 7 are implemented and asserted, including the row CONTEXT.md does not name (wrong Python type → `TypeError: SANE_FIXED requires a floating point number`). The most consequential row is the first: an unknown option name is **stored silently**, which is the exact inverse of `_NoGeometryDevice`'s raise and the reason `_set_geometry` could always return `True` while the crop fallback it guarded was unreachable.

The default option table uses realistic constraints so C-06- and N-01-class defects cannot pass against it: the long `"Automatic Document Feeder"` source name, and resolution as the **range** `(1.0, 1200.0, 1.0)` rather than a list. Range constraints clamp silently (5000 → 1200.0, 0 → 1.0); list constraints raise `Invalid argument`. `resolution` reads back as `float`, not `int`.

Option **names** are hyphenated (`tl-x`) while attribute **assignment** uses underscores (`dev.tl_x`); both spellings are honoured because D-09's presence check reads the first and the assignment writes the second.

**Constructor knobs later plans will use** (all keyword-only):

| Knob | Default | Purpose |
|---|---|---|
| `options` | `None` | A full nine-element option table. When given, `geometry_range` is ignored because the table already carries its constraints. Use this to narrow a constraint (24-05's source-narrows-resolution case). |
| `pages` | `3` | How many sheets the feeder holds. |
| `start_error` | `None` | An exception raised from `start()`. `FakeSaneError("Document feeder out of documents")` ends the feed cleanly; any other message is a real failure that propagates. |
| `start_error_page` | `0` | Zero-based page index at which `start_error` fires. |
| `geometry_range` | `(0.0, 300.0, 1.0)` | Constraint for the four geometry options. Default admits A4; **24-06 passes `(0.0, 200.0, 1.0)`** to reproduce D-19's measured clamp. |

The argument count is capped at five deliberately — ruff's `PLR0913` limit is 5 and the project forbids raising or suppressing it. Further configuration goes through `options`, for which a module-private `_build_option_table(sources=, modes=, resolution_range=, geometry_range=)` helper exists.

`FakeSaneModule` provides `init()` (returns `(1, 0, 3)`, counts calls), `get_devices()` (four-element tuples), `open()` (returns the one shared device so a test can configure it first) and `exit()`.

### Task 2 — marker registration and the widened filter

Commit `072b7a8`.

`sane_hardware` is registered in `pyproject.toml`'s `markers` list. `strict_markers = true` is live, so this had to land before Task 3 used it.

**Marker name rationale (Q4), as the plan asked to be recorded:** `sane_hardware` was chosen over `integration`, which is too broad — Phase 29 and later will want that name for other things — and over `sane`, which collides conceptually with the module name.

The default deselect filter was widened from `-m "not browser"` to `-m "not browser and not sane_hardware"` in the three places that must agree: `.github/workflows/ci.yml`'s test step, and CONTRIBUTING's five-checks table row and reproduce-the-gate code block.

No `uv run pytest -m sane_hardware` CI step was added, deliberately. D-18 wants CI to run the marker, but the ten-page test is RED until 24-04 lands D-05; adding it here would make CI red for three waves. **Plan 24-04 owns that step.**

No new apt package: `libsane-dev` depends on `libsane1`, which ships `libsane-test.so.1`, and both CI jobs already install it. Verified present on this machine at `/usr/lib64/sane/libsane-test.so.1.0.32`.

### Task 3 — `tests/test_sane_hardware.py`

Commit `26bbb97`.

Three tests behind a class-level `@pytest.mark.sane_hardware` decorator. The `SANE_CONFIG_DIR` fixture is session-scoped and autouse within the module, using `pytest.MonkeyPatch.context()` because the function-scoped fixture of that name is unavailable at session scope. Only `dll.conf` is written; no `test.conf` is copied.

Enumeration asserts `"test:0" in names` **positively**. A "list is not empty" assertion would pass for the wrong reason in CI, where an empty device list and a missing test backend are indistinguishable (T-24-02).

The device's real behaviour was re-measured here and matches RESEARCH exactly: devices `['test:0', 'test:1']`, sources `['Flatbed', 'Automatic Document Feeder']`, resolution `(1.0, 1200.0, 1.0)`, `tl-x` `(0.0, 200.0, 1.0)`.

## The RED Ten-Page Test — Recorded Verbatim

**This test fails on purpose and must stay failing until plan 24-04.** It is recorded here so 24-04 can prove it turned green rather than merely changed shape.

Command: `uv run pytest -m sane_hardware -q`

Result: **`1 failed, 2 passed, 807 deselected in 0.26s`**

```
        pages = list(SaneBackend().scan_pages("test:0", settings))
>       assert len(pages) == 10
E       assert 0 == 10
E        +  where 0 = len([])

tests/test_sane_hardware.py:113: AssertionError
------------------------------ Captured log call -------------------------------
WARNING  saneless.scanner.sane_backend:sane_backend.py:204 Page 1: pure black (mean=0.0, stddev=0.0), skipping
WARNING  saneless.scanner.sane_backend:sane_backend.py:204 Page 2: pure black (mean=0.0, stddev=0.0), skipping
... Pages 3 through 9, identical ...
WARNING  saneless.scanner.sane_backend:sane_backend.py:204 Page 10: pure black (mean=0.0, stddev=0.0), skipping
FAILED tests/test_sane_hardware.py::TestRealSaneTestBackend::test_ten_pages_come_back_through_the_feeder
```

Ten sheets were fed and ten pages acquired; `_validate_page_image` discarded every one. The feeder name and the routing are correct — it is the content policy that destroys the scan. Run twice in a row the result is byte-identical (`1 failed, 2 passed` in 0.26s both times), confirming the fixture is hermetic.

The test is **not** registered as an expected failure. `xfail_strict = true` would flip it to an error the moment D-05 landed, and marking it would record a defect as intended behaviour.

## Deviations from Plan

### 1. [Rule 3 — Blocking] pyrefly could not resolve `tests.fake_sane`

- **Found during:** Task 1 verification.
- **Issue:** `uv run pyrefly check src tests` failed with `Cannot find module 'tests.fake_sane' [missing-import]`. Pyrefly infers a single import root from the src layout (`src`), so the `tests` package is unresolvable — yet the plan mandates the package-qualified import form, which is the only one that works under pytest 9.
- **First attempt, rejected:** `--search-path .` resolved the import but **regressed 14 new errors**, because it lets `src/saneless` resolve as both `saneless.*` and `src.saneless.*`, and the two identities are not assignable to each other (`Argument 'src.saneless.vocabulary.ErrorCategory' is not assignable to parameter with type 'saneless.vocabulary.ErrorCategory'`).
- **Fix:** `search-path = ["src", "."]` in `[tool.pyrefly]`, with `src` first so each source file pins to one module name and `.` resolves `tests` only. Measured: 0 errors.
- **Why this is not a suppression:** it declares a true fact about the project's import roots. No `# type: ignore`, no `# noqa`, no rule disabled.
- **Files modified:** `pyproject.toml`. **Commit:** `72dbde1`.

### 2. [Rule 1 — Bug in my own test, not the fake] iterator call-sequence assertion

- **Found during:** Task 1 GREEN.
- **Issue:** I first asserted a two-page scan produces `["start", "snap", "start", "snap"]`. The fake produced a trailing fifth `"start"`.
- **Resolution:** **the fake was right and the test was wrong.** The real `_SaneIterator` learns the feeder is empty only by calling `start()` one more time and catching the message it raises, so `start, snap, start, snap, start` is the faithful sequence. I corrected the assertion and added a comment explaining the probe, rather than trimming the fake to match a convenient expectation — trimming it would have reintroduced exactly the `iter(list)` infidelity this plan exists to kill.

### 3. [Deliberate, documented divergence] groups remain in the option table

The real `__load_option_dict` filters `TYPE_GROUP` options out of `opt`, which makes the library's own `"Groups don't have values"` branch unreachable dead code. The fake keeps groups in the table so that documented branch is exercisable. Either way a group raises `AttributeError`; only the message differs. This is recorded in the module docstring.

### 4. [Doc consistency] a fourth stale filter in CONTRIBUTING

CONTRIBUTING's prek section carried a fourth `uv run pytest -m "not browser"` that my change made stale. Updating it verbatim would have produced three occurrences of the new filter and broken the plan's exact acceptance count of two, so it now defers to "the five commands above". No stale filter survives anywhere (`grep -c 'm "not browser"' CONTRIBUTING.md` is 0).

## Verification

| Check | Result |
|---|---|
| `uv run pytest -m "not browser and not sane_hardware" -q` | **777 passed, 33 deselected**, 0 failed (baseline 742) |
| `uv run pytest tests/test_scanner.py -q -k TestFakeSaneContract` | **35 passed**, 0 failed (plan required ≥12) |
| `uv run pytest -m sane_hardware -q` | **2 passed, 1 failed** — the documented RED ten-page test |
| `uv run ruff check .` | exit 0 |
| `uv run ruff format --check .` | exit 0, 43 files |
| `uv run ty check` | All checks passed |
| `uv run pyrefly check src tests` | 0 errors |
| `grep -rn "# noqa\|# type: ignore" tests/fake_sane.py tests/test_sane_hardware.py` | no matches |

Acceptance probes: unknown-option assignment prints `1`; `type(dev.resolution).__name__` prints `float`; qualified import count 1; bare import count 0; `scope="session"` 1; `monkeypatch.setenv` 0; `xfail` 0; `test:0` 4; ci.yml `apt-get install` 2 and `libsane-dev` 2 (unchanged).

All four commits landed with hooks enabled. No `--no-verify`, no `SKIP=`, no stub.

## Known Stubs

None. The one deliberately failing test is a recorded defect reproduction, not a stub.

## Threat Flags

None. This plan adds no production surface. `SANE_CONFIG_DIR` (T-24-01) appears only inside the test fixture, inside a `MonkeyPatch.context()` that unsets it at session end, and appears nowhere in `src/` or any shipped config.

## Notes for Downstream Plans

- **24-04 must turn the ten-page test green** by removing the pure-black check, and owns adding the `uv run pytest -m sane_hardware` CI step. Until then CI stays green because the marker is deselected.
- **24-08** deletes `MockSaneDev`, `MockSaneModule` and `_FakeSaneDevice`; all three are untouched here by design.
- **Finding 9's Protocol lie is still live.** `SaneDevice.resolution` is declared `int` at `sane_backend.py:219` while the real object returns `float`. The fake returns `float` faithfully, but nothing in this plan assigns it to a `SaneDevice`, so no type checker has been forced to notice yet. Whichever plan wires the fake into `SaneBackend` will surface it and must fix the Protocol rather than annotate around it.

## Self-Check: PASSED

- `tests/fake_sane.py` — FOUND
- `tests/test_sane_hardware.py` — FOUND
- `.planning/phases/24-scanner-truthfulness/24-01-SUMMARY.md` — FOUND
- Commit `d1a62ce` — FOUND
- Commit `72dbde1` — FOUND
- Commit `072b7a8` — FOUND
- Commit `26bbb97` — FOUND
