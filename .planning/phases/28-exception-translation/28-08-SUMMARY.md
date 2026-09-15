---
phase: 28-exception-translation
plan: 08
subsystem: web-ui
tags: [css, pico, dark-mode, contrast, playwright, tdd]
requires:
  - "28-01: JobState.CANCELLED, status.html/history.html CANCELLED branches"
provides:
  - "app.css .status-cancelled on var(--pico-muted-color)"
  - "Playwright TestCancelledStatusRendering + card placement in AA contrast tests"
  - "UI-SPEC CANCELLED colour, contrast and state rows"
affects: [28-09]
tech-stack:
  added: []
  patterns:
    - "Pico semantic token used directly for a scheme-dependent status colour (convention rule 4)"
    - "Contrast probe gains a `card` placement (first <article>)"
key-files:
  created: []
  modified:
    - src/saneless/web/static/app.css
    - tests/test_browser.py
    - .planning/UI-SPEC.md
decisions:
  - "CANCELLED uses Pico's --pico-muted-color directly with no app-owned pair: Chromium measures 5.36:1 light, 4.77:1 dark page/td, 4.53:1 dark card, all AA (D-01)"
  - "The AA contrast parametrisation measures a card placement for every status class, so the tight 4.53:1 dark-card margin is guarded, not just documented"
metrics:
  duration: 14min
  completed: 2026-09-15
  tasks: 2
  files: 3
requirements: [EXC-04]
---

# Phase 28 Plan 08: CANCELLED Muted Styling Summary

A CANCELLED job now shows in Pico's muted grey, never the error red, through one rule: `.status-cancelled { color: var(--pico-muted-color); }`. Playwright proves four things in Chromium:
- the grey is a fourth status colour, distinct from the other three;
- it meets AA on every surface in light, dark and forced-dark schemes;
- it carries no alert;
- the state is terminal: polling stops, Scan re-enables and history repaints.

## What Was Built

### Task 1: browser tests (RED), then the CSS rule (GREEN)
- **`tests/test_browser.py`**
  - `_MUTED` palette constant (`rgb(100, 107, 121)` / `rgb(123, 132, 149)`) sits beside `_AMBER` and `_ERROR_RED`.
  - `_PROBE_STATUS_COLOURS` now probes four classes, and the fallback distinct-colour check expects 4.
  - New `_PROBE_MUTED_TOKEN` resolves `var(--pico-muted-color)` through a probe's `color`, so the result is an `rgb()` string.
  - `_PROBE_CONTEXT_CONTRAST` gains a `card` context that appends the probe to the first `<article>`.
  - `test_status_colour_meets_aa_contrast` covers 4 classes × 3 placements × 2 schemes (24 tests). The cancelled grey is also pinned by value.
  - New `test_forced_dark_theme_gives_cancelled_the_dark_muted_colour` checks the value and the AA ratio on the dark card under `data-theme="dark"`.
  - New class `TestCancelledStatusRendering`. Its `cancelled_page` fixture uses `finish_job(..., JobState.CANCELLED, error=...)`; teardown clears the pointer and deletes the job (T-28-31). Tests:
    - the copy renders and no element has `role="alert"` (light and dark);
    - the colour equals the muted token and differs from the error red (light and dark);
    - the page is terminal: Scan is enabled with no `aria-busy`, there is no `hx-trigger`, and zero `/api/jobs/current/status` requests go out in 2.5 s;
    - a swap re-enables Scan;
    - a swap repaints the history table with a `td.status-cancelled` reading `Cancelled`.
- **`src/saneless/web/static/app.css`**: the `.status-cancelled` rule sits beside done/error, with a comment on D-01, N-08 and convention rule 4.

### Task 2: UI-SPEC
Added to `.planning/UI-SPEC.md`:
- a colour-table row "Neutral (cancelled)";
- two `.status-cancelled` rows in the measured contrast table, from the values Chromium reported;
- a `CANCELLED` row in the status table;
- the "deliberate stop, neither red nor an alert" sentence;
- a note naming the test that guards the 4.53:1 dark-card margin.

It also brings the component inventory up to date: twelve status presentations, and the history cell class list includes `.status-cancelled`.

## Measured in Chromium

| Class | Scheme | Colour | page / td | card |
|-------|--------|--------|-----------|------|
| `.status-cancelled` | light | `rgb(100, 107, 121)` | 5.356 | 5.356 |
| `.status-cancelled` | dark | `rgb(123, 132, 149)` | 4.766 | 4.528 |
| `.status-cancelled` | forced dark | `rgb(123, 132, 149)` | — | 4.528 |

These match the research estimates, and the existing classes' card values match the numbers UI-SPEC already recorded. Since every ratio passes, no `--saneless-status-cancelled` pair was needed.

## Verification

- RED (`fc2a88b`): 9 tests failed; the unstyled cancelled text inherited the body colour (`rgb(55, 60, 68)` light, `rgb(194, 199, 208)` dark). The terminal-behaviour tests already passed, because plan 28-01's templates exist.
- Test runs after the GREEN commit:

  | Command | Result |
  |---------|--------|
  | `uv run pytest tests/test_browser.py -m browser -k "cancel or Cancel or contrast or dark or colour or fallback"` | 45 passed |
  | `uv run pytest -m browser -q` | 68 passed |
  | `-k cancel` | 14 passed |
  | `uv run pytest tests/test_web_state_rendering.py tests/test_vendor_assets.py tests/test_web.py` | 159 passed |

- `ruff check`, `ruff format --check`, `ty check` and `pyrefly check src tests` all report 0 errors.
- UI-SPEC acceptance checks:
  - `grep -c status-cancelled` gives 7;
  - `| CANCELLED |` matches once;
  - both contrast rows show PASS with a ratio of at least 4.5.

## Deviations from Plan

**1. [Rule 2 - Missing coverage] The card surface is measured, not only documented**
- **Found during:** Task 1
- **Issue:** The plan requires AA "on the card". The existing contrast probe measured only `status-area` and `history-cell`, and the UI-SPEC card column had never been asserted. The dark card, at 4.53:1, is the tightest margin for the muted grey.
- **Fix:** Added a `card` placement to the probe and to the AA parametrisation. This also adds card checks for done, error and fallback, and all of them pass.
- **Commit:** fc2a88b

**2. Polling stop observed, not only inferred from the attribute**
- The terminal test also watches for 2.5 s and asserts that no status poll request is sent. This backs up the `hx-trigger` attribute check.

## TDD Gate Compliance

| Task | RED commit | GREEN commit |
|------|------------|--------------|
| 1 | fc2a88b | 4a2424b |

No refactor commit was needed.

## Commits

- `fc2a88b` test(28-08): add failing browser tests for CANCELLED muted rendering
- `4a2424b` feat(28-08): style CANCELLED status with Pico's muted token
- `6c38124` docs(28-08): record CANCELLED status and measured contrast in UI-SPEC

## Known Stubs

None.

## Self-Check: PASSED

- FOUND: src/saneless/web/static/app.css (`.status-cancelled` with `var(--pico-muted-color)`)
- FOUND: tests/test_browser.py (`TestCancelledStatusRendering`, `"status-cancelled"` in probe and AA list)
- FOUND: .planning/UI-SPEC.md (CANCELLED rows)
- FOUND commits: fc2a88b, 4a2424b, 6c38124
